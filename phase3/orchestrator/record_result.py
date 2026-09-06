"""
Phase 3 - record the validation verdict (DB update + one summary e-mail).

Runs AFTER LISA validation. Phase 3 is LISA testing ONLY: there is no PMC prod
query here (Phase 2 already owns the "is it on prod?" check). For each distro we
simply record the outcome and, at the end of the run, send a SINGLE summary
e-mail listing every distro and -- for the failures -- which tier/step failed.

  LISA passed -> validated = known_supported
  LISA failed -> validated = known_unsupported   (terminal; a human resets the
                 DB row to 'unknown' if a transient/flaky failure buried a good
                 distro -- there is no automatic retry).

The DB row is matched on the SAME 5-key identity Phase 1/Phase 2 use
(publisher, image, sku, region, architecture) so the update actually lands.
"""

import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Tuple

from . import config

logger = logging.getLogger(__name__)

# Marks a verdict as VM-tested, so Phase 2 leaves it alone instead of
# re-provisioning the distro on every run.
LISA_VERDICT = "lisa"

# Marks a run that never reached a verdict (VM never deployed, Azure refused).
# Phase 2 re-feeds rows carrying this so the distro is retried next run.
INFRA_ERROR = "lisa_infra_error"


# ---------------------------------------------------------------------------
# Job model (one entry per distro handed over from Phase 2 / fed to LISA)
# ---------------------------------------------------------------------------
@dataclass
class LisaJob:
    """A single distro job. Field names match Phase 2's lisa_jobs.json exactly,
    so ``load_jobs`` consumes that artifact directly.

    The image identity fields (publisher/image/sku/region/arch) match the Phase 1
    ``images`` table so the DB row can be updated. ``version`` is the marketplace
    image version used to build the URN for LISA. ``aznfs_package_url`` is the
    published package Phase 3 installs; ``aznfs_version`` is asserted in Tier 1/3.
    ``failure_reason`` is filled by the driver (the failing tier) on a LISA fail.
    """

    publisher: str
    image: str
    sku: str
    version: str
    region: str
    arch: str = "x86_64"
    aznfs_version: str = ""
    aznfs_package_url: str = ""
    lisa_passed: bool = False
    distro_label: str = ""
    failure_reason: str = ""
    logs_url: str = ""
    infra_error: bool = False

    def image_key(self) -> Dict[str, str]:
        # The 5-key identity Phase 1/Phase 2 use (NOT version; WITH architecture).
        return {
            "publisher": self.publisher,
            "image": self.image,
            "sku": self.sku,
            "region": self.region,
            "architecture": self.arch,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _github_run_url() -> str:
    """Best-effort GitHub Actions run URL for summary links."""
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return ""


def _image_urn(job: LisaJob) -> str:
    return f"{job.publisher}:{job.image}:{job.sku}:{job.version}"


# ---------------------------------------------------------------------------
# SQLite update (extends the Phase 1 images table with last_validated)
# ---------------------------------------------------------------------------
def _ensure_phase3_columns(conn: sqlite3.Connection) -> None:
    """Add the columns Phase 3 writes if they do not exist (idempotent)."""
    for ddl in (
        "ALTER TABLE images ADD COLUMN last_validated TEXT",
        "ALTER TABLE images ADD COLUMN last_checked TEXT",
        "ALTER TABLE images ADD COLUMN reason TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE images ADD COLUMN last_validated_version TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE images ADD COLUMN last_regressed_version TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE images ADD COLUMN last_validated_image_version TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE images ADD COLUMN verdict_source TEXT NOT NULL DEFAULT ''",
    ):
        try:
            conn.execute(ddl)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # duplicate column name - already added in a prior run


def _record_validation(
    image_key: Dict[str, str], validated: str, reason: str = "",
    last_validated_version: str | None = None,
    last_regressed_version: str | None = None,
    last_validated_image_version: str | None = None,
) -> None:
    """Set validated + last_validated (+ reason) on the matching images row.

    Matches on (publisher, image, sku, region, architecture) - the SAME identity
    Phase 1/Phase 2 use - so the row is found (the earlier version matched on
    `version`, which is 'latest' in the job and never equals the stored
    marketplace version, so it updated zero rows). ``reason`` is the verdict
    reason (empty on a pass, the failure reason on known_unsupported); it is
    surfaced in the monthly digest's known_unsupported table.

    On a pass, ``last_validated_version`` is the AzNFS version that was validated
    and ``last_validated_image_version`` is the marketplace image it ran on (used
    by Phase 2's rate-limited image-drift re-validation). ``last_regressed_version``
    records a newer package that FAILED on an already-supported distro (set on
    regression, "" to clear on a pass). Leave any of them None to preserve the
    stored value.
    """
    now = _now_iso()
    conn = sqlite3.connect(config.DB_PATH)
    try:
        _ensure_phase3_columns(conn)
        set_cols = ["validated = ?", "last_validated = ?", "last_modified = ?", "reason = ?",
                    "verdict_source = ?"]
        params: list = [validated, now, now, reason, LISA_VERDICT]
        if last_validated_version is not None:
            set_cols.append("last_validated_version = ?")
            params.append(last_validated_version)
        if last_regressed_version is not None:
            set_cols.append("last_regressed_version = ?")
            params.append(last_regressed_version)
        if last_validated_image_version is not None:
            set_cols.append("last_validated_image_version = ?")
            params.append(last_validated_image_version)
        params.extend([
            image_key["publisher"], image_key["image"], image_key["sku"],
            image_key["region"], image_key["architecture"],
        ])
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
            logger.warning("no images row matched %s (state=%s)", image_key, validated)
    finally:
        conn.close()


def _current_validation(image_key: Dict[str, str]) -> Tuple[str, str, str]:
    """Return (validated, last_validated_version, last_validated_image_version)
    for the row, ("", "", "") if none."""
    conn = sqlite3.connect(config.DB_PATH)
    try:
        _ensure_phase3_columns(conn)
        row = conn.execute(
            """
            SELECT validated, last_validated_version, last_validated_image_version FROM images
             WHERE publisher = ? AND image = ? AND sku = ?
               AND region = ? AND architecture = ?
            """,
            (image_key["publisher"], image_key["image"], image_key["sku"],
             image_key["region"], image_key["architecture"]),
        ).fetchone()
        return (row[0] or "", row[1] or "", row[2] or "") if row else ("", "", "")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Notifications - reuse the Phase 1 ACS notifier (one summary per run)
# ---------------------------------------------------------------------------
def _notify(subject: str, plain: str, html_body: str | None = None) -> None:
    """Send via the Phase 1 ACS notifier when importable; otherwise just log.

    The workflow puts ``scripts/`` on PYTHONPATH (same as Phase 2), so
    ``import notifier`` resolves to scripts/notifier.py and uses its ACS path.
    Lazy import keeps this module testable without Phase 1 on the path.
    """
    try:
        import notifier  # type: ignore
    except ModuleNotFoundError:
        try:
            from scripts import notifier  # type: ignore
        except ModuleNotFoundError:
            logger.info("NOTIFY (no notifier module):\n%s\n%s", subject, plain)
            return
    notifier.notify(subject, plain, html_body=html_body)


def _table_html(title: str, columns: List[Tuple[str, str]], rows: List[Dict[str, str]]) -> str:
    """Render one titled HTML table. ``columns`` = [(dict_key, header), ...]."""
    import html as _html

    head = "".join(
        "<th style='text-align:left;padding:6px 10px;background:#0078d4;color:#fff;"
        f"font-weight:600;white-space:nowrap'>{_html.escape(hdr)}</th>"
        for _, hdr in columns
    )
    if rows:
        body = ""
        for i, r in enumerate(rows):
            bg = "#ffffff" if i % 2 == 0 else "#f6f8fa"
            cells = "".join(
                "<td style='padding:6px 10px;border-top:1px solid #e1e4e8;"
                f"word-break:break-all'>{_html.escape(str(r.get(k, '') or ''))}</td>"
                for k, _ in columns
            )
            body += f"<tr style='background:{bg}'>{cells}</tr>"
    else:
        body = (
            f"<tr><td colspan='{len(columns)}' "
            "style='padding:6px 10px;color:#888'>(none)</td></tr>"
        )
    return (
        f"<h3 style='font-family:Segoe UI,sans-serif;font-size:15px;margin:18px 0 6px'>"
        f"{_html.escape(title)}</h3>"
        "<table style='border-collapse:collapse;font-family:Segoe UI,sans-serif;"
        "font-size:13px;border:1px solid #e1e4e8'>"
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    )


def _send_summary(
    processed: int,
    supported: List[Dict[str, str]],
    unsupported: List[Dict[str, str]],
    regressions: List[Dict[str, str]] | None = None,
    infra_errors: List[Dict[str, str]] | None = None,
) -> None:
    """The single end-of-run e-mail: pass / fail (+ package/image-regression) tables."""
    regressions = regressions or []
    infra_errors = infra_errors or []
    reg_subject = f", {len(regressions)} regressed" if regressions else ""
    infra_subject = f", {len(infra_errors)} untestable" if infra_errors else ""
    subject = (
        f"[AzNFS Phase 3] validation summary: {len(supported)} supported, "
        f"{len(unsupported)} unsupported{reg_subject}{infra_subject} (of {processed})"
    )

    def _plain(rows, keys):
        if not rows:
            return "  (none)"
        return "\n".join(
            "  - " + " | ".join(f"{k}={r.get(k, '')}" for k in keys) for r in rows
        )

    plain = (
        f"Phase 3 validated {processed} distro(s) with LISA.\n\n"
        f"a) Validation successful (known_supported) ({len(supported)}):\n"
        f"{_plain(supported, ['label', 'arch'])}\n\n"
        f"b) Validation fails (kept in known_unsupported) ({len(unsupported)}):\n"
        f"{_plain(unsupported, ['label', 'arch', 'urn', 'logs_url', 'reason'])}\n\n"
        f"c) Regressions (newer AzNFS or new image failed; kept known_supported) ({len(regressions)}):\n"
        f"{_plain(regressions, ['label', 'arch', 'urn', 'logs_url', 'reason'])}\n\n"
        f"d) Could NOT be tested -- infrastructure, no verdict recorded, will retry ({len(infra_errors)}):\n"
        f"{_plain(infra_errors, ['label', 'arch', 'urn', 'logs_url', 'reason'])}"
    )

    html_body = (
        "<div style='font-family:Segoe UI,sans-serif;color:#24292e'>"
        f"<p style='font-size:14px'>Phase 3 validated <b>{processed}</b> distro(s) with LISA &mdash; "
        f"<b>{len(supported)}</b> supported, <b>{len(unsupported)}</b> unsupported, "
        f"<b>{len(regressions)}</b> regressed, <b>{len(infra_errors)}</b> untestable.</p>"
        + _table_html(
            f"a) Validation successful (known_supported) ({len(supported)})",
            [("label", "Distro"), ("arch", "Arch")],
            supported,
        )
        + _table_html(
            f"b) Validation fails (kept in known_unsupported) ({len(unsupported)})",
            [
                ("label", "Distro"),
                ("arch", "Arch"),
                ("urn", "Image URN"),
                ("logs_url", "Logs URL"),
                ("reason", "Reason"),
            ],
            unsupported,
        )
        + _table_html(
            f"c) Regressions (newer AzNFS or new image failed; kept known_supported) ({len(regressions)})",
            [
                ("label", "Distro"),
                ("arch", "Arch"),
                ("urn", "Image URN"),
                ("logs_url", "Logs URL"),
                ("reason", "Reason"),
            ],
            regressions,
        )
        + _table_html(
            f"d) Could NOT be tested &mdash; infrastructure, no verdict recorded, will retry ({len(infra_errors)})",
            [
                ("label", "Distro"),
                ("arch", "Arch"),
                ("urn", "Image URN"),
                ("logs_url", "Logs URL"),
                ("reason", "Reason"),
            ],
            infra_errors,
        )
        + "</div>"
    )
    _notify(subject, plain, html_body=html_body)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _mark_infra_error(image_key: Dict[str, str], reason: str) -> None:
    """Flag "could not test", deliberately WITHOUT touching ``validated``.

    A VM that never deployed proves nothing about AzNFS, so the row keeps
    whatever verdict it had; only the marker is set, which is what makes Phase 2
    retry it. Mirrors db_manager.mark_probe_failed on the Phase 2 side.

    ``reason`` is left alone too: it is the detail behind a verdict, so writing
    an Azure error there would leave the row asserting its existing verdict for
    a reason that is not why. The detail goes to the summary e-mail and the run
    log instead.
    """
    now = _now_iso()
    conn = sqlite3.connect(config.DB_PATH)
    try:
        _ensure_phase3_columns(conn)
        cur = conn.execute(
            """
            UPDATE images
               SET verdict_source = ?, last_checked = ?
             WHERE publisher    = ?
               AND image        = ?
               AND sku          = ?
               AND region       = ?
               AND architecture = ?
            """,
            (INFRA_ERROR, now, image_key["publisher"], image_key["image"],
             image_key["sku"], image_key["region"], image_key["architecture"]),
        )
        conn.commit()
        if cur.rowcount == 0:
            logger.warning("no images row matched %s (infra error)", image_key)
        else:
            logger.debug("marked %s for retry: %s", image_key, reason)
    finally:
        conn.close()


def process_job(job: LisaJob) -> Tuple[str, str]:
    """Record one distro's verdict. Returns (validated_state, failure_reason).

    A LISA failure on a distro that is ALREADY known_supported is treated as a
    package REGRESSION, not a distro becoming unsupported: the distro stays
    known_supported at its last-good ``last_validated_version`` (so it keeps being
    re-checked and auto-recovers when a fixed package ships), and the failing
    version is stored in ``last_regressed_version`` so Phase 2 will not re-test /
    re-alert that exact package (a strictly newer one supersedes it). A failure on
    any other prior state (first-time / retry) is a real known_unsupported.

    A run that could not test the distro at all records no verdict: it is only
    flagged for retry, so an Azure problem never retires a distro.
    """
    if job.lisa_passed:
        logger.info("[%s] LISA PASSED -> recording known_supported in DB", job.distro_label)
        _record_validation(job.image_key(), "known_supported", reason="",
                           last_validated_version=job.aznfs_version,
                           last_regressed_version="",
                           last_validated_image_version=job.version)
        return "known_supported", ""
    if job.infra_error:
        logger.warning(
            "[%s] could not be tested (%s) -> no verdict recorded, will retry",
            job.distro_label, job.failure_reason or "no reason",
        )
        _mark_infra_error(job.image_key(), job.failure_reason)
        return "infra_error", job.failure_reason
    prior_state, prior_version, prior_image = _current_validation(job.image_key())
    if prior_state == "known_supported":
        # Distinguish an IMAGE regression (the SAME AzNFS package that previously
        # passed now fails on a newer marketplace image) from a PACKAGE regression
        # (a newer AzNFS package fails). Gate 3 only re-emits a supported distro
        # for a newer package OR an image drift, so an unchanged package version
        # means the OS image was the trigger. The DB also encodes this:
        # last_regressed_version == last_validated_version => image regression.
        if prior_version and job.aznfs_version == prior_version:
            reason = (
                f"image regression: AzNFS {job.aznfs_version} (unchanged) regressed on "
                f"new image {job.version} (last good image {prior_image or '?'}); "
                f"kept known_supported: {job.failure_reason or 'validation failed'}"
            )
        else:
            reason = (
                f"package regression: AzNFS {job.aznfs_version} regressed "
                f"(last good v{prior_version or '?'}); "
                f"kept known_supported: {job.failure_reason or 'validation failed'}"
            )
        logger.warning("[%s] LISA FAILED -> %s, keeping known_supported: %s",
                       job.distro_label, reason.split(':', 1)[0].upper(),
                       job.failure_reason or "no reason")
        # Keep last_validated_version/-image at the last-good values; mark the
        # failing package as regressed so Phase 2 will not re-test it.
        _record_validation(job.image_key(), "known_supported", reason="",
                           last_regressed_version=job.aznfs_version)
        return "regression", reason
    logger.info("[%s] LISA FAILED (%s) -> recording known_unsupported in DB",
                job.distro_label, job.failure_reason or "no reason")
    _record_validation(job.image_key(), "known_unsupported", reason=job.failure_reason)
    return "known_unsupported", job.failure_reason


def run(jobs: List[LisaJob]) -> Dict[str, int]:
    """Record every job's verdict and send ONE summary e-mail. Returns counts."""
    # Like Phase 1 (silent when no new distro is found), stay silent when there
    # is nothing to validate: no jobs means no verdicts, so skip the e-mail.
    if not jobs:
        logger.info("Phase 3: no jobs to record; skipping summary e-mail.")
        return {"known_supported": 0, "known_unsupported": 0, "regressions": 0,
                "infra_errors": 0}
    run_url = _github_run_url()
    supported: List[Dict[str, str]] = []
    unsupported: List[Dict[str, str]] = []
    regressions: List[Dict[str, str]] = []
    infra_errors: List[Dict[str, str]] = []
    for job in jobs:
        label = job.distro_label or f"{job.publisher}/{job.image}/{job.sku}"
        logs_url = job.logs_url or run_url or "n/a"
        urn = _image_urn(job)
        state, reason = process_job(job)
        if state == "known_supported":
            supported.append({"label": label, "arch": job.arch})
        elif state == "regression":
            regressions.append(
                {"label": label, "arch": job.arch, "urn": urn,
                 "logs_url": logs_url, "reason": reason}
            )
        elif state == "infra_error":
            infra_errors.append(
                {"label": label, "arch": job.arch, "urn": urn,
                 "logs_url": logs_url, "reason": reason or "could not be tested"}
            )
        else:
            unsupported.append(
                {
                    "label": label,
                    "arch": job.arch,
                    "urn": urn,
                    "logs_url": logs_url,
                    "reason": reason or "validation failed",
                }
            )
    _send_summary(len(jobs), supported, unsupported, regressions, infra_errors)
    logger.info(
        "Phase 3: %d supported, %d unsupported, %d regressed, %d untestable (of %d)",
        len(supported), len(unsupported), len(regressions), len(infra_errors),
        len(jobs),
    )
    return {
        "known_supported": len(supported),
        "known_unsupported": len(unsupported),
        "regressions": len(regressions),
        "infra_errors": len(infra_errors),
    }


def load_jobs(path: str) -> List[LisaJob]:
    """Load LISA jobs from a JSON file (Phase 2's lisa_jobs.json)."""
    with open(path, "r") as fh:
        raw = json.load(fh)
    known = {f for f in LisaJob.__dataclass_fields__}  # noqa: B019
    return [LisaJob(**{k: v for k, v in item.items() if k in known}) for item in raw]


def main() -> None:
    import argparse

    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="Phase 3: record LISA verdicts in the DB + send a summary."
    )
    parser.add_argument(
        "jobs_json", help="JSON list of LISA job results (with lisa_passed set)."
    )
    args = parser.parse_args()
    run(load_jobs(args.jobs_json))


if __name__ == "__main__":
    main()

