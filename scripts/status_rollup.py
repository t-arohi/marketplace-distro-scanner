"""Validation-state rollup shared by the monthly digest e-mail and `query_status.py`.

Kept free of `config`/Azure imports so read-only status queries work without
Azure credentials.
"""

import os
import re

import aznfs_support
import db_manager

_DEFAULT_EXCLUDED_PREFIXES = "centos"

_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I
)
# ARM paths keep their shape so the reason still reads sensibly.
_ARM_SCOPE_RE = re.compile(r"/subscriptions/[^\s'\"]*", re.I)


def redact(text: str) -> str:
    """Strip subscription/principal identifiers out of a verdict reason.

    Reasons are copied verbatim from Azure errors and end up on a page anyone
    can read, so drop the operational identifiers and keep the failure itself.
    """
    if not text:
        return ""
    cleaned = _ARM_SCOPE_RE.sub("<scope redacted>", text)
    return _UUID_RE.sub("<id redacted>", cleaned)


def prefixes_from_env() -> list[str]:
    """Distro-label prefixes to drop, from EXCLUDED_DISTRO_PREFIXES."""
    raw = os.environ.get("EXCLUDED_DISTRO_PREFIXES", _DEFAULT_EXCLUDED_PREFIXES)
    return [s.strip().lower() for s in raw.split(",") if s.strip()]


def exclude_distros(records: list[dict], prefixes: list[str]) -> list[dict]:
    """Drop records whose distro_label starts with any of ``prefixes``.

    Applied to both the delta hand-off and the EMIT_BACKLOG feed so excluded
    distros (e.g. EOL CentOS) never reach Phase 2/3, regardless of what the
    cached DB still holds.
    """
    if not prefixes:
        return records
    kept = []
    for r in records:
        label = (r.get("distro_label") or "").lower()
        if any(label.startswith(p) for p in prefixes):
            continue
        kept.append(r)
    return kept


def sku_label(sku: dict) -> str:
    """One image identified the way the reports show it: ``offer/sku (arch)``."""
    return f"{sku.get('image', '')}/{sku.get('sku', '')} ({sku.get('architecture', '')})"


def group_skus_by_reason(skus: list[dict]) -> list[tuple[str, list[dict]]]:
    """Collapse SKUs that failed identically, so the reason is stated once.

    Whole releases usually fail the same way (every Debian 11 image missing the
    same package), and repeating a 70-character reason per SKU buries the one
    thing the reader needs: which images are affected.
    """
    groups: dict[str, list[dict]] = {}
    for s in skus:
        groups.setdefault(s.get("reason", "") or "", []).append(s)
    return sorted(groups.items(), key=lambda kv: kv[0])


def buckets_by_state(records: list[dict], in_scope_only: bool = True) -> dict[str, list[dict]]:
    """Group tracked images into per-validation-state buckets for the monthly reminder.

    Buckets are ``known_supported`` / ``known_unsupported`` / ``unknown`` (the
    last also folds in the not-yet-decided ``pending_*`` states). For each
    (state, distro_label) the latest version observed is kept, with the
    contributing publishers and the number of SKUs. Returns {state: [distro,...]}.

    Distros outside the AzNFS support matrix are dropped by default: they are
    scanned and stored, but never handed to Phase 2/3, so reporting them as
    'not yet validated' promises a backlog that does not exist and buries the
    releases that genuinely are pending. Filtering here rather than in each
    caller keeps the status page and the monthly e-mail showing the same thing.

    Each entry also carries ``skus``: the individual images behind it, with the
    per-image reason. A distro release is a group of quite different images
    (server, minimal, cvm, pro, arm64), so "Ubuntu 24.04 is unsupported" is not
    actionable on its own -- the reports name the exact SKU that failed.
    """
    def _state_of(img: dict) -> str:
        v = img.get("validated", "") or ""
        if v == "known_supported":
            return "known_supported"
        if v == "known_unsupported":
            return "known_unsupported"
        return "unknown"  # unknown + pending_publish + new

    if in_scope_only:
        records = [r for r in records
                   if aznfs_support.is_supported_distro(r.get("distro_label", ""))]

    groups: dict[tuple[str, str], dict] = {}
    for img in records:
        state = _state_of(img)
        key = (state, img.get("distro_label", ""))
        g = groups.get(key)
        if g is None:
            g = {
                "state": state,
                "distro_label": key[1],
                "version": img.get("version", ""),
                "publishers": set(),
                "sku_count": 0,
                "reasons": set(),
                "skus": [],
            }
            groups[key] = g
        if img.get("publisher"):
            g["publishers"].add(img["publisher"])
        # Numeric: '9.10.x' is newer than '9.8.x' but sorts below it as a string.
        if db_manager.version_tuple(img.get("version", "")) > db_manager.version_tuple(g["version"]):
            g["version"] = img["version"]
        # Collect the distinct verdict reasons -- only meaningful for unsupported.
        r = redact((img.get("reason") or "").strip())
        if state == "known_unsupported" and r:
            g["reasons"].add(r)
        g["skus"].append({
            "image": img.get("image", ""),
            "sku": img.get("sku", ""),
            "architecture": img.get("architecture", ""),
            "version": img.get("version", ""),
            "reason": r,
        })
        g["sku_count"] += 1

    buckets: dict[str, list[dict]] = {}
    for g in groups.values():
        buckets.setdefault(g["state"], []).append(
            {
                "distro_label": g["distro_label"],
                "version": g["version"],
                "publishers": sorted(g["publishers"]),
                "sku_count": g["sku_count"],
                "reason": "; ".join(sorted(g["reasons"])),
                "skus": sorted(g["skus"], key=lambda s: (s["architecture"], s["image"], s["sku"])),
            }
        )
    for st in buckets:
        buckets[st].sort(key=lambda d: d["distro_label"])
    return buckets
