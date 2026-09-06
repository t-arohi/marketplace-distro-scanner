"""Live Phase 2 entry point (PMC **prod**, no PMC API).

Wires the public prod package client (``pmc_packages.ProdPackageIndex``) plus
Phase 1's existing notifier and universal database into
:func:`orchestrator.run_phase2`, and writes the Phase 3 hand-off artifact
``output/lisa_jobs.json``.

Design notes
------------
* Everything is read from the anonymous, public ``packages.microsoft.com`` -- no
  corp proxy, no PMC API, no ADO build, no onboarding metadata.
* The notifier is reused verbatim from Phase 1 (``scripts/notifier.py``) and DB
  verdicts go through Phase 1's universal ``db_manager`` -- this module only
  *adapts* the orchestrator's small Protocol surface onto those functions.
* Before the gates run, each image is enriched with its DB
  ``last_validated_version`` (so Gate 3 can decide if re-validation is needed),
  and any image parked ``pending_publish`` on a previous run is merged back in so
  it re-flows once the package finally appears on prod.
* Phase 1 modules are imported lazily (inside ``_load_phase1``) so tests can
  import this module, and inject fakes, without Phase 1 on the path.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from typing import Any

from . import orchestrator, pmc_packages
import aznfs_support

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration (env-overridable; defaults mirror Phase 1 / the spec)
# ---------------------------------------------------------------------------
PHASE2_INPUT = os.environ.get("PHASE2_INPUT", "output/needs_validation.json")
LISA_JOBS_OUTPUT = os.environ.get("LISA_JOBS_OUTPUT", "output/lisa_jobs.json")
DB_PATH = os.environ.get("DB_PATH", "marketplace.db")


def _identity(entry: dict) -> tuple[str, str, str, str, str]:
    return (
        entry.get("publisher", ""),
        entry.get("image") or entry.get("offer") or "",
        entry.get("sku", ""),
        entry.get("region", ""),
        entry.get("architecture") or entry.get("arch") or "",
    )


# ---------------------------------------------------------------------------
# Phase 1 adapters: map the orchestrator Protocols onto Phase 1's functions
# ---------------------------------------------------------------------------
class Phase1NotifierAdapter:
    """Adapt :class:`orchestrator.NotifierLike` onto Phase 1 ``scripts/notifier``.

    Phase 2 sends exactly one e-mail per run -- the end-of-run summary listing
    every distro and, for the failing ones, the reason. No per-distro mail.
    """

    def __init__(self, notifier_mod: Any) -> None:
        self._n = notifier_mod

    def notify_summary(self, processed, to_phase3, trusted, pending_publish, unsupported, errors) -> None:
        self._n.send_phase2_summary(
            processed=processed,
            to_phase3=to_phase3,
            trusted=trusted,
            pending_publish=pending_publish,
            unsupported=unsupported,
            errors=errors,
        )


class Phase1DbAdapter:
    """Adapt :class:`orchestrator.DbLike` onto Phase 1 ``db_manager.set_validation_state``."""

    def __init__(self, db_mod: Any, db_path: str) -> None:
        self._db = db_mod
        self._path = db_path

    def set_validation_state(self, identity, state, reason=None, verdict_source=None) -> None:
        updated = self._db.set_validation_state(
            self._path, identity, state, reason=reason, verdict_source=verdict_source
        )
        if not updated:
            logger.warning("No DB row matched identity %s (state=%s)", identity, state)

    def mark_probe_failed(self, identity) -> None:
        if not self._db.mark_probe_failed(self._path, identity):
            logger.warning("No DB row matched identity %s (probe failure)", identity)


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------
def load_entries(path: str) -> list[dict]:
    """Load the Phase 1 hand-off (``needs_validation.json``)."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON list, got {type(data).__name__}")
    return data


def entries_from_db(
    db_mod, db_path: str, wanted: set[str],
    exclude_substrings: tuple[str, ...] = (),
) -> list[dict]:
    """Build Phase 2 entries straight from the DB for selected distro_labels.

    A manual Phase 2 run usually depends on Phase 1's ``needs_validation.json``,
    which is delta-only -- it carries just the newly found/updated SKUs, so a
    distro you want to re-validate is often absent. This pulls every tracked SKU
    row from the DB, keeps the wanted ``distro_label`` values, and collapses them
    to one representative SKU per (distro_label, architecture) -- the newest
    marketplace ``version`` -- so Phase 2 can validate exactly the selected
    distros without re-running Phase 1. The DB rows already carry every field
    Phase 2 needs (publisher, image, sku, region, architecture, family,
    distro_label, version, last_validated_version), so they are used as-is and
    bypass the ``enrich_and_merge`` skip filter (this is a deliberate, explicit
    re-validation of the selected distros, regardless of their current state).

    ``exclude_substrings`` drops rows whose OFFER (``image``) or ``sku`` contains
    any of the substrings (case-insensitive) BEFORE picking the representative.
    This matters because the newest marketplace ``version`` for a release is
    often a NON-DEPLOYABLE image: ``advanced-sla`` offers are restricted-audience
    (the subscription is not entitled) and ``pro`` offers carry a purchase plan
    (deploy needs marketplace terms the runner identity cannot accept). Excluding
    them makes the pick fall back to the plan-free plain ``*-server-*`` image,
    which deploys without terms. Non-Ubuntu offers (Rocky/RHEL/SLES) carry none
    of these tokens, so they are unaffected.
    """
    rows = db_mod.get_all_records(db_path)
    if wanted:
        rows = [r for r in rows if (r.get("distro_label") or "").casefold() in wanted]
    if exclude_substrings:
        subs = tuple(s.casefold() for s in exclude_substrings)
        def _ok(r: dict) -> bool:
            hay = f"{r.get('image', '')} {r.get('sku', '')}".casefold()
            return not any(s in hay for s in subs)
        rows = [r for r in rows if _ok(r)]
    chosen: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r.get("distro_label", ""), r.get("architecture", ""))
        cur = chosen.get(key)
        if cur is None or _is_preferred_image(r, cur):
            chosen[key] = r
    return sorted(
        chosen.values(),
        key=lambda r: (r.get("distro_label", ""), r.get("architecture", "")),
    )


# ``known_supported`` is deliberately NOT skipped: it is re-fed below so Gate 3
# can compare the current prod AzNFS version against last_validated_version and
# re-validate when a newer package ships (an unchanged package stays trusted, no
# VM). ``unknown`` (fresh from Phase 1) and re-queued ``pending_publish`` flow on.
#
# ``known_unsupported`` is NOT terminal either: a verdict Phase 2 made itself
# (verdict_source 'gate') is just a repo/package lookup, and it goes stale as
# soon as AzNFS publishes for that distro -- or was never true, if PMC happened
# to be unreachable that run. Those are re-checked every run and self-heal. Only
# ``lisa`` verdicts (the suite actually failed on a VM) are skipped, so a broken
# distro is not re-provisioned daily; reset it explicitly to re-test.
_LISA_VERDICT = "lisa"
# Not a verdict: set when PMC could not be reached, cleared by any real verdict.
_PROBE_ERROR = "probe_error"


def _is_preferred_image(row: dict, cur: dict) -> bool:
    """True when ``row`` is the better representative of the two.

    Not recency alone: most deployable first, then newest version (numerically --
    '9.10.2026...' is newer than '9.8.2026...' but sorts BELOW it as a string),
    then name so a tie cannot resolve differently between runs. See
    ``aznfs_support.is_preferred_image`` for why that order.
    """
    return aznfs_support.is_preferred_image(row, cur, pmc_packages.version_tuple)


def _load_exclusions() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(distro_label prefixes, offer/sku substrings) excluded from the
    known_supported re-feed -- the same env-driven policy as Phase 1
    (``EXCLUDED_DISTRO_PREFIXES`` / ``EXCLUDED_OFFER_SUBSTRINGS``, same defaults).

    Read straight from the environment rather than ``import config``: config.py
    reads a required ``AZURE_SUBSCRIPTION_ID`` at import time, so importing it in a
    bare/manual Phase 2 run without that Azure-only variable would raise and
    (via the caller's guard) silently disable the whole re-feed.
    """
    def _split(var: str, default: str) -> tuple[str, ...]:
        return tuple(
            s.strip().casefold()
            for s in os.environ.get(var, default).split(",")
            if s.strip()
        )
    return (_split("EXCLUDED_DISTRO_PREFIXES", "centos"),
            _split("EXCLUDED_OFFER_SUBSTRINGS", "advanced-sla"))


def enrich_and_merge(entries: list[dict], db_mod: Any, db_path: str) -> list[dict]:
    """Build Phase 2's work queue from the Phase 1 hand-off + the DB.

    The incoming ``entries`` are validated as given -- Phase 1 already limited
    the hand-off to the AzNFS support matrix, and a manual ``--distros`` run is
    an explicit operator choice. Only the DB re-feeds below are matrix-checked,
    since those are re-entries Phase 2 initiates itself and can still surface a
    legacy row for a release AzNFS never published for.

    The DB ``validated`` state is authoritative:

    * Skip any image ``known_unsupported`` from a Phase 3 VM run
      (``verdict_source == 'lisa'``), so a distro the suite actually failed on
      is not re-provisioned daily; reset it explicitly to re-test.
    * Re-check ``known_unsupported`` rows that Phase 2 itself decided: those are
      repo/package lookups that go stale the moment AzNFS publishes for the
      distro, so leaving them terminal silently under-reports support.
    * Enrich the survivors with their DB ``last_validated_version`` and tag their
      ``_db_state`` (Gate 3's baseline; the tag lets process_entry avoid
      downgrading a known_supported distro on a transient re-check failure).
    * Merge images parked ``pending_publish`` on a previous run so they re-flow
      once the package is finally published, even if Phase 1 did not re-emit
      them. De-duplicated against the incoming entries by identity.
    * Re-feed every ``known_supported`` distro so Gate 3 re-checks the current
      prod AzNFS version against ``last_validated_version`` -- a newer package
      re-validates, an unchanged one stays trusted (no VM). This is what makes a
      new AzNFS release re-validate an already-supported distro.
    """
    out: list[dict] = []
    seen: set[tuple] = set()

    for e in entries:
        ident = _identity(e)
        seen.add(ident)
        v_last = e.get("last_validated_version", "")
        v_regressed = e.get("last_regressed_version", "")
        v_img = e.get("last_validated_image_version", "")
        ts = e.get("last_validated", "")
        state = None
        source = ""
        try:
            rec = db_mod.get_image_record(db_path, *ident)
            if rec:
                state = rec.get("validated")
                source = rec.get("verdict_source") or ""
                v_last = rec.get("last_validated_version", v_last)
                v_regressed = rec.get("last_regressed_version", v_regressed)
                v_img = rec.get("last_validated_image_version", v_img)
                ts = rec.get("last_validated", ts)
        except Exception:  # pragma: no cover - DB best-effort; entry default stands
            logger.debug("DB lookup failed for %s; using entry default", ident)
        if state == "known_unsupported" and source == _LISA_VERDICT:
            logger.info("Skipping %s: known_unsupported from a Phase 3 VM run (reset to re-test)", ident)
            continue
        out.append({**e, "last_validated_version": v_last or "",
                    "last_regressed_version": v_regressed or "",
                    "last_validated_image_version": v_img or "",
                    "last_validated": ts or "", "_db_state": state})

    try:
        for row in db_mod.get_rows_by_state(db_path, "pending_publish"):
            ident = (
                row.get("publisher", ""), row.get("image", ""), row.get("sku", ""),
                row.get("region", ""), row.get("architecture", ""),
            )
            if ident in seen or not aznfs_support.is_supported_distro(row.get("distro_label", "")):
                continue
            seen.add(ident)
            out.append(row)  # DB rows already carry family / distro_label / last_validated_version
    except Exception:  # pragma: no cover - re-entry is best-effort
        logger.exception("pending_publish merge skipped")

    # Re-feed already-validated distros so Gate 3 re-checks the current prod
    # AzNFS version against last_validated_version: a NEWER package -> re-validate;
    # an unchanged one -> trusted (no VM). Tagged _db_state so process_entry never
    # DOWNGRADES a known_supported distro if the re-check hits a transient prod
    # hiccup. Collapsed to ONE representative per (distro_label, architecture) --
    # most deployable, then newest -- matching Phase 3's per-URL dedup so a release
    # is re-validated once, not once per SKU; and filtered by the same exclusion
    # policy Phase 1 uses so a stale row for a non-deployable distro/offer
    # (e.g. CentOS, advanced-sla) is not re-fed. Deduped against entries above.
    try:
        distro_prefixes, offer_subs = _load_exclusions()

        def _excluded(row: dict) -> bool:
            label = (row.get("distro_label") or "").casefold()
            hay = f"{row.get('image', '')} {row.get('sku', '')}".casefold()
            return (any(label.startswith(p) for p in distro_prefixes)
                    or any(s in hay for s in offer_subs)
                    or not aznfs_support.is_supported_distro(row.get("distro_label", "")))

        reps: dict[tuple, dict] = {}
        for row in db_mod.get_rows_by_state(db_path, "known_supported"):
            if _excluded(row):
                continue
            key = (row.get("distro_label", ""), row.get("architecture", ""))
            cur = reps.get(key)
            if cur is None or _is_preferred_image(row, cur):
                reps[key] = row
        for row in reps.values():
            ident = (
                row.get("publisher", ""), row.get("image", ""), row.get("sku", ""),
                row.get("region", ""), row.get("architecture", ""),
            )
            if ident in seen:
                continue
            seen.add(ident)
            out.append({**row, "_db_state": "known_supported"})

        # Same re-feed for Phase 2's OWN known_unsupported verdicts: they are
        # repo/package lookups, so they go stale as soon as AzNFS publishes for
        # the distro (and a single unreachable-PMC run could have caused one).
        # Rows a Phase 3 VM run decided are left alone. Legacy rows predating
        # verdict_source are re-checked once, which is what heals them.
        unsupported_reps: dict[tuple, dict] = {}
        for row in db_mod.get_rows_by_state(db_path, "known_unsupported"):
            if _excluded(row) or (row.get("verdict_source") or "") == _LISA_VERDICT:
                continue
            key = (row.get("distro_label", ""), row.get("architecture", ""))
            cur = unsupported_reps.get(key)
            if cur is None or _is_preferred_image(row, cur):
                unsupported_reps[key] = row
        for row in unsupported_reps.values():
            ident = (
                row.get("publisher", ""), row.get("image", ""), row.get("sku", ""),
                row.get("region", ""), row.get("architecture", ""),
            )
            if ident in seen:
                continue
            seen.add(ident)
            out.append({**row, "_db_state": "known_unsupported"})

        # Rows whose last check could not reach PMC. They carry no verdict --
        # only the marker -- and `unknown` rows are not re-fed by anything else,
        # so without this a release stranded by one unreachable run would wait
        # for Phase 1 to re-emit its image, which only happens when it changes.
        for row in db_mod.get_rows_by_verdict_source(db_path, _PROBE_ERROR):
            if _excluded(row):
                continue
            ident = (
                row.get("publisher", ""), row.get("image", ""), row.get("sku", ""),
                row.get("region", ""), row.get("architecture", ""),
            )
            if ident in seen:
                continue
            seen.add(ident)
            logger.info("Retrying %s: last check could not reach PMC", ident)
            out.append({**row, "_db_state": row.get("validated") or None})
    except Exception:  # pragma: no cover - re-check is best-effort
        logger.exception("known_supported re-check re-feed skipped")

    return out


def _load_phase1():
    """Import Phase 1's notifier + db_manager (co-located in the repo)."""
    try:
        import notifier  # type: ignore
        import db_manager  # type: ignore
    except ModuleNotFoundError:  # pragma: no cover - exercised in the real repo
        from scripts import notifier  # type: ignore
        from scripts import db_manager  # type: ignore
    return notifier, db_manager


# ---------------------------------------------------------------------------
# Orchestration entry point
# ---------------------------------------------------------------------------
def run(
    *,
    entries: list[dict] | None = None,
    prod: Any | None = None,
    notifier_obj: Any | None = None,
    db: Any | None = None,
    lisa_jobs_path: str | None = None,
) -> list[dict]:
    """Wire the live prod client (or injected fakes) and run Phase 2.

    All collaborators are optional so tests can inject fakes; anything left
    ``None`` is constructed from the environment. When ``entries`` is ``None`` the
    Phase 1 hand-off is loaded and enriched/merged from the DB; tests pass an
    explicit ``entries`` list and skip the DB step.
    """
    if lisa_jobs_path is None:
        lisa_jobs_path = LISA_JOBS_OUTPUT

    notifier_mod = db_mod = None
    if notifier_obj is None or db is None or entries is None:
        notifier_mod, db_mod = _load_phase1()
    if notifier_obj is None:
        notifier_obj = Phase1NotifierAdapter(notifier_mod)
    if db is None:
        db = Phase1DbAdapter(db_mod, DB_PATH)
    if entries is None:
        entries = enrich_and_merge(load_entries(PHASE2_INPUT), db_mod, DB_PATH)
    if prod is None:
        logger.info("Using PMC prod content server %s", pmc_packages.PROD_BASE)
        prod = pmc_packages.from_env()

    jobs = orchestrator.run_phase2(
        entries=entries,
        prod=prod,
        db=db,
        notifier=notifier_obj,
        lisa_jobs_path=lisa_jobs_path,
    )
    logger.info("Phase 2 complete: %d LISA job(s) -> %s", len(jobs), lisa_jobs_path)
    return jobs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 2: validate AzNFS coverage on PMC prod.")
    parser.add_argument("--input", default=PHASE2_INPUT, help="Phase 1 needs_validation.json")
    parser.add_argument("--output", default=LISA_JOBS_OUTPUT, help="lisa_jobs.json output path")
    parser.add_argument(
        "--distros", default="",
        help="comma-separated distro_label allow-list (case-insensitive); "
             "empty = all. e.g. 'Ubuntu 22.04,RHEL 9'",
    )
    parser.add_argument(
        "--from-db", action="store_true",
        help="build entries straight from the DB for the --distros selection "
             "(one rep SKU per distro+arch: most deployable, then newest) "
             "instead of reading "
             "Phase 1's delta-only needs_validation.json. Use for a manual "
             "re-validation of specific distros without re-running Phase 1.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="resolve clients + input and report counts, but do not run gates")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    wanted = {d.strip().casefold() for d in args.distros.split(",") if d.strip()}

    try:
        notifier_mod, db_mod = _load_phase1()
    except Exception:
        logger.exception("Could not load Phase 1 helpers (notifier/db_manager)")
        return 2

    if args.from_db:
        # Pull the selected distros straight from the DB (no needs_validation.json).
        # Skip non-deployable / non-standard offers so the representative pick is a
        # plan-free plain "*-server-*" / "*-base" image that deploys without
        # marketplace terms:
        #   advanced-sla -> restricted audience (subscription not entitled)
        #   pro          -> carries a purchase plan (needs terms the MI can't accept)
        #   cvm/confidential -> confidential-compute image (needs special VM size)
        #   minimal      -> stripped image (prefer the full server image)
        #   fips         -> FIPS image (carries a plan)
        # Override via FROMDB_EXCLUDE_OFFERS (comma-separated, case-insensitive).
        excl = tuple(
            s.strip() for s in os.environ.get(
                "FROMDB_EXCLUDE_OFFERS",
                "advanced-sla,pro,cvm,confidential,minimal,fips",
            ).split(",") if s.strip()
        )
        entries = entries_from_db(db_mod, DB_PATH, wanted, exclude_substrings=excl)
        logger.info(
            "From-DB mode: %d entr(ies) for %s (excluding offers/skus matching %s)",
            len(entries), sorted(wanted) if wanted else "ALL distros", list(excl),
        )
    else:
        try:
            entries = load_entries(args.input)
        except (OSError, ValueError) as exc:
            logger.error("Cannot read Phase 1 input %s: %s", args.input, exc)
            return 2
        if wanted:
            before = len(entries)
            entries = [e for e in entries if e.get("distro_label", "").casefold() in wanted]
            logger.info("Distro filter %s: %d -> %d entr(ies)", sorted(wanted), before, len(entries))
        entries = enrich_and_merge(entries, db_mod, DB_PATH)

    if args.dry_run:
        logger.info("Dry run: %d entr(ies)", len(entries))
        return 0

    try:
        run(
            entries=entries,
            notifier_obj=Phase1NotifierAdapter(notifier_mod),
            db=Phase1DbAdapter(db_mod, DB_PATH),
            lisa_jobs_path=args.output,
        )
    except Exception:
        logger.exception("Phase 2 run failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
