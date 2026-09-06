"""Phase 2 orchestration: validate AzNFS coverage against PMC **prod**.

Prod-only design (no PMC API, no tux-dev, no ADO build). For each image handed
over by Phase 1 the orchestrator walks three checks built straight on the public
``packages.microsoft.com`` version-indexed layout:

    Gate 1  repo exists?      GET /<distro>/<version>/prod/ returns 200
              no  -> DB known_unsupported  (reason: "repo is missing")
    Gate 2  package exists?   the aznfs dir lists a 0.3.x build for this arch
              no  -> DB known_unsupported  (email flags pending_publish: publish manually)
    Gate 3  validation needed?  numeric-latest 0.3.x prod version p  vs  DB last_validated_version
              no  (p == v_last) -> DB known_supported  (trusted)
              yes (first time, or p > v_last) -> emit LISA job; DB state UNCHANGED (Phase 3 sets it)

Phase 2 sends EXACTLY ONE e-mail per run: the end-of-run summary, which lists
every distro and -- for the failing ones -- the reason. No per-distro mail is
sent. External effects (prod client, DB, notifier) stay injectable so the flow
is easy to unit-test and to wire into the CLI/workflow layer (see ``run.py``).
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Protocol

from . import pmc_packages
import aznfs_support
import requests

logger = logging.getLogger(__name__)

# Validation states written back to the DB ``validated`` column (mirror db_manager).
KNOWN_SUPPORTED = "known_supported"
KNOWN_UNSUPPORTED = "known_unsupported"
# Phase 2 owns cheap repo/package verdicts; Phase 3 owns VM-tested ones.
GATE_VERDICT = "gate"
PENDING_PUBLISH = "pending_publish"


class ProdLike(Protocol):
    """The PMC-prod read surface the gates need (see pmc_packages.ProdPackageIndex)."""
    def resolve_repo(self, distro: str, candidates: list[str], family: str = "") -> str | None: ...
    def list_packages(self, distro: str, version: str, family: str) -> list[str]: ...


class DbLike(Protocol):
    def set_validation_state(
        self,
        identity: tuple[str, str, str, str, str],
        state: str,
        reason: str | None = None,
        verdict_source: str | None = None,
    ) -> None: ...

    def mark_probe_failed(self, identity: tuple[str, str, str, str, str]) -> None: ...


class NotifierLike(Protocol):
    """Phase 2 emits a single end-of-run summary; there are no per-distro mails.

    Each bucket is a list of small dicts so the summary e-mail renders one table
    per outcome, one column per field:
      * to_phase3       -> {"label", "arch", "url"}
      * trusted         -> {"label", "arch"}
      * pending_publish -> {"label", "arch", "reason"}
      * unsupported     -> {"label", "arch", "reason"}
    ``errors`` stays a list of ``(label, reason)`` tuples.
    """
    def notify_summary(
        self,
        processed: int,
        to_phase3: list[dict],
        trusted: list[dict],
        pending_publish: list[dict],
        unsupported: list[dict],
        errors: list[tuple[str, str]],
    ) -> None: ...


@dataclass
class Phase2Result:
    outcome: str  # known_unsupported | pending_publish | trusted | to_phase3
    reason: str = ""
    lisa_job: dict | None = None


@dataclass
class GateResult:
    passed: bool
    reason: str = ""
    details: str = ""
    segment: str | None = None
    resolved_version: str | None = None


def _identity(entry: dict) -> tuple[str, str, str, str, str]:
    return (
        entry.get("publisher", ""),
        entry.get("image") or entry.get("offer") or "",
        entry.get("sku", ""),
        entry.get("region", ""),
        entry.get("architecture") or entry.get("arch") or "",
    )


_AZNFS_PACKAGES_CSV_URL = (
    "https://raw.githubusercontent.com/Azure/AZNFS-mount/main/packages.csv"
)


def _major_minor(label: str) -> tuple[str, str]:
    return aznfs_support.major_minor(label)


def _is_aznfs_supported_distro(label: str) -> bool:
    return aznfs_support.is_supported_distro(label)


def _packages_csv_mentions_distro(label: str) -> bool:
    """Best-effort check whether AZNFS-mount/packages.csv has this distro family."""
    tokens = []
    s = (label or "").strip().lower()
    major, minor = _major_minor(s)

    if "ubuntu" in s and major and minor:
        tokens.extend([f"ubuntu {major}.{minor}", f"ubuntu-{major}.{minor}"])
    elif "rhel" in s and major:
        tokens.extend([f"rhel {major}", f"rhel-{major}", f"redhat {major}"])
    elif "rocky" in s and major:
        tokens.extend([f"rocky {major}", f"rocky-{major}"])
    elif "sles" in s and major:
        tokens.extend([f"sles {major}", f"sles-{major}"])
    elif "debian" in s and major:
        tokens.extend([f"debian {major}", f"debian-{major}"])
    elif "azure linux" in s:
        tokens.extend(["azure linux", "azurelinux"])
    elif "mariner" in s:
        tokens.extend(["cbl-mariner", "mariner"])

    if not tokens:
        return False

    try:
        resp = requests.get(_AZNFS_PACKAGES_CSV_URL, timeout=20)
        resp.raise_for_status()
    except requests.RequestException:
        return False

    text = resp.text.lower()
    if any(tok in text for tok in tokens):
        return True

    # Fallback to CSV cell scan in case formatting changes.
    reader = csv.reader(io.StringIO(resp.text))
    for row in reader:
        row_txt = " ".join(c.strip().lower() for c in row)
        if any(tok in row_txt for tok in tokens):
            return True
    return False


# ---------------------------------------------------------------------------
# Gate 1: does a prod repo exist for this distro release?
# ---------------------------------------------------------------------------
_UNKNOWN_PUBLISH_TIME = datetime.min.replace(tzinfo=timezone.utc)


def _publish_key(prod: ProdLike, segment: str, version: str, family: str,
                 name: str) -> tuple:
    """Sort key for "which package is latest": publication time, then version.

    PMC version numbers are NOT publication-sequential -- 0.3.49 shipped after
    0.3.459 -- so the autoindex timestamp is authoritative and the version only
    breaks ties. Clients without timestamps (test fakes, an index that omits the
    date column) fall back to version order, which is the previous behaviour.
    """
    published_at = getattr(prod, "published_at", None)
    when = published_at(segment, version, family, name) if callable(published_at) else None
    return (when or _UNKNOWN_PUBLISH_TIME,
            pmc_packages.version_tuple(pmc_packages.version_from_filename(name)))


def _newest_package(prod: ProdLike, segment: str, version: str, family: str,
                    want_arch: str) -> tuple:
    """Sort key of the latest-published in-series aznfs package in one pocket.

    Best-effort: a listing failure here scores the pocket as empty rather than
    failing the image, so a broken /rhel/8.0/ cannot take down an image whose
    /rhel/8/ is healthy. Gate 2 lists the pocket that wins and does surface a
    failure there, so a verdict is never recorded off a listing that errored.
    """
    try:
        files = prod.list_packages(segment, version, family)
    except Exception as exc:  # noqa: BLE001 - any listing failure means "unknown"
        logger.warning("Could not list /%s/%s/ while choosing a pocket: %s",
                       segment, version, exc)
        return ()

    keys = [_publish_key(prod, segment, version, family, name)
            for name in files
            if pmc_packages.file_arch(name, family) == want_arch
            and pmc_packages.in_series(pmc_packages.version_from_filename(name))]
    return max(keys, default=())


def gate1_repo_exists(entry: dict, prod: ProdLike) -> GateResult:
    """A PMC prod pocket exists for this image's distro release.

    Resolves the ``<distro>`` segment + ``<version>`` candidates from the image's
    ``distro_label`` (no codename map) and probes ``/<distro>/<version>/prod/``.

    PMC serves an x.0 release at two paths (rhel/8 and rhel/8.0), and they are
    not guaranteed to be in step, so when both exist the one carrying the newer
    aznfs package wins -- that pocket is what Gate 2 counts and what Phase 3
    installs from. Ties keep candidate order.
    """
    label = entry.get("distro_label", "")
    family = entry.get("family") or ""
    segment = pmc_packages.distro_segment(label, entry.get("publisher", ""))
    if not segment:
        return GateResult(False, "unmapped distro", details=label or entry.get("publisher", ""))

    candidates = pmc_packages.version_candidates(label, entry.get("version", ""))
    if not candidates:
        return GateResult(False, "unparseable version", details=f"{label!r}")

    # Two candidates are always the x / x.0 pair: one release served at two
    # paths that can drift, so both are probed and the newer one wins.
    existing = [v for v in candidates if prod.resolve_repo(segment, [v], family)]
    if not existing:
        return GateResult(False, "prod repo missing", details=f"{segment} {candidates}")

    resolved = existing[0]
    if len(existing) > 1:
        arch = entry.get("architecture") or entry.get("arch") or ""
        want_arch = pmc_packages.normalize_arch(arch, family)
        resolved = max(existing,
                       key=lambda v: _newest_package(prod, segment, v, family, want_arch))
        logger.info("[%s] %s pockets %s both exist -> using /%s/%s/ (newest package)",
                    label or "?", segment, existing, segment, resolved)
    return GateResult(True, segment=segment, resolved_version=resolved)


# ---------------------------------------------------------------------------
# LISA job (Phase 3 hand-off)
# ---------------------------------------------------------------------------
def _make_lisa_job(entry: dict, distro: str, version: str, family: str,
                   package_filename: str, aznfs_version: str) -> dict:
    """Assemble the Phase 3 LISA job for a prod-published package needing validation.

    The field names match Phase 3's ``LisaJob`` dataclass EXACTLY so Phase 3's
    ``load_jobs`` consumes this artifact directly (it keeps only known fields):
    ``publisher / image / sku / version / region / arch`` identify the
    marketplace image + DB row, and ``aznfs_package_url / aznfs_version`` are the
    published package Phase 3 installs and asserts. ``distro_label`` is carried
    through for human-readable reporting.
    """
    download_url = pmc_packages.aznfs_dir_url(distro, version, family) + package_filename
    return {
        "publisher": entry.get("publisher"),
        "image": entry.get("image") or entry.get("offer"),
        "sku": entry.get("sku"),
        "version": entry.get("version"),
        "region": entry.get("region"),
        "arch": entry.get("architecture") or entry.get("arch"),
        "distro_label": entry.get("distro_label"),
        "aznfs_package_url": download_url,
        "aznfs_version": aznfs_version,
    }


def write_lisa_jobs(jobs: list[dict], path: str) -> None:
    """Persist the run's LISA jobs as the Phase 3 hand-off artifact."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(jobs, fh, indent=2)
    logger.info("Wrote %d LISA job(s) -> %s", len(jobs), path)


# ---------------------------------------------------------------------------
# Image-drift re-validation (rate-limited): re-check a supported distro when its
# marketplace image has changed AND enough time has passed, so OS rebuilds get
# re-validated without re-running on every (often daily) image bump.
# ---------------------------------------------------------------------------
def _image_revalidate_days() -> int:
    """Days before a changed marketplace image triggers re-validation (env-tunable, 0 = off)."""
    try:
        return int((os.environ.get("PHASE3_IMAGE_REVALIDATE_DAYS", "") or "15").strip())
    except ValueError:
        return 15


def _image_needs_revalidation(entry: dict) -> bool:
    """True when the distro's marketplace image changed since its last validation
    AND at least PHASE3_IMAGE_REVALIDATE_DAYS have elapsed since then."""
    days = _image_revalidate_days()
    if days <= 0:
        return False
    v_img_last = (entry.get("last_validated_image_version") or "").strip()
    if not v_img_last:
        return False  # never validated an image yet -> the first-time path handles it
    cur_img = (entry.get("version") or "").strip()
    if not cur_img or cur_img == v_img_last:
        return False  # image unchanged
    last = (entry.get("last_validated") or "").strip()
    if not last:
        return True  # image changed and no timestamp -> allow
    try:
        dt = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return True  # unparseable timestamp -> allow
    return datetime.now(timezone.utc) - dt >= timedelta(days=days)


# ---------------------------------------------------------------------------
# Per-image flow
# ---------------------------------------------------------------------------
def process_entry(entry: dict, prod: ProdLike, db: DbLike) -> Phase2Result:
    """Run the three prod checks for one image and apply the DB side-effect.

    Returns the per-image outcome + reason; the caller (:func:`run_phase2`)
    rolls every result into the single end-of-run summary e-mail. No per-distro
    notification is sent here.
    """
    ident = _identity(entry)
    family = entry.get("family") or ""
    arch = (entry.get("architecture") or entry.get("arch") or "").lower()
    label = entry.get("distro_label", "")
    logger.info("[%s] Phase 2 start (family=%s, arch=%s)", label or "?", family or "?", arch or "?")

    # A re-check of an already-validated distro must never DOWNGRADE it: a
    # transient prod hiccup (repo momentarily unlistable, package index blip)
    # would otherwise flip a good distro to known_unsupported -- and, being
    # terminal, it would then be skipped forever. So when the row is
    # known_supported, Gate 1/2 failures keep it as-is; only Gate 3 finding a
    # NEWER package re-validates it. The supported state arrives under different
    # keys per input path: ``_db_state`` (enrich re-feed), ``validated``
    # (--from-db rows), ``validation_status`` (Phase 1 artifact) -- honour all.
    protect_supported = KNOWN_SUPPORTED in (
        entry.get("_db_state"), entry.get("validated"), entry.get("validation_status")
    )

    # Gate 1: prod repo exists?
    g1 = gate1_repo_exists(entry, prod)
    if not g1.passed:
        if protect_supported:
            logger.info("[%s] re-check: prod repo not resolvable now -> keeping known_supported (no downgrade)",
                        label or "?")
            return Phase2Result("trusted", reason="re-check skipped (prod repo not resolvable); kept known_supported")
        reason = "prod repo is missing"
        logger.info("[%s] Gate 1 FAIL (%s: %s) -> known_unsupported",
                    label or "?", g1.reason, g1.details)
        db.set_validation_state(ident, KNOWN_UNSUPPORTED, reason=reason, verdict_source=GATE_VERDICT)
        return Phase2Result("known_unsupported", reason=reason)

    distro, version = g1.segment, g1.resolved_version
    logger.info("[%s] Gate 1 PASS -> prod repo /%s/%s/prod/", label or "?", distro, version)

    # Gate 2: is an aznfs package for this arch published in the tracked 0.3.x series?
    want_arch = pmc_packages.normalize_arch(arch, family)
    files = prod.list_packages(distro, version, family)
    arch_files = [
        f for f in files
        if pmc_packages.file_arch(f, family) == want_arch
        and pmc_packages.in_series(pmc_packages.version_from_filename(f))
    ]
    logger.info("[%s] Gate 2: %d package file(s) listed, %d match arch=%s in %s.x series",
                label or "?", len(files), len(arch_files), want_arch, pmc_packages.AZNFS_SERIES)
    if not arch_files:
        label = entry.get("distro_label", "")
        if protect_supported:
            logger.info("[%s] re-check: no newer %s.x package on prod (arch=%s) -> keeping known_supported",
                        label or "?", pmc_packages.AZNFS_SERIES, want_arch)
            return Phase2Result("trusted", reason="re-check: no newer package on prod; kept known_supported")
        # (a) the distro is outside the AzNFS support matrix -> terminal.
        if not _is_aznfs_supported_distro(label):
            reason = "repo is found but packages are not found because distro is not supported by AzNFS"
            logger.info("[%s] Gate 2 FAIL: no %s.x package (arch=%s) + distro NOT in support set -> known_unsupported",
                        label or "?", pmc_packages.AZNFS_SERIES, want_arch)
            db.set_validation_state(ident, KNOWN_UNSUPPORTED, reason=reason, verdict_source=GATE_VERDICT)
            return Phase2Result("known_unsupported", reason=reason)
        # (b) supported distro already listed in AZNFS-mount/packages.csv -> the
        # csv does not need a change; a human just needs to publish the package.
        # validation_state stays known_unsupported (only 3 states are used:
        # known_supported / known_unsupported / unknown); the email still flags
        # it as pending_publish so a human knows to publish + re-invoke.
        if _packages_csv_mentions_distro(label):
            reason = (
                "no AzNFS packages found on prod and packages.csv does not "
                "require modification; publish packages manually and re-invoke Phase 2"
            )
            logger.info("[%s] Gate 2 FAIL: no package but distro IS in packages.csv -> pending_publish (e-mail flag; DB known_unsupported)",
                        label or "?")
            db.set_validation_state(ident, KNOWN_UNSUPPORTED, reason=reason, verdict_source=GATE_VERDICT)
            return Phase2Result("pending_publish", reason=reason)
        # (c) supported distro MISSING from packages.csv -> needs a csv/code
        # change first; mark known_unsupported until that branch is built.
        reason = "team must update packages.csv + push branch + re-invoke Phase 2 with the new branch"
        logger.info("[%s] Gate 2 FAIL: no package + distro MISSING from packages.csv -> known_unsupported",
                    label or "?")
        db.set_validation_state(ident, KNOWN_UNSUPPORTED, reason=reason, verdict_source=GATE_VERDICT)
        return Phase2Result("known_unsupported", reason=reason)

    logger.info("[%s] Gate 2 PASS -> %d %s.x package(s) published for arch=%s",
                label or "?", len(arch_files), pmc_packages.AZNFS_SERIES, want_arch)

    # Gate 3: validation needed? Latest-PUBLISHED 0.3.x package vs what Phase 3
    # last validated. Publication order, not version order: 0.3.49 shipped after
    # 0.3.459, so a numeric max would keep validating a superseded build.
    best = max(arch_files,
               key=lambda f: _publish_key(prod, distro, version, family, f))
    p = pmc_packages.version_from_filename(best)
    v_last = (entry.get("last_validated_version") or "").strip()
    v_regressed = (entry.get("last_regressed_version") or "").strip()

    # A CHANGED package needs validating, not just a numerically greater one --
    # the newest publication can carry a lower version than the last validated.
    is_newer = (not v_last) or p != v_last
    # A version already known to regress on THIS supported distro is NOT re-tested
    # (it would just fail + re-alert every run); a strictly newer package supersedes
    # the marker and IS validated -> auto-recovery. Gate this on the supported state
    # so a RESET row that still carries a stale marker is NOT trusted into
    # known_supported without a LISA run.
    known_bad = protect_supported and bool(v_regressed) and (
        pmc_packages.version_tuple(p) == pmc_packages.version_tuple(v_regressed)
    )
    # Even when the package is unchanged, re-validate a supported distro whose
    # marketplace IMAGE has drifted (rate-limited) so OS rebuilds get re-checked.
    image_drift = _image_needs_revalidation(entry)
    if known_bad or (not is_newer and not image_drift):
        cur_img = (entry.get("version") or "").strip()
        v_img_last = (entry.get("last_validated_image_version") or "").strip()
        if known_bad:
            why = f"known regression v{p}, awaiting a newer fix"
        elif v_img_last and cur_img and cur_img != v_img_last:
            why = (f"prod v{p} == last-validated v{v_last}; image changed "
                   f"({v_img_last} -> {cur_img}) but re-validation not due yet")
        else:
            why = f"prod v{p} == last-validated v{v_last}, image unchanged"
        logger.info("[%s] Gate 3: %s -> known_supported (trusted, no LISA run)", label or "?", why)
        db.set_validation_state(ident, KNOWN_SUPPORTED, reason="", verdict_source=GATE_VERDICT)
        return Phase2Result("trusted", reason=f"no re-validation needed (v{p})")

    lisa_job = _make_lisa_job(entry, distro, version, family, best, p)
    # Emit the LISA job WITHOUT changing validation_state: it stays whatever it
    # was (unknown for a fresh distro). Phase 3 sets known_supported/-unsupported
    # on its verdict. Only 3 states ever persist.
    if not v_last:
        detail = "first validation"
    elif is_newer:
        detail = f"newer than last-validated v{v_last}"
    else:
        detail = f"image drift (pkg v{p} unchanged, new image {entry.get('version')})"
    logger.info("[%s] Gate 3: %s -> emit LISA job (hand off to Phase 3)", label or "?", detail)
    reason = f"validate v{p} ({detail})"
    return Phase2Result("to_phase3", reason=reason, lisa_job=lisa_job)


def _dedup_jobs_by_url(jobs: list[dict]) -> list[dict]:
    """One LISA job per distinct ``aznfs_package_url``, keeping the newest image.

    Many marketplace SKUs of the same OS release (e.g. RHEL 9.0 .. 9.8, or
    Rocky 8.x) all resolve to the SAME prod package URL (rhel/9, rocky/8,
    ...). Phase 3 only needs to validate that package once per architecture, so
    collapse them to the entry with the latest marketplace ``version`` -- a
    deterministic pick that also validates the freshest image. The result is a
    list whose ``aznfs_package_url`` values are all distinct.
    """
    best: dict[str, dict] = {}
    for j in jobs:
        url = j.get("aznfs_package_url", "")
        cur = best.get(url)
        if cur is None or pmc_packages.version_tuple(j.get("version", "")) > pmc_packages.version_tuple(cur.get("version", "")):
            best[url] = j
    return sorted(best.values(), key=lambda j: (j.get("distro_label") or "", j.get("arch") or ""))


def _dedup_label_arch(rows: list[dict]) -> list[dict]:
    """Collapse rows to one per (distro_label, arch), keeping the first reason.

    A distro that shows up under many SKUs of the same release+architecture
    should appear ONCE in the summary; different architectures stay separate
    rows (that is why the tables carry an ``arch`` column).
    """
    seen: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r.get("label") or "", r.get("arch") or "")
        if key not in seen:
            seen[key] = r
    return sorted(seen.values(), key=lambda r: (r.get("label") or "", r.get("arch") or ""))


def run_phase2(
    entries: list[dict],
    prod: ProdLike,
    db: DbLike,
    notifier: NotifierLike,
    lisa_jobs_path: str | None = None,
) -> list[dict]:
    """Process every image, write the Phase 3 hand-off, and send the single summary.

    The DB side-effect runs once per input image (each SKU row gets its state),
    but the LISA hand-off and the summary tables are de-duplicated: the jobs to
    one entry per distinct prod package URL (latest image wins) and the report
    buckets to one row per (distro_label, architecture).
    """
    raw_jobs: list[dict] = []
    unsupported: list[dict] = []
    pending_publish: list[dict] = []
    trusted: list[dict] = []
    errors: list[tuple[str, str]] = []

    for e in entries:
        label = e.get("distro_label", "?")
        arch = e.get("architecture") or e.get("arch") or ""
        try:
            result = process_entry(e, prod, db)
        except pmc_packages.ProbeError as exc:
            # PMC unreachable proves nothing: leave the stored verdict alone and
            # only flag the row, so exactly these are retried on the next run.
            logger.warning("PMC unreachable while checking %s: %s", label, exc)
            try:
                db.mark_probe_failed(_identity(e))
            except Exception:  # pragma: no cover - marking is best-effort
                logger.exception("Could not flag %s for retry", label)
            errors.append((label, f"PMC unreachable, verdict left unchanged: {exc}"))
            continue
        except Exception as exc:  # one image's failure never aborts the run
            logger.exception("Unexpected error processing %s", label)
            errors.append((label, f"orchestrator error (will retry next run): {exc}"))
            continue

        if result.outcome == "known_unsupported":
            unsupported.append({"label": label, "arch": arch, "reason": result.reason})
        elif result.outcome == "pending_publish":
            pending_publish.append({"label": label, "arch": arch, "reason": result.reason})
        elif result.outcome == "trusted":
            trusted.append({"label": label, "arch": arch})
        elif result.lisa_job:  # to_phase3
            raw_jobs.append(result.lisa_job)

    lisa_jobs = _dedup_jobs_by_url(raw_jobs)
    to_phase3 = [
        {"label": j.get("distro_label"), "arch": j.get("arch"), "url": j.get("aznfs_package_url")}
        for j in lisa_jobs
    ]
    trusted = _dedup_label_arch(trusted)
    pending_publish = _dedup_label_arch(pending_publish)
    unsupported = _dedup_label_arch(unsupported)

    if lisa_jobs_path:
        write_lisa_jobs(lisa_jobs, lisa_jobs_path)
    # Like Phase 1 (which stays silent when no new distro is found), only send
    # the summary e-mail when there is something ACTIONABLE to report. The daily
    # known_supported re-check produces a big ``trusted`` bucket every run, so
    # trusted-only (nothing newer shipped) is NOT worth an e-mail -- it would be
    # daily noise. Mail only on to_phase3 / pending_publish / unsupported / errors.
    if to_phase3 or pending_publish or unsupported or errors:
        notifier.notify_summary(
            processed=len(entries),
            to_phase3=to_phase3,
            trusted=trusted,
            pending_publish=pending_publish,
            unsupported=unsupported,
            errors=errors,
        )
    else:
        logger.info(
            "Phase 2: nothing actionable (only trusted / nothing newer); "
            "skipping summary e-mail."
        )
    logger.info(
        "Phase 2: %d processed | %d to-phase3 | %d trusted | %d pending-publish | %d known_unsupported | %d errors",
        len(entries), len(to_phase3), len(trusted), len(pending_publish), len(unsupported), len(errors),
    )
    return lisa_jobs

