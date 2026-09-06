#!/usr/bin/env python3
"""Delete what Phase 3 leaves behind in Azure, and alert if anything survives.

LISA's only cleanup is deleting the whole resource group it created. Two ways
that fails, and this handles both:

* **No RG pinned** (the normal setup): LISA makes one group per environment and
  deletes it at the end. If it dies first the group is orphaned, so this sweeps
  the groups tagged ``created_by=aznfs-phase3`` that outlived their run.
* **An RG pinned** in the runbook: LISA deliberately skips deletion
  (``platform_.py``: "skipped to delete resource group ... as it's specified in
  runbook") and has no per-resource fallback, so every VM survives. That is how
  one group reached 103 running VMs in a week. This deletes the VMs, then the
  NICs / public IPs / disks / private endpoints they orphan, then LISA's
  transient storage accounts -- keeping the shared VNet, NSG and the shared
  storage account, which LISA reuses.

Run with ``--alert`` to e-mail when anything survives, so a future cleanup
regression is noticed instead of quietly costing money.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# LISA's per-environment NFS storage accounts (features.py: STORAGE_ACCOUNT_PREFIX
# = "lisasc" + 10 random chars). Deliberately NOT just "lisa": LISA's SHARED
# account is lisas<location><subscription-suffix> (e.g. lisascentralindi92ef804a),
# which is reused across runs and must survive.
STORAGE_PREFIX = "lisasc"
# "lisasc" still matches that shared account wherever the location starts with a
# c (centralindia, centralus, canadacentral...), so exclude the shared shape too.
# Locations can contain digits (chinanorth3), and the random suffix is always 10
# chars -- which cannot match this, since it would need a 3-char location.
SHARED_STORAGE_RE = re.compile(r"^lisas[a-z0-9]{4,11}[0-9a-f]{8}$")

# Applied by the runbook to every resource group LISA creates for us, so an
# orphan can be identified without guessing from its name.
OWNER_TAG = "created_by"
OWNER_VALUE = "aznfs-phase3"


def _az(*args: str) -> object:
    """Run an `az` command and return its parsed JSON output.

    Pinned to AZURE_SUBSCRIPTION_ID when set: these commands delete things, and
    the CLI would otherwise use whatever subscription happens to be the default
    on the runner. The workflow logs in with --allow-no-subscriptions, so there
    may not even be one.
    """
    subscription = os.environ.get("AZURE_SUBSCRIPTION_ID", "").strip()
    scope = ["--subscription", subscription] if subscription else []
    proc = subprocess.run(
        ["az", *args, *scope, "--output", "json"],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"az {' '.join(args)} failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def _parse_created(value: str) -> datetime | None:
    # Azure reports 100ns precision (7 fractional digits); fromisoformat only
    # accepts up to 6 before Python 3.11, and this package supports 3.10.
    text = re.sub(r"(\.\d{6})\d+", r"\1", (value or "").replace("Z", "+00:00"))
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    # fromisoformat accepts a string with no offset and returns a naive datetime,
    # which cannot be compared against the aware cutoff -- that TypeError would
    # abort the sweep and quietly resume the leak.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def stale_vms(resource_group: str, older_than_hours: float) -> list[dict]:
    """VMs eligible for deletion, oldest first.

    ``older_than_hours`` guards a concurrently running environment: 0 sweeps
    everything (safe straight after a run, which holds the only Phase 3 slot),
    a few hours suits a scheduled sweep.
    """
    # Sorted on the parsed time, not the raw string: offsets and missing values
    # do not sort sensibly as text. An unreadable timestamp counts as very old,
    # matching the eligibility rule below.
    oldest_first = datetime.min.replace(tzinfo=timezone.utc)

    vms = _az("vm", "list", "-g", resource_group,
              "--query", "[].{name:name, id:id, created:timeCreated}") or []
    if older_than_hours <= 0:
        return sorted(vms, key=lambda v: _parse_created(v.get("created", "")) or oldest_first)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
    eligible: list[dict] = []
    for vm in vms:
        created = _parse_created(vm.get("created", ""))
        if created is None:
            # Deleting a VM we cannot date could kill a running test, so keep it
            # -- but it is counted as undatable so the alert still fires and it
            # cannot sit there forever unnoticed.
            logger.warning("Keeping %s: creation time %r unreadable and a cutoff is in force",
                           vm.get("name"), vm.get("created"))
        elif created < cutoff:
            eligible.append(vm)
        else:
            logger.info("Keeping %s: created %s, newer than the cutoff",
                        vm.get("name"), vm.get("created"))
    return sorted(eligible, key=lambda v: _parse_created(v.get("created", "")) or oldest_first)


def _delete_each(kind: str, ids: list[str], *cmd: str) -> tuple[int, int]:
    """Delete resources one at a time, surviving individual failures.

    A single stubborn resource must not abandon the rest of the sweep -- that is
    how one private-endpoint NIC left 106 disks and public IPs behind.
    """
    ok = failed = 0
    for resource_id in ids:
        try:
            _az(*cmd, "--ids", resource_id)
            ok += 1
        except Exception as exc:  # noqa: BLE001 - report and keep going
            failed += 1
            logger.warning("Could not delete %s %s: %s", kind, resource_id.split("/")[-1], exc)
    return ok, failed


def _delete_orphans(resource_group: str,
                    detached_only: bool = False) -> tuple[dict[str, int], int]:
    """Remove what the VMs leave behind, in dependency order.

    ``detached_only`` is the safe subset for when a cutoff is protecting a live
    environment: disks, NICs and public IPs are filtered to those provably not
    in use, but a private endpoint and a storage account carry no such state and
    a running test needs both -- so those are skipped entirely.

    Private endpoints otherwise go FIRST: their NIC cannot be deleted on its own
    (Azure rejects it with NicInUseWithPrivateEndpoint) and disappears with the
    endpoint.
    """
    removed = {"private_endpoints": 0, "nics": 0, "public_ips": 0, "disks": 0, "storage": 0}
    failures = 0

    # The `az network *` deletes reject --yes as an unrecognized argument and do
    # not prompt; `az disk`/`az storage account` below do take it.
    if not detached_only:
        endpoints = _az("network", "private-endpoint", "list", "-g", resource_group,
                        "--query", "[].id") or []
        removed["private_endpoints"], f = _delete_each(
            "private endpoint", endpoints, "network", "private-endpoint", "delete")
        failures += f

    # A private endpoint's NIC also has no VM, but cannot be deleted directly.
    nics = _az("network", "nic", "list", "-g", resource_group,
               "--query", "[?virtualMachine==null && privateEndpoint==null].id") or []
    removed["nics"], f = _delete_each("nic", nics, "network", "nic", "delete")
    failures += f

    ips = _az("network", "public-ip", "list", "-g", resource_group,
              "--query", "[?ipConfiguration==null].id") or []
    removed["public_ips"], f = _delete_each("public ip", ips, "network", "public-ip", "delete")
    failures += f

    disks = _az("disk", "list", "-g", resource_group,
                "--query", "[?diskState=='Unattached'].id") or []
    removed["disks"], f = _delete_each("disk", disks, "disk", "delete", "--yes")
    failures += f

    if not detached_only:
        accounts = _az("storage", "account", "list", "-g", resource_group,
                       "--query", f"[?starts_with(name, '{STORAGE_PREFIX}')]"
                                  ".{name:name, id:id}") or []
        ids = [a["id"] for a in accounts if not SHARED_STORAGE_RE.match(a["name"])]
        removed["storage"], f = _delete_each(
            "storage account", ids, "storage", "account", "delete", "--yes")
        failures += f

    return removed, failures


def orphan_groups(older_than_hours: float) -> list[str]:
    """Tagged resource groups LISA should have deleted and did not.

    Only reachable when no RG is pinned -- which is the normal configuration,
    where LISA makes one group per environment and deletes it itself. A group
    still standing afterwards means that cleanup did not happen.

    Age is taken from the newest VM inside, because resource groups carry no
    creation timestamp. A group with no VMs left is treated as sweepable.
    """
    groups = _az("group", "list", "--tag", f"{OWNER_TAG}={OWNER_VALUE}",
                 "--query", "[].name") or []
    if older_than_hours <= 0:
        return sorted(groups)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
    stale: list[str] = []
    for name in groups:
        stamps = _az("vm", "list", "-g", name, "--query", "[].timeCreated") or []
        parsed = [c for c in (_parse_created(s) for s in stamps) if c]
        if not stamps:
            stale.append(name)          # nothing left running: safe to remove
        elif not parsed:
            # Holds VMs we cannot date -- assume one is live rather than delete
            # the group out from under it.
            logger.warning("Keeping %s: holds VMs with unreadable creation times", name)
        elif max(parsed) < cutoff:
            stale.append(name)
        else:
            logger.info("Keeping %s: holds a VM created %s", name, max(parsed))
    return sorted(stale)


def sweep_orphan_groups(older_than_hours: float, dry_run: bool = False) -> dict:
    """Delete whole orphaned groups -- the cleanup LISA skipped.

    Deletion is asynchronous (``--no-wait``, so several groups unwind at once
    rather than blocking the job for minutes each), which means a request being
    accepted is not proof it finished. Nothing here claims otherwise: the count
    is of requests, and because the sweep is idempotent and alerts on *any*
    orphan, one that stalls or fails is reported again by the next run.
    """
    victims = orphan_groups(older_than_hours)
    logger.info("%d orphaned resource group(s) tagged %s=%s",
                len(victims), OWNER_TAG, OWNER_VALUE)
    if dry_run:
        for name in victims:
            logger.info("  would delete resource group %s", name)
        return {"deletions_requested": 0, "eligible": len(victims), "failures": 0}

    failures = 0
    for name in victims:
        try:
            _az("group", "delete", "--name", name, "--yes", "--no-wait")
            logger.info("Requested deletion of resource group %s", name)
        except Exception as exc:  # noqa: BLE001 - report and keep going
            failures += 1
            logger.warning("Could not delete resource group %s: %s", name, exc)
    return {"deletions_requested": len(victims) - failures,
            "eligible": len(victims), "failures": failures}


def _undatable_count(resource_group: str, older_than_hours: float) -> int:
    """VMs a cutoff has to keep because their creation time cannot be read.

    Zero without a cutoff: nothing is being kept then, so counting them would
    report the VMs being deleted as "left running" -- and in a dry run the same
    VM would appear in both halves of the message.
    """
    if older_than_hours <= 0:
        return 0
    vms = _az("vm", "list", "-g", resource_group,
              "--query", "[].{name:name, created:timeCreated}") or []
    return sum(1 for vm in vms if _parse_created(vm.get("created", "")) is None)


def sweep(resource_group: str, older_than_hours: float,
          dry_run: bool = False) -> dict:
    """Delete stale VMs and their orphans. Returns what was removed and left.

    With ``older_than_hours`` at 0 this is the AGGRESSIVE path: everything in
    the group goes, including private endpoints and storage accounts that
    nothing marks as detached. Only run it against a group known to be idle --
    straight after a run, which holds the only Phase 3 slot. Any cutoff above 0
    restricts it to what is provably unused, so a live environment survives.
    """
    victims = stale_vms(resource_group, older_than_hours)
    logger.info("%d VM(s) eligible for deletion in %s", len(victims), resource_group)

    if dry_run:
        for vm in victims:
            logger.info("  would delete %s (created %s)", vm["name"], vm.get("created"))
        # Reported, not zeroed: a dry run has to predict the alert a real one
        # would raise, and undatable VMs are part of that.
        return {"deleted_vms": 0, "eligible": len(victims), "failures": 0,
                "orphans": {"private_endpoints": 0, "nics": 0, "public_ips": 0,
                            "disks": 0, "storage": 0},
                # No deletion was attempted, so nothing survived one. What a
                # real run would remove is `eligible`.
                "remaining": 0,
                "undatable": _undatable_count(resource_group, older_than_hours)}

    # One at a time: a bulk `az vm delete --ids` aborts the whole sweep if any
    # single VM fails, which is how orphans piled up before.
    deleted, failures = _delete_each(
        "vm", [vm["id"] for vm in victims], "vm", "delete", "--yes")

    # With a cutoff in force some VMs are deliberately still running, so only
    # touch resources that are provably detached from them.
    orphans, orphan_failures = _delete_orphans(
        resource_group, detached_only=older_than_hours > 0)

    # Count what is still ELIGIBLE, not every VM: the ones the cutoff keeps are
    # meant to be there and must not raise an alert. VMs kept only because their
    # age is unreadable are separate -- they DO need reporting, or they sit
    # there forever.
    remaining = len(stale_vms(resource_group, older_than_hours))
    undatable = _undatable_count(resource_group, older_than_hours)
    return {"deleted_vms": deleted, "eligible": len(victims),
            "orphans": orphans, "failures": failures + orphan_failures,
            "remaining": remaining, "undatable": undatable}


def _cutoff_hours(value: str) -> float:
    """Reject a cutoff that cannot mean what the caller intended.

    A negative value silently selects the aggressive path -- so a typo like
    ``--older-than-hours -24`` would delete everything instead of protecting the
    last 24 hours. Infinity raises deep inside timedelta instead of here.
    """
    hours = float(value)
    if not math.isfinite(hours) or hours < 0:
        raise argparse.ArgumentTypeError(
            f"must be a finite, non-negative number of hours, not {value!r}")
    return hours


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource-group", default="",
                        help="a pinned RG to sweep; omit to sweep the tagged "
                             "per-environment groups LISA failed to delete")
    parser.add_argument("--older-than-hours", type=_cutoff_hours, default=0.0,
                        help="0 (default) sweeps everything; use a few hours for a "
                             "scheduled sweep that must not touch a live run")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--alert", action="store_true",
                        help="e-mail if anything survives the sweep")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    scope = (f"resource group {args.resource_group}" if args.resource_group
             else f"the groups tagged {OWNER_TAG}={OWNER_VALUE}")
    try:
        if args.resource_group:
            result = sweep(args.resource_group, args.older_than_hours, args.dry_run)
        else:
            result = sweep_orphan_groups(args.older_than_hours, args.dry_run)
    except Exception as exc:  # noqa: BLE001 - cleanup must report, never abort the run
        logger.exception("VM sweep failed")
        if args.alert:
            _alert(scope, f"the sweep itself failed: {exc}")
        return 1

    if not args.resource_group:
        logger.info("Requested deletion of %d orphaned group(s); %d failure(s)",
                    result["deletions_requested"], result["failures"])
        # An orphan at all means LISA's own cleanup did not run. Deletion is
        # async, so a request that stalls is caught by the next run finding the
        # same group still there.
        if args.alert and (result["eligible"] or result["failures"]):
            _alert(scope,
                   f"{'[dry run] ' if args.dry_run else ''}"
                   f"{result['eligible']} group(s) outlived the run that made "
                   f"them and {result['failures']} deletion(s) could not be requested")
        return 0

    logger.info("Deleted %d VM(s); orphans removed: %s; %d failure(s); "
                "%d VM(s) remain; %d of unreadable age",
                result["deleted_vms"], result["orphans"],
                result.get("failures", 0), result["remaining"],
                result.get("undatable", 0))
    if args.dry_run:
        # Report what a real run WOULD remove. Saying these VMs "survived"
        # inverts the meaning: they are the deletion candidates.
        if args.alert and (result["eligible"] or result.get("undatable")):
            _alert(scope,
                   f"[dry run] {result['eligible']} VM(s) would be deleted and "
                   f"{result.get('undatable', 0)} left running because their age "
                   f"could not be read; nothing was actually deleted")
    elif args.alert and (result["remaining"] or result.get("failures")
                         or result.get("undatable")):
        # "still present" would contradict itself: remaining counts only the
        # ELIGIBLE survivors, so it can be 0 while undatable VMs are running.
        _alert(scope,
               f"{result['remaining']} eligible VM(s) survived the sweep, "
               f"{result.get('undatable', 0)} were left running because their age "
               f"could not be read, and {result.get('failures', 0)} resource(s) "
               f"could not be deleted")
    return 0


def _alert(scope: str, detail: str) -> None:
    """Mail the team; a silent cleanup failure is what let 103 VMs accumulate.

    ``scope`` is a resource group name or a tag selector, so the wording has to
    read correctly for both.
    """
    try:
        import notifier
        notifier.notify(
            subject=f"[AzNFS pipeline] Phase 3 VM cleanup needs attention ({scope})",
            plain=(f"Phase 3 leaves Azure resources behind unless they are swept, and "
                   f"{detail}.\n\nCheck {scope}: every VM left running is billed "
                   f"until it is removed."),
        )
    except Exception:  # noqa: BLE001 - never let the alert break the run
        logger.exception("Could not send the cleanup alert")


if __name__ == "__main__":
    raise SystemExit(main())
