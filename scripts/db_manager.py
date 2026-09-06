"""
All SQLite operations for the marketplace scanner.

Tracks one row per (publisher, image, sku, region, architecture).
The `version` column always holds the LATEST version seen for that SKU.

check_and_upsert() returns one of:
  "new"       -> brand-new SKU; row inserted with validated='unknown'
  "updated"   -> existing SKU got a newer version; row updated, validation state PRESERVED
  "unchanged" -> SKU known at same/older version; only last_checked refreshed
"""

import logging
import os
import re
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

NEW = "new"
UPDATED = "updated"
UNCHANGED = "unchanged"

# Phase 2 validation states written back to the `validated` column.
KNOWN_SUPPORTED = "known_supported"
KNOWN_UNSUPPORTED = "known_unsupported"
PENDING_PUBLISH = "pending_publish"        # summary-e-mail label; no code writes it, but Phase 2 re-queues rows found in it
UNKNOWN = "unknown"
_VALID_STATES = {KNOWN_SUPPORTED, KNOWN_UNSUPPORTED, PENDING_PUBLISH, UNKNOWN}

# verdict_source value meaning "the last check could not reach PMC". Not a
# verdict: it leaves `validated` alone and only marks the row for a retry.
PROBE_ERROR = "probe_error"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def version_tuple(version: str) -> tuple[int, ...]:
    """Parse a marketplace version into ints for NUMERIC comparison.

    '9.10.2026062413' is newer than '9.8.2026062413' but sorts BELOW it as a
    string, because '1' < '8'. Every version comparison in Phase 1 must go
    through this; the date suffix hides the bug until a minor reaches 10.
    """
    parts: list[int] = []
    for token in str(version or "").split("."):
        m = re.match(r"\d+", token.strip())
        parts.append(int(m.group()) if m else 0)
    return tuple(parts)


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _lazy_migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the initial schema, for in-place upgrades."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(images)").fetchall()}
    if not cols:
        return
    adds = []
    if "architecture" not in cols:
        adds.append("ALTER TABLE images ADD COLUMN architecture TEXT NOT NULL DEFAULT 'x86_64'")
    if "family" not in cols:
        adds.append("ALTER TABLE images ADD COLUMN family TEXT NOT NULL DEFAULT 'unknown'")
    if "distro_label" not in cols:
        adds.append("ALTER TABLE images ADD COLUMN distro_label TEXT NOT NULL DEFAULT ''")
    if "last_validated_version" not in cols:
        adds.append("ALTER TABLE images ADD COLUMN last_validated_version TEXT NOT NULL DEFAULT ''")
    if "last_validated_image_version" not in cols:
        adds.append("ALTER TABLE images ADD COLUMN last_validated_image_version TEXT NOT NULL DEFAULT ''")
    if "last_regressed_version" not in cols:
        adds.append("ALTER TABLE images ADD COLUMN last_regressed_version TEXT NOT NULL DEFAULT ''")
    if "reason" not in cols:
        adds.append("ALTER TABLE images ADD COLUMN reason TEXT NOT NULL DEFAULT ''")
    if "verdict_source" not in cols:
        adds.append("ALTER TABLE images ADD COLUMN verdict_source TEXT NOT NULL DEFAULT ''")
    if adds:
        logger.warning(
            "Legacy schema detected — adding new columns. "
            "Delete the DB file for a fully-clean schema (the legacy UNIQUE "
            "constraint cannot be altered)."
        )
        for stmt in adds:
            conn.execute(stmt)
        conn.commit()

    # 'pending_validation' was written by an older Phase 2 and is no longer set
    # by anything. Phase 2 skipped that state and the reset preserved it, so the
    # rows could never be validated or cleared -- release them. The markers go
    # too: a surviving last_validated_version would let Gate 3 trust the row
    # without a run, which is the opposite of releasing it.
    stranded = conn.execute(
        """UPDATE images
              SET validated                    = 'unknown',
                  last_validated_version       = '',
                  last_validated_image_version = '',
                  last_regressed_version       = '',
                  reason                       = '',
                  verdict_source               = ''
            WHERE validated = 'pending_validation'"""
    ).rowcount
    if stranded:
        conn.commit()
        logger.warning("Released %d row(s) stranded in 'pending_validation' -> 'unknown'", stranded)


def initialize(db_path: str, schema_path: str) -> None:
    """Create the database from schema.sql (idempotent)."""
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    with open(schema_path, "r") as fh:
        schema_sql = fh.read()

    conn = _connect(db_path)
    try:
        # Migrate any legacy table FIRST, so the schema's CREATE INDEX
        # statements (e.g. idx_architecture) don't reference a column the old
        # table is missing.
        _lazy_migrate(conn)
        conn.executescript(schema_sql)
        conn.commit()
        logger.info("Database ready at %s", db_path)
    finally:
        conn.close()


def check_and_upsert(
    db_path: str,
    publisher: str,
    image: str,
    sku: str,
    version: str,
    region: str,
    architecture: str = "x86_64",
    family: str = "unknown",
    distro_label: str = "",
) -> str:
    """Upsert a SKU row, deduplicating across versions.

    Returns 'new', 'updated', or 'unchanged' (see module docstring).
    On 'updated': version + date_added + last_modified + last_checked are all
    set to now; validated state is preserved (per design).
    """
    now = _now_iso()
    conn = _connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, version FROM images
            WHERE publisher    = ?
              AND image        = ?
              AND sku          = ?
              AND region       = ?
              AND architecture = ?
            """,
            (publisher, image, sku, region, architecture),
        )
        row = cursor.fetchone()

        if row is None:
            cursor.execute(
                """
                INSERT INTO images
                    (publisher, image, sku, version, region, architecture,
                     family, distro_label,
                     date_added, last_modified, last_checked, validated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unknown')
                """,
                (publisher, image, sku, version, region, architecture,
                 family, distro_label, now, now, now),
            )
            conn.commit()
            logger.info(
                "New SKU: %s / %s / %s [%s, %s] v%s",
                publisher, image, sku, region, architecture, version,
            )
            return NEW

        # Existing SKU -- compare numerically. Marketplace versions are date-style
        # ('24.04.202405010', '9.3.2023121113'), but the leading minor rolls past
        # 9, and a string compare would rank '9.10.x' below '9.8.x' and miss the
        # update entirely.
        if version_tuple(version) > version_tuple(row["version"]):
            cursor.execute(
                """
                UPDATE images
                   SET version       = ?,
                       date_added    = ?,
                       last_modified = ?,
                       last_checked  = ?,
                       family        = ?,
                       distro_label  = ?
                 WHERE id = ?
                """,
                (version, now, now, now, family, distro_label, row["id"]),
            )
            conn.commit()
            logger.info(
                "Version bump: %s / %s / %s [%s, %s]  %s -> %s",
                publisher, image, sku, region, architecture, row["version"], version,
            )
            return UPDATED

        # Same or older version we've already seen.
        cursor.execute(
            "UPDATE images SET last_checked = ? WHERE id = ?",
            (now, row["id"]),
        )
        conn.commit()
        return UNCHANGED

    finally:
        conn.close()


def get_image_record(
    db_path: str,
    publisher: str,
    image: str,
    sku: str,
    region: str,
    architecture: str = "x86_64",
) -> dict:
    conn = _connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM images
            WHERE publisher    = ?
              AND image        = ?
              AND sku          = ?
              AND region       = ?
              AND architecture = ?
            """,
            (publisher, image, sku, region, architecture),
        )
        row = cursor.fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def set_validation_state(
    db_path: str,
    identity: tuple[str, str, str, str, str],
    state: str,
    last_validated_version: str | None = None,
    reason: str | None = None,
    verdict_source: str | None = None,
) -> bool:
    """Phase 2/3: update the validation verdict for one image row.

    identity is the full row identity tuple
    (publisher, image, sku, region, architecture) — the same key used by
    check_and_upsert / get_image_record. ``state`` must be one of
    'known_supported', 'known_unsupported', 'pending_publish', 'unknown'.

    ``last_validated_version`` is the AzNFS version a successful Phase 3 run
    just validated on prod; when given it is recorded so the next Phase 2 run
    can skip re-validating the same version. Leave it None to preserve the
    stored value.

    ``reason`` is the human-readable verdict reason (e.g. why a row is
    known_unsupported); pass "" to clear it on a known_supported verdict, or
    leave it None to preserve the stored value. It is surfaced in the monthly
    digest's known_unsupported table. All other Phase 1 columns are preserved.

    ``verdict_source`` records which phase decided: 'gate' (Phase 2) or 'lisa'
    (Phase 3). Phase 2 re-checks its own 'gate' verdicts on later runs; 'lisa'
    verdicts are left alone so a failing distro is not re-provisioned daily.
    The column also carries 'probe_error' (set by :func:`mark_probe_failed`),
    which is not a verdict at all -- see that function. Writing any real verdict
    here clears it.

    Returns True if a row was updated, False if no matching row exists.
    """
    if state not in _VALID_STATES:
        raise ValueError(
            f"invalid validation state {state!r}; expected one of {sorted(_VALID_STATES)}"
        )
    publisher, image, sku, region, architecture = identity
    now = _now_iso()
    conn = _connect(db_path)
    try:
        # Ensure newer columns (reason, …) exist before we write them, so a
        # manual Phase 2 run against a DB that predates them still works.
        _lazy_migrate(conn)
        set_cols = ["validated = ?", "last_checked = ?"]
        params: list = [state, now]
        if last_validated_version is not None:
            set_cols.append("last_validated_version = ?")
            params.append(last_validated_version)
        if reason is not None:
            set_cols.append("reason = ?")
            params.append(reason)
        if verdict_source is not None:
            set_cols.append("verdict_source = ?")
            params.append(verdict_source)
        params.extend([publisher, image, sku, region, architecture])
        cur = conn.execute(
            f"""
            UPDATE images
               SET {", ".join(set_cols)}
             WHERE publisher    = ?
               AND image        = ?
               AND sku          = ?
               AND region       = ?
               AND architecture = ?
            """,
            params,
        )
        conn.commit()
        if cur.rowcount == 0:
            logger.warning(
                "set_validation_state: no row for %s / %s / %s [%s, %s]",
                publisher, image, sku, region, architecture,
            )
            return False
        logger.info(
            "Validation state: %s / %s / %s [%s, %s] -> %s",
            publisher, image, sku, region, architecture, state,
        )
        return True
    finally:
        conn.close()


def get_all_records(db_path: str) -> list[dict]:
    """Return every tracked image row as a list of dicts (for the distro rollup)."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM images ORDER BY publisher, distro_label, sku"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def reset_validation_to_unknown(
    db_path: str, exclude_states: tuple[str, ...] = ()
) -> int:
    """Reset every image row's ``validated`` back to 'unknown'.

    Used by the one-shot full re-validation: after a broken Phase 3 run buried
    distros as 'known_unsupported' (or marked some 'known_supported'), this
    clears those verdicts so the backlog feed re-runs the WHOLE fleet through
    Phase 2/3 again. ``last_validated_version``, ``last_validated_image_version``
    and ``last_regressed_version`` are ALL cleared so Phase 2's Gate 3 treats each
    as a first-time validation -- no stale regression marker survives a reset (a
    surviving marker would let Gate 3's ``known_bad`` path trust a row without a
    LISA run). ``reason`` and ``verdict_source`` go too, so a reset row carries no
    trace of the verdict it used to have. Rows whose current state is in
    ``exclude_states`` are left untouched.

    Returns the number of rows reset.
    """
    now = _now_iso()
    conn = _connect(db_path)
    try:
        _lazy_migrate(conn)
        if exclude_states:
            placeholders = ",".join("?" for _ in exclude_states)
            cur = conn.execute(
                f"""
                UPDATE images
                   SET validated                    = 'unknown',
                       last_validated_version       = '',
                       last_validated_image_version = '',
                       last_regressed_version       = '',
                       reason                       = '',
                       verdict_source               = '',
                       last_checked                 = ?
                 WHERE validated NOT IN ({placeholders})
                """,
                (now, *exclude_states),
            )
        else:
            cur = conn.execute(
                """
                UPDATE images
                   SET validated                    = 'unknown',
                       last_validated_version       = '',
                       last_validated_image_version = '',
                       last_regressed_version       = '',
                       reason                       = '',
                       verdict_source               = '',
                       last_checked                 = ?
                """,
                (now,),
            )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def get_rows_by_state(db_path: str, state: str) -> list[dict]:
    """Return all image rows currently in a given ``validated`` state.

    Phase 2 uses this to rebuild its work queue: rows parked in
    'pending_publish' on a previous run are re-processed (re-checked against
    prod) on the next run, even if Phase 1 did not re-emit them in
    needs_validation.json. This is what lets a distro that was waiting on a
    manual publish flow back into lisa_jobs.json once the package appears.
    """
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM images WHERE validated = ? ORDER BY publisher, distro_label, sku",
            (state,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_rows_by_verdict_source(db_path: str, source: str) -> list[dict]:
    """Return all image rows whose ``verdict_source`` is ``source``.

    Phase 2 uses this with 'probe_error' to find the rows whose last check could
    not reach PMC, so exactly those are retried on the next run.
    """
    conn = _connect(db_path)
    try:
        _lazy_migrate(conn)
        rows = conn.execute(
            "SELECT * FROM images WHERE verdict_source = ? "
            "ORDER BY publisher, distro_label, sku",
            (source,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_probe_failed(db_path: str, identity: tuple[str, str, str, str, str]) -> bool:
    """Flag a row as "PMC was unreachable", WITHOUT touching its verdict.

    An unreachable PMC proves nothing, so ``validated`` is deliberately left as
    it was; only ``verdict_source`` is set, which is what makes the row eligible
    for a retry next run. Any later real verdict overwrites the marker.
    """
    publisher, image, sku, region, architecture = identity
    conn = _connect(db_path)
    try:
        _lazy_migrate(conn)
        cur = conn.execute(
            "UPDATE images SET verdict_source = ?, last_checked = ? "
            "WHERE publisher = ? AND image = ? AND sku = ? AND region = ? "
            "AND architecture = ?",
            (PROBE_ERROR, _now_iso(), publisher, image, sku, region, architecture),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_records_since(db_path: str, since_iso: str) -> list[dict]:
    """Return image rows first seen (``date_added``) on/after ``since_iso``.

    ``since_iso`` must be an ISO8601 UTC string in the SAME format as the stored
    ``date_added`` (e.g. '2026-06-01T00:00:00Z'); same-format ISO8601 strings
    compare correctly with a plain lexicographic ``>=``. Used by the monthly
    digest to report the releases first seen within a trailing window.
    """
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM images WHERE date_added >= ? "
            "ORDER BY publisher, distro_label, sku",
            (since_iso,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def distinct_distro_labels(db_path: str) -> set[str]:
    """Return the set of distro_label values currently tracked.

    Used to diff at the distro-release level: snapshot before a scan, compare
    after, and the difference is the set of brand-new OS releases to validate.
    """
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT DISTINCT distro_label FROM images").fetchall()
        return {r["distro_label"] for r in rows}
    finally:
        conn.close()


def get_meta(db_path: str, key: str) -> str | None:
    """Return a value from the meta key/value table, or None if the key is absent."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def set_meta(db_path: str, key: str, value: str) -> None:
    """Insert or update a value in the meta key/value table."""
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()
