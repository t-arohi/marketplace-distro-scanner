#!/usr/bin/env python3
"""
scan_marketplace.py ΓÇö Phase 1 entry point.

Workflow
--------
1. Ensure the local SQLite database exists (create from schema if not).
2. Authenticate to Azure and build a Compute client.
3. For every configured region ├ù publisher, crawl offers ΓåÆ SKUs ΓåÆ versions.
4. For each version:
     - If the (publisher, offer, sku, version, region) tuple is NOT in the DB
       ΓåÆ insert it as validated='unknown' and flag it for the output JSON.
     - If it IS in the DB ΓåÆ update last_checked only; no output.
5. Write all flagged images to output/needs_validation.json.
6. Exit with code 0 when there are no new images, or 1 when new images were found
   so that the GitHub Actions step can branch on the result.

Running locally
---------------
  export AZURE_SUBSCRIPTION_ID=<your-sub-id>
  az login          # DefaultAzureCredential picks this up
  python scan_marketplace.py
"""

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone

import config
import azure_client
import aznfs_support
import db_manager
import notifier
import status_rollup
from status_rollup import buckets_by_state

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Derivations
# ---------------------------------------------------------------------------

def derive_family_and_distro_label(
    publisher: str, offer: str, sku: str
) -> tuple[str, str]:
    """Derive package family and human-readable distro label from image metadata."""
    p = (publisher or "").strip().lower()
    o = (offer or "").strip().lower()
    s = (sku or "").strip().lower()

    # Ubuntu family
    if p == "canonical" and "ubuntu" in o:
        # Ubuntu Core is a distinct product line (e.g. ubuntu-core-24-private).
        if "ubuntu-core" in o:
            match = re.search(r"ubuntu-core-(\d+)", o)
            if match:
                return "apt", f"Ubuntu Core {match.group(1)}"
            return "apt", "Ubuntu Core"

        # Releases are encoded as NN_NN or NN.NN in the offer (e.g.
        # ubuntu-22_04-lts, ubuntu-pro-26_04-lts) or the SKU (e.g. 16.04-LTS).
        match = re.search(r"(\d{2})[._](\d{2})", o) or re.search(r"(\d{2})[._](\d{2})", s)
        if match:
            return "apt", f"Ubuntu {match.group(1)}.{match.group(2)}"

        # Older / special offers use the release codename (e.g.
        # 0001-com-ubuntu-confidential-vm-focal).
        codename_to_version = {
            "xenial": "16.04",
            "bionic": "18.04",
            "focal": "20.04",
            "groovy": "20.10",
            "hirsute": "21.04",
            "impish": "21.10",
            "jammy": "22.04",
            "kinetic": "22.10",
            "lunar": "23.04",
            "mantic": "23.10",
            "noble": "24.04",
            "questing": "25.10",
            "resolute": "26.04",
        }
        for codename, version in codename_to_version.items():
            if codename in o:
                return "apt", f"Ubuntu {version}"

        return "apt", "Ubuntu"

    # Debian family
    if p == "debian" or "debian" in o:
        match = re.search(r"debian-(\d+)", o) or re.search(r"^(\d+)", s)
        if match:
            return "apt", f"Debian {match.group(1)}"
        return "apt", "Debian"

    # Red Hat family
    if p == "redhat" or o == "rhel" or "rhel" in o:
        # Some offers/SKUs embed the major right after 'rhel' (offer 'rh-rhel',
        # sku 'rh-rhel9' / 'rh-rhel9-arm64'); match that first so it does not
        # fall through to a bare 'RHEL'. Otherwise SKUs encode the release as
        # major[.minor] with assorted separators or none at all, e.g.
        # 9-lvm-gen2 -> 9, 8_2 -> 8.2, 7.9 -> 7.9, 90-gen2 -> 9.0, 810 -> 8.10.
        match = (
            re.search(r"rhel[-_]?(10|\d)(?:[._-]?(\d{1,2}))?", s)
            or re.search(r"rhel[-_]?(10|\d)(?:[._-]?(\d{1,2}))?", o)
            or re.match(r"(10|\d)[._-]?(\d{1,2})?", s)
        )
        if match:
            major, minor = match.group(1), match.group(2)
            if minor:
                return "yum", f"RHEL {major}.{minor}"
            return "yum", f"RHEL {major}"
        return "yum", "RHEL"

    # SUSE family
    if p == "suse" or "sles" in o or "suse" in o or "opensuse" in o:
        match = re.search(r"(?:sles[-_]?)(\d+)", o) or re.search(r"^(\d+)", s)
        if match:
            return "yum", f"SLES {match.group(1)}"
        if "opensuse" in o:
            return "yum", "openSUSE"
        return "yum", "SUSE Linux"

    # Rocky Linux (commonly under RESF publisher in Marketplace).
    if "rocky" in o or "rocky" in s or p == "resf":
        # The major may be in the offer (rockylinux-x86_64 + sku '8-base') or
        # buried in the sku after the arch token (offer 'rockylinux-aarch64',
        # sku 'rockylinux-aarch64-8' / 'rockylinux-aarch64-9-lvm'). Strip the
        # arch / gen tokens first, then take the version digit.
        cleaned = re.sub(r"(aarch64|x86_64|arm64|gen\d)", " ", f"{o} {s}")
        match = re.search(r"rocky\w*[-_ ]*(\d+)", cleaned) or re.search(r"(\d+)", cleaned)
        if match:
            return "yum", f"Rocky {match.group(1)}"
        return "yum", "Rocky"

    # Azure Linux / CBL-Mariner family (Microsoft's own distro).
    # Uses RPM + tdnf, so it shares the "yum" repo family for Phase 2.
    if (
        p == "microsoftcblmariner"
        or "azure-linux" in o or "azurelinux" in o
        or "cbl-mariner" in o or "mariner" in o
    ):
        match = re.search(r"azure-?linux-?(\d+)", o) or re.search(r"azure-?linux-?(\d+)", s)
        if match:
            return "yum", f"Azure Linux {match.group(1)}"
        match = (
            re.search(r"cbl-?mariner-?(\d+)", o)
            or re.search(r"cbl-?mariner-?(\d+)", s)
            or re.match(r"(\d+)", s)
        )
        if match:
            return "yum", f"CBL-Mariner {match.group(1)}"
        return "yum", "Azure Linux"

    return "unknown", "Unknown"


def rollup_by_distro(images: list[dict]) -> list[dict]:
    """Collapse SKU-level rows to the unit AzNFS actually validates: a distro release.

    SKU, version, region, architecture and offer are *not* part of a distro's
    identity ΓÇö many marketplace offers/SKUs ship the same OS release ΓÇö so they
    are folded away here, leaving one entry per (family, distro_label).
    Architecture and the contributing publishers/offers are kept as aggregated
    details so no information is lost.
    """
    groups: dict[tuple[str, str], dict] = {}
    for img in images:
        key = (img.get("family", ""), img.get("distro_label", ""))
        g = groups.get(key)
        if g is None:
            g = {
                "family": key[0],
                "distro_label": key[1],
                "publishers": set(),
                "architectures": set(),
                "offers": set(),
                "sku_count": 0,
            }
            groups[key] = g
        if img.get("publisher"):
            g["publishers"].add(img["publisher"])
        if img.get("architecture"):
            g["architectures"].add(img["architecture"])
        if img.get("image"):
            g["offers"].add(img["image"])
        g["sku_count"] += 1

    rollup = [
        {
            "family": g["family"],
            "distro_label": g["distro_label"],
            "publishers": sorted(g["publishers"]),
            "architectures": sorted(g["architectures"]),
            "offer_count": len(g["offers"]),
            "sku_count": g["sku_count"],
        }
        for g in groups.values()
    ]
    rollup.sort(key=lambda r: (r["family"], r["distro_label"]))
    return rollup


def _exclude_distros(records: list[dict]) -> list[dict]:
    """Drop records whose distro_label matches config.EXCLUDED_DISTRO_PREFIXES."""
    return status_rollup.exclude_distros(records, config.EXCLUDED_DISTRO_PREFIXES)


def dedup_backlog(records: list[dict]) -> list[dict]:
    """Pick one representative SKU per (distro_label, architecture) from a backlog.

    Phase 2 probes PMC prod once per distro release + architecture, and Phase 3
    provisions one VM per emitted job, so handing over every still-unknown SKU
    row (there can be ~1k for ~50 releases) would multiply prod HTTP checks and
    spin up duplicate VMs for the same release. Collapsing to a single
    representative per (distro_label, architecture) keeps a concrete marketplace
    image for Phase 3 while bounding the work to roughly one job per
    release/arch. The pick is the most deployable SKU, then the newest version,
    then name order -- see ``aznfs_support.is_preferred_image``. Deployability
    leads because an image whose plan we cannot accept yields no result at all,
    so an older SKU that boots beats a newer one that does not.
    """
    chosen: dict[tuple[str, str], dict] = {}
    for r in records:
        # Skip restricted offers (e.g. advanced-sla) the subscription cannot
        # deploy -- mirrors the marketplace-scan filter so a re-armed backlog
        # never re-emits a non-deployable image.
        haystack = (r.get("image", "") + " " + r.get("sku", "")).lower()
        if any(p in haystack for p in config.EXCLUDED_OFFER_SUBSTRINGS):
            continue
        key = (r.get("distro_label", ""), r.get("architecture", ""))
        cur = chosen.get(key)
        if cur is None or aznfs_support.is_preferred_image(
                r, cur, db_manager.version_tuple):
            chosen[key] = r
    return sorted(
        chosen.values(),
        key=lambda r: (
            r.get("family", ""),
            r.get("distro_label", ""),
            r.get("architecture", ""),
        ),
    )


def format_phase2_input(records: list[dict]) -> list[dict]:
    """Prepare Phase 2 input rows with stable fields.

    Keep all existing fields used by Phase 2, but rename ``validated`` to
    ``validation_status`` in the JSON artifact.

    This is also where the AzNFS support matrix is enforced: every byte Phase 2
    receives is built here, so filtering at this one point means Phase 2 can
    simply validate whatever it is handed. Releases AzNFS does not publish for
    are still scanned, stored and reported -- they are just never handed over,
    because a missing package there is expected rather than a finding.
    """
    out: list[dict] = []
    skipped = 0
    for r in records:
        if not aznfs_support.is_supported_distro(r.get("distro_label", "")):
            skipped += 1
            continue
        row = dict(r)
        status = row.pop("validated", "")
        row["validation_status"] = status
        out.append(row)
    if skipped:
        logger.info("Phase 2 hand-off: %d row(s) held back, %s",
                    skipped, aznfs_support.OUT_OF_MATRIX_REASON)
    return out


def write_step_summary(rollup: list[dict], total_tracked: int) -> None:
    """Render the cut-down distro list into the GitHub Actions run summary.

    One row per tracked OS release still awaiting validation (SKU / version /
    region / architecture collapsed to counts) -- the same view the team reads
    after every daily scan. No-op when ``GITHUB_STEP_SUMMARY`` is unset (local
    runs); failures are swallowed so they never affect the scan's exit code.
    """
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    try:
        lines: list[str] = []
        if not rollup:
            lines.append(
                f"## Marketplace scan: 0 distro release(s) awaiting validation "
                f"({total_tracked} SKU row(s) tracked)\n"
            )
        else:
            lines.append(f"## {len(rollup)} distro release(s) tracked (cut-down list)\n")
            lines.append(
                "_One row per OS release \u2014 SKU/version/region/architecture "
                "collapsed to counts._\n"
            )
            lines.append("| Family | Distro Label | Publishers | Architectures | # SKUs |")
            lines.append("|---|---|---|---|---|")
            for d in rollup:
                pubs = ", ".join(d.get("publishers", []))
                arch = ", ".join(d.get("architectures", []))
                lines.append(
                    f"| {d.get('family','')}"
                    f" | {d.get('distro_label','')}"
                    f" | {pubs}"
                    f" | {arch}"
                    f" | {d.get('sku_count','')} |"
                )
            lines.append(
                "\n_Phase 2 consumes `output/needs_validation.json`; e-mail for "
                "**new** releases is sent in-process via ACS + Managed Identity._"
            )
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception as exc:  # never let a reporting glitch fail the scan
        logger.warning("Could not write step summary: %s", exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    # ------------------------------------------------------------------
    # Step 1 ΓÇö Initialise database
    # ------------------------------------------------------------------
    logger.info("Initialising database at: %s", config.DB_PATH)
    db_manager.initialize(config.DB_PATH, config.SCHEMA_PATH)

    # ------------------------------------------------------------------
    # Step 2 ΓÇö Azure client
    # ------------------------------------------------------------------
    logger.info("Building Azure Compute client ΓÇª")
    client = azure_client.get_compute_client()

    # ------------------------------------------------------------------
    # Step 3+4 ΓÇö Scan and compare
    # ------------------------------------------------------------------
    # Snapshot the distro releases already known, so that after the scan we can
    # report only *new* OS releases (the cut-down list), not per-SKU churn.
    known_distros_before = db_manager.distinct_distro_labels(config.DB_PATH)

    new_images: list[dict] = []
    updated_images: list[dict] = []

    for region in config.REGIONS:
        logger.info("=== Region: %s ===", region)

        for publisher in config.PUBLISHERS:
            logger.info("  Publisher: %s", publisher)
            offers = azure_client.list_offers(client, region, publisher)

            # None => the SDK call failed (see the warning it logged); [] => the
            # publisher genuinely has no offers. Keep them distinct so an auth /
            # token error is never mistaken for "nothing to scan".
            if offers is None:
                logger.error(
                    "    Listing offers FAILED for region=%s publisher=%s "
                    "(see warning above) — skipping this publisher.",
                    region, publisher,
                )
                continue
            if not offers:
                logger.info("    No offers found — skipping.")
                continue

            # Per-publisher tallies so each publisher's contribution (and the
            # unchanged denominator) is visible at INFO, not just the raw stream.
            p_offers = p_skus = p_new = p_updated = p_unchanged = 0

            for offer in offers:
                # Skip PRIVATE / restricted-audience offers the subscription
                # cannot deploy (e.g. Canonical "Pro - Advanced SLA"). They show
                # up in the listing but fail Phase 3 provisioning with
                # "Offer ... not found / restricted audience"; the same releases
                # exist in public offers, so dropping these keeps coverage.
                offer_l = (offer or "").lower()
                if any(p in offer_l for p in config.EXCLUDED_OFFER_SUBSTRINGS):
                    logger.info("    Skipping restricted offer: %s", offer)
                    continue

                p_offers += 1
                skus = azure_client.list_skus(client, region, publisher, offer)

                for sku in skus:
                    versions = azure_client.list_versions(
                        client, region, publisher, offer, sku
                    )
                    if not versions:
                        continue

                    p_skus += 1
                    # Dedup: one row per (publisher, image, sku, region, arch).
                    # Marketplace versions sort lexicographically (date-style).
                    latest = max(versions)
                    architecture = azure_client.get_image_architecture(
                        client, region, publisher, offer, sku, latest
                    )
                    family, distro_label = derive_family_and_distro_label(
                        publisher, offer, sku
                    )

                    status = db_manager.check_and_upsert(
                        config.DB_PATH,
                        publisher, offer, sku, latest, region,
                        architecture, family, distro_label,
                    )
                    if status == db_manager.UNCHANGED:
                        p_unchanged += 1
                        logger.debug(
                            "    unchanged: %s / %s / %s %s (%s)",
                            publisher, offer, sku, latest, distro_label,
                        )
                        continue

                    record = db_manager.get_image_record(
                        config.DB_PATH, publisher, offer, sku, region, architecture,
                    )
                    if status == db_manager.NEW:
                        p_new += 1
                        logger.info(
                            "    NEW: %s / %s / %s %s -> %s [%s] (validated=unknown)",
                            publisher, offer, sku, latest, distro_label, architecture,
                        )
                        new_images.append(record)
                    else:  # UPDATED
                        p_updated += 1
                        logger.info(
                            "    updated: %s / %s / %s -> %s (%s) [%s]",
                            publisher, offer, sku, latest, distro_label, architecture,
                        )
                        updated_images.append(record)

            logger.info(
                "  Publisher %s summary: %d offer(s), %d SKU row(s) -> "
                "%d new, %d updated, %d unchanged.",
                publisher, p_offers, p_skus, p_new, p_updated, p_unchanged,
            )

    # ------------------------------------------------------------------
    # Step 5 - Write JSON output  (new and updated SKUs go to Phase 2)
    # ------------------------------------------------------------------
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)

    # Emit new SKUs AND updated ones (a newer marketplace image version of a
    # tracked SKU) so a refreshed image is re-validated. Gate 3 still decides
    # whether the AzNFS package actually changed, so an unchanged package just
    # stays trusted (no VM).
    phase2_input = format_phase2_input(_exclude_distros(new_images + updated_images))
    with open(config.OUTPUT_JSON, "w", encoding="utf-8") as fh:
        json.dump(phase2_input, fh, indent=2)
    logger.info(
        "Wrote %d entry(ies) to %s (the Phase 2 hand-off).",
        len(phase2_input), config.OUTPUT_JSON,
    )

    # ------------------------------------------------------------------
    # Step 5b ΓÇö Distro-level rollup  (the unit AzNFS validates)
    # ------------------------------------------------------------------
    # Collapse every still-unknown SKU row to its unique OS release. A SKU is
    # inserted as validated='unknown' and contributes to its release until
    # Phase 2/3 marks it known_supported / known_unsupported. This distro view
    # drives the new-release diff and the e-mail; it is kept IN MEMORY ONLY ΓÇö
    # needs_validation.json (written above) is the single JSON artifact Phase 1
    # produces (and Phase 2's input).
    all_records = _exclude_distros(db_manager.get_all_records(config.DB_PATH))

    # ------------------------------------------------------------------
    # Step 5a-bis -- One-shot FULL re-validation reset (RESET_VALIDATION)
    # ------------------------------------------------------------------
    # A broken Phase 3 run can bury distros as 'known_unsupported' (or mark a
    # few 'known_supported'), so they are no longer 'unknown' and the backlog
    # feed below would skip them. Arming RESET_VALIDATION (a repo variable / a
    # fresh token) clears EVERY row's verdict back to 'unknown' ONCE, so the
    # whole fleet flows through Phase 2/3 again. It is a one-shot keyed on the
    # token (DB meta), so a forgotten variable does not wipe verdicts every day.
    # Pair it with EMIT_BACKLOG (same or a new token) to actually emit the
    # reset rows; on their own the reset rows only become 'unknown' in the DB.
    reset_token = os.environ.get("RESET_VALIDATION", "").strip()
    if reset_token.lower() not in ("", "0", "false", "no"):
        if db_manager.get_meta(config.DB_PATH, "reset_validation_token") == reset_token:
            logger.info(
                "RESET_VALIDATION=%r already consumed (one-shot); no reset.",
                reset_token,
            )
        else:
            n = db_manager.reset_validation_to_unknown(config.DB_PATH)
            db_manager.set_meta(config.DB_PATH, "reset_validation_token", reset_token)
            logger.warning(
                "RESET_VALIDATION=%r: reset %d image row(s) back to 'unknown' "
                "-- one-shot full re-validation armed.",
                reset_token, n,
            )
            # A reset only marks rows 'unknown'; the backlog feed is what hands
            # them to Phase 2. Armed alone it therefore ERASES every verdict and
            # re-validates nothing, leaving the whole fleet reported as unknown.
            if os.environ.get("EMIT_BACKLOG", "").strip().lower() in ("", "0", "false", "no"):
                logger.error(
                    "RESET_VALIDATION was armed WITHOUT EMIT_BACKLOG: %d row(s) "
                    "lost their verdict and nothing will re-validate them, because "
                    "the hand-off is delta-only. Arm EMIT_BACKLOG with a new token "
                    "(or tick full_revalidation, which arms both).", n,
                )
            all_records = _exclude_distros(db_manager.get_all_records(config.DB_PATH))

    unvalidated_records = [
        r for r in all_records if r.get("validated") == "unknown"
    ]
    distro_rollup = rollup_by_distro(unvalidated_records)
    logger.info(
        "Distros to validate: %d unvalidated release(s) collapsed from "
        "%d unvalidated SKU row(s) (of %d tracked).",
        len(distro_rollup), len(unvalidated_records), len(all_records),
    )
    write_step_summary(distro_rollup, len(all_records))


    # ------------------------------------------------------------------
    # Step 5c ΓÇö One-shot backlog feed  (opt-in, temporary: EMIT_BACKLOG)
    # ------------------------------------------------------------------
    # Normally needs_validation.json carries only the new/updated delta, so
    # distros already cached as validated='unknown' (e.g. after the DB cache is
    # restored) never reach Phase 2/3. Arming EMIT_BACKLOG (a repo variable, so
    # it also applies to the scheduled run -- or the workflow_dispatch input)
    # overwrites the hand-off with the FULL unvalidated backlog so those releases
    # get validated for the first time. It is deduped to one representative SKU
    # per (distro_label, architecture) -- emitting every SKU (~1k rows) would
    # overload Phase 2's per-entry prod checks and Phase 3's VM provisioning.
    #
    # It is a ONE-SHOT: the feed emits at most once per distinct EMIT_BACKLOG
    # value (the "token"), recorded in the DB meta table (which rides the cached
    # marketplace.db across runs). So once the armed run has emitted, every later
    # scan -- even with the repo variable still set -- stays delta-only and will
    # NOT keep re-feeding Phase 3 real VMs. Re-arm by setting EMIT_BACKLOG to a
    # new value (e.g. a fresh date). Empty / 0 / false / no = disabled.
    emit_token = os.environ.get("EMIT_BACKLOG", "").strip()
    if emit_token.lower() not in ("", "0", "false", "no"):
        already = db_manager.get_meta(config.DB_PATH, "backlog_emitted_token")
        if already == emit_token:
            logger.info(
                "EMIT_BACKLOG=%r already consumed (one-shot); staying delta-only.",
                emit_token,
            )
        else:
            backlog = dedup_backlog(unvalidated_records)
            with open(config.OUTPUT_JSON, "w", encoding="utf-8") as fh:
                json.dump(format_phase2_input(backlog), fh, indent=2)
            db_manager.set_meta(config.DB_PATH, "backlog_emitted_token", emit_token)
            logger.warning(
                "EMIT_BACKLOG=%r: wrote %d backlog entry(ies) (one per "
                "distro_label+architecture, collapsed from %d unvalidated SKU "
                "row(s)) to %s -- one-shot full-backlog feed for Phase 2/3 consumed.",
                emit_token, len(backlog), len(unvalidated_records), config.OUTPUT_JSON,
            )


    # ------------------------------------------------------------------
    # Step 6 ΓÇö Notify + exit code  (distro-release granularity)
    # ------------------------------------------------------------------
    # The actionable signal is a NEW distro release, not a new SKU. A new SKU of
    # an already-known release (e.g. another Ubuntu 22.04 variant) is not worth
    # an alert ΓÇö that release is already tracked/validated.
    new_distros = [
        d for d in distro_rollup if d["distro_label"] not in known_distros_before
    ]

    # ------------------------------------------------------------------
    # Monthly reminder ΓÇö independent of the new-release alert below.
    # ------------------------------------------------------------------
    # Sent at most once per calendar month (UTC), on the FIRST scan of the month,
    # regardless of whether that run also found new releases. So on the month's
    # first run both can go out: the new-release email (if any) AND this snapshot
    # of every tracked distro grouped by AzNFS validation state (three groups:
    # known_supported / known_unsupported / unknown). The other ~29 daily runs
    # stay silent. Using the first run of the month (not strictly the 1st) means
    # a missed run on the 1st still sends on the next run.
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    if db_manager.get_meta(config.DB_PATH, "last_monthly_reminder") != current_month:
        buckets = buckets_by_state(all_records)
        if buckets:
            notifier.send_monthly_reminder(buckets)
            db_manager.set_meta(config.DB_PATH, "last_monthly_reminder", current_month)
            logger.info("Monthly reminder sent for %s.", current_month)

    if new_distros:
        logger.info(
            "Scan complete: %d new distro release(s) to validate "
            "(%d new + %d updated SKU row(s) underneath).",
            len(new_distros), len(new_images), len(updated_images),
        )
        notifier.send_phase1_summary(new_distros)
        return 1

    logger.info(
        "Scan complete: no new distro releases "
        "(%d new + %d updated SKU row(s), all within known releases).",
        len(new_images), len(updated_images),
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Exit 2 (not 1) so the workflow can tell a real crash apart from the
        # intentional "new distro release(s) found" signal (exit 1).
        logger.exception("Scan failed with an unhandled error.")
        sys.exit(2)
