#!/usr/bin/env python3
"""
Phase 3 automation driver.

Turns Phase 2 output into Phase 3 outcomes with no human in the loop:

    phase2 jobs.json  ->  [ run LISA per distro ]  ->  parse pass/fail
                                                          |
                                                          v
                                      record_result.run()  (DB update +
                                      one summary e-mail)

It is deliberately thin: all the real logic already lives in the LISA suite
(provision + install + validate) and in ``orchestrator/record_result.py`` (the
post-validation DB update + summary e-mail). This script only sequences them.

INPUT  - a Phase 2 JSON file (lisa_jobs.json): a list of distro jobs, each with
         the marketplace image identity, the published PMC prod package URL +
         version, and an arch. Example item:
           {
             "publisher": "redhat", "image": "rhel", "sku": "9_5",
             "version": "latest", "region": "centralindia",
             "arch": "x86_64", "distro_label": "RHEL 9.5",
             "aznfs_package_url": "https://.../aznfs-0.3.458-1.x86_64.rpm",
             "aznfs_version": "0.3.458"
           }

FLOW
  1. For each distro, run the base runbook with ``-v`` overrides (3 cases in
     parallel via ``concurrency:3``; each case in its own auto-deleted RG).
  2. Read that run's ``lisa.junit.xml`` (the junit notifier) -> the distro is
     "passed" when it has at least one case and zero failed cases; on a failure
     the failing ``[Tier N: step]`` tag is extracted.
  3. Hand the pass/fail results to ``record_result.run()`` which records each
     verdict in the DB and sends one summary e-mail.

Distro-level parallelism is controlled by ``--max-parallel-distros`` (each
distro itself already runs its 3 cases in parallel). Bound it by your vCPU
quota: VMs in flight ~= max_parallel_distros * 3 * 2 vCPUs.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import logging
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Tuple

# Reuse the orchestrator's job model + DB/notify logic.
from .orchestrator import record_result
from .orchestrator.record_result import LisaJob

logger = logging.getLogger("phase3.driver")

_HERE = Path(__file__).resolve().parent
_BASE_RUNBOOK = _HERE / "runbooks" / "aznfs_validation.yml"
# LISA logs the line `run log path: <PATH>, working path: <PATH>` (note the
# trailing comma + space after <PATH> -- see lisa/main.py). Capturing `\S+`
# greedily swallowed that comma, yielding `<PATH>,/lisa.junit.xml` (a path that
# never exists). Exclude whitespace AND commas so we get the clean directory.
_RUN_LOG_RE = re.compile(r"run log path:\s*([^\s,]+)")
# A test asserts with a "[Tier N: step]" prefix so a failure can be blamed on the
# exact stage (artifact / install / footprint / mount / io / watchdog).
_TIER_RE = re.compile(r"\[Tier \d[^\]]*\]")


def _overrides(job: LisaJob, subscription_id: str, concurrency: int) -> List[str]:
    """Build the ``-v key:value`` arguments for one distro's LISA run."""
    image = f"{job.publisher} {job.image} {job.sku} {job.version}"
    pairs = {
        "subscription_id": subscription_id,
        "marketplace_image": image,
        "aznfs_package_url": job.aznfs_package_url,
        "aznfs_expected_version": job.aznfs_version,
        "keep_environment": "no",
        "concurrency": str(concurrency),
        # PHASE3_RESOURCE_GROUP pins all envs into one pre-created RG so the
        # runner MI only needs Network/Storage/VM Contributor on that RG (no
        # subscription resourcegroups/write). A pinned RG shares one ARM
        # deployment name + SSH key, so concurrency is forced to 1 in main().
        # Empty => one auto-deleted RG per env (needs sub-scope rights).
        "resource_group_name": os.environ.get("PHASE3_RESOURCE_GROUP", ""),
    }
    args: List[str] = []
    for key, value in pairs.items():
        args += ["-v", f"{key}:{value}"]
    return args


def _run_lisa(job: LisaJob, subscription_id: str, concurrency: int) -> Path:
    """Run LISA for one distro; return the path to that run's junit XML.

    The run log path is parsed from LISA's stdout, so concurrent distro runs
    never confuse each other's results (no "newest dir" guessing).
    """
    cmd = ["lisa", "run", "-r", str(_BASE_RUNBOOK)] + _overrides(
        job, subscription_id, concurrency
    )
    logger.info("[%s] starting LISA: %s", job.distro_label, " ".join(cmd))
    proc = subprocess.run(
        cmd, capture_output=True, text=True, check=False
    )
    # LISA writes its structured log (including "run log path:") to stderr
    # (Python logging default). Search both streams so neither is missed.
    match = _RUN_LOG_RE.search(proc.stdout) or _RUN_LOG_RE.search(proc.stderr)
    if not match:
        raise RuntimeError(
            f"could not find run log path in LISA output for {job.distro_label}\n"
            f"stdout: {proc.stdout[-500:]}\nstderr: {proc.stderr[-500:]}"
        )
    junit = Path(match.group(1)) / "lisa.junit.xml"
    if not junit.exists():
        # LISA printed a run-log path but produced no junit -> it exited before
        # writing results (e.g. it crashed at startup, or the image/auth failed
        # fast). Surface LISA's own output so the failure is diagnosable in the
        # summary e-mail instead of a bare FileNotFoundError.
        raise RuntimeError(
            f"LISA produced no junit for {job.distro_label} "
            f"(exit={proc.returncode}); last LISA output:\n"
            f"{(proc.stderr or proc.stdout)[-700:]}"
        )
    return junit


def _parse_junit(xml_path: Path) -> Tuple[int, int, int, str]:
    """Return (total, failed, skipped, reason) case counts from a junit report.

    ``reason`` summarizes the failed case(s) so the driver can report WHICH tier
    failed. When a test's ``[Tier N: step]`` tag is present in the failure
    message it is used verbatim (it already names the failing stage); otherwise
    the LISA case name is used.
    """
    root = ET.parse(xml_path).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    total = failed = skipped = 0
    reasons: List[str] = []
    for suite in suites:
        total += int(suite.get("tests", 0))
        failed += int(suite.get("failures", 0)) + int(suite.get("errors", 0))
        skipped += int(suite.get("skipped", 0))
        for case in suite.findall("testcase"):
            problem = case.find("failure")
            if problem is None:
                problem = case.find("error")
            if problem is None:
                continue
            raw = (problem.get("message") or problem.text or "").strip()
            first = raw.splitlines()[0].strip() if raw else "failed"
            if _TIER_RE.search(first):
                reasons.append(first)  # already starts with "[Tier N: step] ..."
            else:
                reasons.append(f"{case.get('name') or 'case'}: {first}")
    return total, failed, skipped, "; ".join(reasons)


# Failures that mean "we could not test this distro", not "AzNFS is broken on it":
# the VM never deployed, Azure refused, or the environment died before any case
# ran. Recording these as a verdict retires a distro for an Azure problem.
_INFRA_RE = re.compile(
    r"driver/infra error"
    r"|deployment failed"
    r"|environment failed"
    r"|no available environment"
    r"|authorizationfailed"
    r"|httpresponseerror"
    r"|marketplace\w*ordering"
    r"|purchase\w* plan|accept the terms|termsnotaccepted"
    r"|quotaexceeded|subscriptionrequeststhrottled|allocationfailed|skunotavailable"
    r"|failed to connect|connection\s+(?:refused|timed out|reset)"
    r"|ssh.*(?:timeout|timed out|authentication failed)"
    r"|private key file not exist|no such file or directory.*environment\.log",
    re.IGNORECASE,
)


def _is_infra_failure(reason: str) -> bool:
    return bool(reason) and bool(_INFRA_RE.search(reason))


def _validate_one(
    job: LisaJob, subscription_id: str, concurrency: int
) -> LisaJob:
    """Run + score one distro, setting ``job.lisa_passed`` and, on failure,
    ``job.failure_reason`` (the failing tier/step for the summary e-mail).

    ``job.infra_error`` marks a run that never produced a usable answer, which
    is recorded as "retry me" rather than as a verdict against the distro.
    """
    try:
        junit = _run_lisa(job, subscription_id, concurrency)
        total, failed, skipped, reason = _parse_junit(junit)
        ran = total - skipped
        job.lisa_passed = ran > 0 and failed == 0
        if not job.lisa_passed:
            job.failure_reason = reason or (
                "no test cases ran (all skipped or environment failed)"
            )
            # Nothing executed, or what failed was Azure rather than AzNFS.
            job.infra_error = ran <= 0 or _is_infra_failure(job.failure_reason)
        logger.info(
            "[%s] total=%d failed=%d skipped=%d -> %s%s",
            job.distro_label, total, failed, skipped,
            "PASSED" if job.lisa_passed
            else ("INFRA ERROR" if job.infra_error else "FAILED"),
            "" if job.lisa_passed else f" ({job.failure_reason})",
        )
    except Exception as exc:
        job.lisa_passed = False
        job.infra_error = True
        job.failure_reason = f"driver/infra error: {exc}"
        logger.error("[%s] LISA run errored: %s", job.distro_label, exc)
    return job


def validate_distros(
    jobs: List[LisaJob],
    subscription_id: str,
    concurrency: int,
    max_parallel_distros: int,
) -> List[LisaJob]:
    """Run LISA for every distro (distros optionally in parallel)."""
    if max_parallel_distros <= 1:
        return [_validate_one(j, subscription_id, concurrency) for j in jobs]
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max_parallel_distros
    ) as pool:
        futures = [
            pool.submit(_validate_one, j, subscription_id, concurrency)
            for j in jobs
        ]
        return [f.result() for f in futures]


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Phase 3 automation driver.")
    parser.add_argument(
        "jobs_json", help="Phase 2 output: JSON list of distro jobs."
    )
    parser.add_argument(
        "--subscription-id", required=True, help="Azure subscription id."
    )
    parser.add_argument(
        "--concurrency", type=int, default=3,
        help="Cases in parallel per distro (default 3 = all cases at once).",
    )
    parser.add_argument(
        "--max-parallel-distros", type=int, default=1,
        help="Distros validated at once (bound by vCPU quota).",
    )
    parser.add_argument(
        "--distro-filter", default=os.environ.get("PHASE3_DISTRO_FILTER", ""),
        help=(
            "Optional case-insensitive regex. When set, only distros whose "
            "'<publisher> <image> <sku> <version>' identity matches are "
            "validated (the rest are skipped). Defaults to env "
            "PHASE3_DISTRO_FILTER. Empty => validate every distro."
        ),
    )
    args = parser.parse_args()

    # A pinned resource group shares one ARM deployment name + SSH key, so envs
    # cannot run in parallel: force serial (1 case, 1 distro at a time).
    if os.environ.get("PHASE3_RESOURCE_GROUP", ""):
        if args.concurrency != 1 or args.max_parallel_distros != 1:
            logger.warning(
                "PHASE3_RESOURCE_GROUP=%s pins one RG -> forcing concurrency=1 "
                "and max_parallel_distros=1",
                os.environ.get("PHASE3_RESOURCE_GROUP", ""),
            )
        args.concurrency = 1
        args.max_parallel_distros = 1

    jobs = record_result.load_jobs(args.jobs_json)
    if not jobs:
        logger.warning("no jobs in %s; nothing to do.", args.jobs_json)
        return 0

    # Optional subsetting: validate only the distros whose identity matches the
    # filter regex (e.g. to re-run a single failure class). Matching nothing is
    # treated as an error so a typo'd filter fails loudly instead of silently
    # recording zero results.
    if args.distro_filter:
        pattern = re.compile(args.distro_filter, re.IGNORECASE)
        selected = [
            j for j in jobs
            if pattern.search(f"{j.publisher} {j.image} {j.sku} {j.version}")
        ]
        logger.info(
            "distro filter %r matched %d of %d distro(s): %s",
            args.distro_filter, len(selected), len(jobs),
            [j.distro_label or j.sku for j in selected],
        )
        if not selected:
            logger.error(
                "distro filter %r matched no distros; available identities:\n%s",
                args.distro_filter,
                "\n".join(
                    f"  {j.publisher} {j.image} {j.sku} {j.version}" for j in jobs
                ),
            )
            return 1
        jobs = selected

    logger.info("validating %d distro(s) with LISA...", len(jobs))
    jobs = validate_distros(
        jobs, args.subscription_id, args.concurrency,
        args.max_parallel_distros,
    )

    # Post-validation: record verdicts in the DB + send ONE summary e-mail.
    summary = record_result.run(jobs)
    logger.info("phase 3 complete. states: %s", summary)
    # Exit codes mirror Phase 1: 0 = everything passed, 1 = a distro genuinely
    # failed validation (expected, actionable by the AzNFS team), 2 = we could
    # not test something. Without the split every failing distro paints the run
    # red and a broken pipeline looks exactly like a normal bad result.
    if any(j.infra_error for j in jobs):
        broken = [j.distro_label or j.sku for j in jobs if j.infra_error]
        logger.error(
            "%d distro(s) could not be tested (infrastructure, not AzNFS): %s",
            len(broken), ", ".join(broken),
        )
        return 2
    return 0 if all(j.lisa_passed for j in jobs) else 1


if __name__ == "__main__":
    sys.exit(main())

