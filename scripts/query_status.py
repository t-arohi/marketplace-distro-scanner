#!/usr/bin/env python3
"""
query_status.py - print the current validation buckets straight from the DB.

Same rollup the monthly reminder e-mail is built from (known_supported /
known_unsupported / unknown), queryable on demand:

  python scripts/query_status.py                       # all buckets, text
  python scripts/query_status.py --state known_unsupported
  python scripts/query_status.py --distro ubuntu
  python scripts/query_status.py --format json
  python scripts/query_status.py --skus                # per-SKU rows, not the rollup
  python scripts/query_status.py --format markdown --out STATUS.md   # repo status page

Needs no Azure credentials - it only reads the SQLite DB (DB_PATH env var or
marketplace.db in the repo root).
"""

import argparse
import json
import os
import sys

import aznfs_support
import db_manager
import status_rollup

STATES = ("known_supported", "known_unsupported", "unknown")
_TITLES = {
    "known_supported": "Known supported",
    "known_unsupported": "Known unsupported",
    "unknown": "Unknown / not yet validated",
}
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_db_path() -> str:
    return os.environ.get("DB_PATH", os.path.join(_PROJECT_ROOT, "marketplace.db"))


def _fmt(value) -> str:
    return ", ".join(value) if isinstance(value, (list, tuple, set)) else str(value)


def load_buckets(
    db_path: str,
    states: tuple[str, ...] = STATES,
    distro: str = "",
    include_excluded: bool = False,
) -> dict[str, list[dict]]:
    """Return {state: [distro rollup, ...]} for the requested states."""
    records = db_manager.get_all_records(db_path)
    if not include_excluded:
        records = status_rollup.exclude_distros(records, status_rollup.prefixes_from_env())
    buckets = status_rollup.buckets_by_state(records, in_scope_only=not include_excluded)
    needle = distro.casefold()
    return {
        state: [
            row for row in buckets.get(state, [])
            if needle in (row.get("distro_label") or "").casefold()
        ]
        for state in states
    }


def matching_skus(
    db_path: str,
    states: tuple[str, ...] = STATES,
    distro: str = "",
    include_excluded: bool = False,
) -> list[dict]:
    """Return the individual SKU rows behind the buckets."""
    records = db_manager.get_all_records(db_path)
    if not include_excluded:
        records = status_rollup.exclude_distros(records, status_rollup.prefixes_from_env())
        # Match the buckets: a distro the pipeline will never validate should
        # not reappear once you drill into its SKUs.
        records = [
            r for r in records
            if aznfs_support.is_supported_distro(r.get("distro_label", ""))
        ]
    needle = distro.casefold()
    wanted = set(states)
    rows = []
    for record in records:
        state = record.get("validated") or "unknown"
        bucket = state if state in STATES else "unknown"
        if bucket not in wanted:
            continue
        if needle and needle not in (record.get("distro_label") or "").casefold():
            continue
        rows.append(
            {
                "distro_label": record.get("distro_label", ""),
                "state": state,
                "publisher": record.get("publisher", ""),
                "image": record.get("image", ""),
                "sku": record.get("sku", ""),
                "version": record.get("version", ""),
                "architecture": record.get("architecture", ""),
                "last_validated_version": record.get("last_validated_version", ""),
                "last_validated": record.get("last_validated", ""),
                "reason": record.get("reason", ""),
            }
        )
    rows.sort(key=lambda r: (r["distro_label"], r["architecture"], r["sku"]))
    return rows


def render_text(buckets: dict[str, list[dict]]) -> str:
    total_distros = sum(len(rows) for rows in buckets.values())
    total_skus = sum(row.get("sku_count", 0) for rows in buckets.values() for row in rows)
    lines = [
        f"AzNFS validation status: {total_distros} distro release(s) "
        f"across {total_skus} SKU(s)",
        "",
    ]
    for state, rows in buckets.items():
        lines.append(f"[{_TITLES.get(state, state)}] ({len(rows)})")
        if not rows:
            lines.append("  (none)")
        for row in rows:
            line = (
                f"  - {row.get('distro_label')} "
                f"(latest {row.get('version')}; {_fmt(row.get('publishers', []))}; "
                f"{row.get('sku_count')} SKU(s))"
            )
            if state == "known_unsupported" and row.get("reason"):
                line += f" -- {row['reason']}"
            lines.append(line)
            if state == "known_unsupported":
                for reason, group in status_rollup.group_skus_by_reason(row.get("skus", [])):
                    for s in group:
                        lines.append(f"      * {status_rollup.sku_label(s)}")
                    if reason:
                        lines.append(f"        -- {reason}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _sku_cell(row: dict) -> str:
    """Failing images for one distro, grouped so a shared reason is stated once."""
    skus = row.get("skus") or []
    if not skus:
        return row.get("reason") or "-"
    parts = []
    for reason, group in status_rollup.group_skus_by_reason(skus):
        labels = ", ".join(f"`{status_rollup.sku_label(s)}`" for s in group)
        parts.append(f"{labels} — {reason}" if reason else labels)
    return "<br>".join(parts)


def render_markdown(buckets: dict[str, list[dict]]) -> str:
    total_distros = sum(len(rows) for rows in buckets.values())
    total_skus = sum(row.get("sku_count", 0) for rows in buckets.values() for row in rows)
    counts = " | ".join(
        f"**{_TITLES.get(state, state)}:** {len(rows)}" for state, rows in buckets.items()
    )
    out = [
        "# AzNFS validation status",
        "",
        f"{total_distros} distro release(s) across {total_skus} marketplace SKU(s).",
        "",
        counts,
        "",
        # Deliberately no timestamp: it would change every run and commit churn
        # would hide the real changes. The commit date is the freshness marker.
        "_Generated automatically from the validation database by the AzNFS "
        "pipeline; the commit date shows when it was last refreshed. "
        "Do not edit by hand._",
        "",
    ]
    for state, rows in buckets.items():
        out += [f"## {_TITLES.get(state, state)} ({len(rows)})", ""]
        if not rows:
            out += ["_None._", ""]
            continue
        unsupported = state == "known_unsupported"
        header = "| Distro | Latest image version | Publishers | SKUs |"
        divider = "| --- | --- | --- | ---: |"
        if unsupported:
            header += " Failing SKUs |"
            divider += " --- |"
        out += [header, divider]
        for row in rows:
            line = (
                f"| {row.get('distro_label', '')} | {row.get('version', '')} "
                f"| {_fmt(row.get('publishers', []))} | {row.get('sku_count', 0)} |"
            )
            if unsupported:
                line += f" {_sku_cell(row)} |"
            out.append(line)
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def render_sku_text(rows: list[dict]) -> str:
    if not rows:
        return "No matching SKUs.\n"
    lines = [f"{len(rows)} SKU(s):", ""]
    for row in rows:
        line = (
            f"  - [{row['state']}] {row['distro_label']} ({row['architecture']}) "
            f"{row['publisher']}/{row['image']}/{row['sku']} {row['version']}"
        )
        if row.get("last_validated_version"):
            line += f"; validated aznfs {row['last_validated_version']}"
        if row.get("reason"):
            line += f" -- {row['reason']}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=default_db_path(), help="SQLite DB path (default: $DB_PATH or ./marketplace.db)")
    parser.add_argument("--state", action="append", choices=STATES, help="Limit to a bucket (repeatable)")
    parser.add_argument("--distro", default="", help="Case-insensitive distro-label substring filter")
    parser.add_argument("--format", choices=("text", "json", "markdown"), default="text")
    parser.add_argument("--out", default="", help="Write to this file instead of stdout (e.g. STATUS.md)")
    parser.add_argument("--skus", action="store_true", help="List individual SKUs instead of the distro rollup")
    parser.add_argument("--include-excluded", action="store_true", help="Include distros dropped by EXCLUDED_DISTRO_PREFIXES")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not os.path.exists(args.db):
        print(f"Database not found: {args.db}", file=sys.stderr)
        return 2
    states = tuple(args.state) if args.state else STATES

    if args.skus:
        rows = matching_skus(args.db, states, args.distro, args.include_excluded)
        rendered = json.dumps(rows, indent=2) if args.format == "json" else render_sku_text(rows)
    else:
        buckets = load_buckets(args.db, states, args.distro, args.include_excluded)
        if args.format == "json":
            rendered = json.dumps(buckets, indent=2)
        elif args.format == "markdown":
            rendered = render_markdown(buckets)
        else:
            rendered = render_text(buckets)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(rendered)
        print(f"Wrote {args.out}")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
