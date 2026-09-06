from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.phase2 import pmc_packages
from src.phase2.orchestrator import (
    KNOWN_SUPPORTED,
    KNOWN_UNSUPPORTED,
    PENDING_PUBLISH,
    process_entry,
    run_phase2,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeProd:
    """Models PMC prod: which pockets exist and which aznfs files they list."""

    def __init__(self, repos=None, packages=None, raise_on_resolve=False,
                 published=None):
        self.repos = repos or {}                 # distro -> {version segments}
        self.packages = packages or {}           # (distro, version) -> [filenames]
        self.raise_on_resolve = raise_on_resolve
        self.published = published or {}         # filename -> datetime

    def resolve_repo(self, distro, candidates, family=""):
        if self.raise_on_resolve:
            raise RuntimeError("prod down")
        present = self.repos.get(distro, set())
        for v in candidates:
            if v in present:
                return v
        return None

    def list_packages(self, distro, version, family):
        return list(self.packages.get((distro, version), []))

    def published_at(self, distro, version, family, filename):
        return self.published.get(filename)


@dataclass
class FakeDb:
    updates: list[tuple] = field(default_factory=list)
    sources: list = field(default_factory=list)

    def set_validation_state(self, identity, state, reason=None, verdict_source=None):
        self.updates.append((identity, state, reason))
        self.sources.append(verdict_source)


@dataclass
class FakeNotifier:
    """Phase 2 sends exactly one mail per run -- the summary. Capture it."""

    summaries: list[dict] = field(default_factory=list)

    def notify_summary(self, processed, to_phase3, trusted, pending_publish, unsupported, errors):
        self.summaries.append({
            "processed": processed,
            "to_phase3": to_phase3,
            "trusted": trusted,
            "pending_publish": pending_publish,
            "unsupported": unsupported,
            "errors": errors,
        })


def entry(**kw):
    base = {
        "publisher": "Canonical",
        "image": "ubuntu-22_04-lts",
        "sku": "server",
        "version": "22.04.202506",
        "region": "eastus",
        "architecture": "x86_64",
        "family": "apt",
        "distro_label": "Ubuntu 22.04",
        "last_validated_version": "",
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Gate 1 -> unsupported  (process_entry returns the outcome + reason; no mail)
# ---------------------------------------------------------------------------
def test_no_prod_repo_marks_unsupported():
    prod = FakeProd(repos={})  # no pocket exists
    db = FakeDb()

    r = process_entry(entry(), prod, db)

    assert r.outcome == "known_unsupported"
    assert r.reason == "prod repo is missing"
    assert db.updates[-1] == (
        ("Canonical", "ubuntu-22_04-lts", "server", "eastus", "x86_64"),
        KNOWN_UNSUPPORTED,
        "prod repo is missing",
    )


# ---------------------------------------------------------------------------
# Gate 2 -> pending_publish
# ---------------------------------------------------------------------------
def test_repo_exists_but_no_aznfs_marks_pending_publish(monkeypatch):
    # A SUPPORTED distro (Ubuntu 22.04) whose prod pocket exists but has no aznfs
    # published yet -> pending_publish. The packages.csv lookup is stubbed so the
    # unit test stays offline; csv-present => "publish manually" guidance.
    monkeypatch.setattr(
        "src.phase2.orchestrator._packages_csv_mentions_distro", lambda label: True
    )
    prod = FakeProd(repos={"ubuntu": {"22.04"}}, packages={})
    db = FakeDb()

    r = process_entry(entry(), prod, db)

    assert r.outcome == "pending_publish"
    assert "publish" in r.reason.lower()
    assert db.updates[-1][1] == KNOWN_UNSUPPORTED


def test_repo_exists_unsupported_distro_marks_known_unsupported():
    # Repo exists but the distro is NOT in the AzNFS support list (e.g. Debian)
    # -> known_unsupported with the "repo found, packages missing, distro not
    # supported" reason. No packages.csv lookup happens on this path.
    prod = FakeProd(repos={"debian": {"11"}}, packages={})
    db = FakeDb()

    r = process_entry(entry(publisher="Debian", distro_label="Debian 11"), prod, db)

    assert r.outcome == "known_unsupported"
    assert "not supported by AzNFS" in r.reason
    assert db.updates[-1][1] == KNOWN_UNSUPPORTED


def test_supported_distro_missing_from_csv_marks_known_unsupported(monkeypatch):
    # Supported distro, repo exists, no package, and NOT present in packages.csv
    # -> known_unsupported: the csv needs a change + branch before Phase 2 can
    # validate it (publishing alone is not enough).
    monkeypatch.setattr(
        "src.phase2.orchestrator._packages_csv_mentions_distro", lambda label: False
    )
    prod = FakeProd(repos={"ubuntu": {"22.04"}}, packages={})
    db = FakeDb()

    r = process_entry(entry(), prod, db)

    assert r.outcome == "known_unsupported"
    assert "packages.csv" in r.reason.lower()
    assert db.updates[-1][1] == KNOWN_UNSUPPORTED


def test_aznfs_present_for_other_arch_only_is_pending_publish(monkeypatch):
    # Only arm64 published; the x86_64 image is still uncovered. Ubuntu is a
    # supported distro, so the missing-arch case is pending_publish (csv stubbed).
    monkeypatch.setattr(
        "src.phase2.orchestrator._packages_csv_mentions_distro", lambda label: True
    )
    prod = FakeProd(
        repos={"ubuntu": {"22.04"}},
        packages={("ubuntu", "22.04"): ["aznfs_0.3.2_arm64.deb"]},
    )
    db = FakeDb()

    r = process_entry(entry(architecture="x86_64"), prod, db)

    assert r.outcome == "pending_publish"
    assert db.updates[-1][1] == KNOWN_UNSUPPORTED


# ---------------------------------------------------------------------------
# Gate 3 -> trusted (no validation needed)
# ---------------------------------------------------------------------------
def test_latest_already_validated_marks_trusted():
    prod = FakeProd(
        repos={"ubuntu": {"22.04"}},
        packages={("ubuntu", "22.04"): ["aznfs_0.3.2_amd64.deb"]},
    )
    db = FakeDb()

    r = process_entry(entry(last_validated_version="0.3.2"), prod, db)

    assert r.outcome == "trusted"
    assert r.lisa_job is None
    assert "0.3.2" in r.reason
    assert db.updates[-1][1] == KNOWN_SUPPORTED


# ---------------------------------------------------------------------------
# Re-check of an already known_supported distro (tagged _db_state)
# ---------------------------------------------------------------------------
def test_recheck_known_supported_not_downgraded_on_missing_repo():
    # A re-checked known_supported distro whose prod repo is momentarily
    # unresolvable must NOT be flipped to known_unsupported -- it stays as-is.
    prod = FakeProd(repos={})  # repo momentarily missing
    db = FakeDb()

    r = process_entry(entry(_db_state="known_supported"), prod, db)

    assert r.outcome == "trusted"
    assert db.updates == []  # no downgrade written


def test_recheck_known_supported_not_downgraded_on_missing_package():
    # Repo resolves but no aznfs package is listed right now -> still no
    # downgrade for an already-supported distro.
    prod = FakeProd(repos={"ubuntu": {"22.04"}}, packages={})
    db = FakeDb()

    r = process_entry(
        entry(_db_state="known_supported", last_validated_version="0.3.458"), prod, db
    )

    assert r.outcome == "trusted"
    assert db.updates == []


def test_recheck_known_supported_newer_package_revalidates():
    # A newer prod AzNFS version than last_validated_version re-emits a LISA job
    # even though the distro is already known_supported.
    prod = FakeProd(
        repos={"ubuntu": {"22.04"}},
        packages={("ubuntu", "22.04"): ["aznfs_0.3.458_amd64.deb"]},
    )
    db = FakeDb()

    r = process_entry(
        entry(_db_state="known_supported", last_validated_version="0.3.2"), prod, db
    )

    assert r.outcome == "to_phase3"
    assert r.lisa_job["aznfs_version"] == "0.3.458"
    assert db.updates == []  # LISA path leaves state unchanged


def test_recheck_known_supported_same_package_stays_trusted():
    prod = FakeProd(
        repos={"ubuntu": {"22.04"}},
        packages={("ubuntu", "22.04"): ["aznfs_0.3.458_amd64.deb"]},
    )
    db = FakeDb()

    r = process_entry(
        entry(_db_state="known_supported", last_validated_version="0.3.458"), prod, db
    )

    assert r.outcome == "trusted"
    assert db.updates[-1][1] == KNOWN_SUPPORTED


def test_gate3_known_regressed_version_not_retested():
    # p == last_regressed_version -> already known to regress -> trusted (no re-emit).
    prod = FakeProd(
        repos={"ubuntu": {"22.04"}},
        packages={("ubuntu", "22.04"): ["aznfs_0.3.458_amd64.deb"]},
    )
    db = FakeDb()

    r = process_entry(
        entry(_db_state="known_supported", last_validated_version="0.3.400",
              last_regressed_version="0.3.458"),
        prod, db,
    )

    assert r.outcome == "trusted"
    assert r.lisa_job is None


def test_gate3_newer_than_regressed_revalidates():
    # A package strictly newer than the regressed one supersedes it -> re-validate.
    prod = FakeProd(
        repos={"ubuntu": {"22.04"}},
        packages={("ubuntu", "22.04"): ["aznfs_0.3.500_amd64.deb"]},
    )
    db = FakeDb()

    r = process_entry(
        entry(_db_state="known_supported", last_validated_version="0.3.400",
              last_regressed_version="0.3.458"),
        prod, db,
    )

    assert r.outcome == "to_phase3"
    assert r.lisa_job["aznfs_version"] == "0.3.500"


def _iso_days_ago(days: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_gate3_image_drift_after_interval_revalidates(monkeypatch):
    # Same package, but the marketplace image changed and >15d passed -> re-validate.
    monkeypatch.setenv("PHASE3_IMAGE_REVALIDATE_DAYS", "15")
    prod = FakeProd(
        repos={"ubuntu": {"22.04"}},
        packages={("ubuntu", "22.04"): ["aznfs_0.3.458_amd64.deb"]},
    )
    db = FakeDb()

    r = process_entry(
        entry(_db_state="known_supported", last_validated_version="0.3.458",
              version="22.04.202609", last_validated_image_version="22.04.202601",
              last_validated=_iso_days_ago(30)),
        prod, db,
    )

    assert r.outcome == "to_phase3"
    assert r.lisa_job["aznfs_version"] == "0.3.458"


def test_gate3_image_drift_within_interval_stays_trusted(monkeypatch):
    # Image changed but only 5 days ago (< 15) -> trusted (rate-limited).
    monkeypatch.setenv("PHASE3_IMAGE_REVALIDATE_DAYS", "15")
    prod = FakeProd(
        repos={"ubuntu": {"22.04"}},
        packages={("ubuntu", "22.04"): ["aznfs_0.3.458_amd64.deb"]},
    )
    db = FakeDb()

    r = process_entry(
        entry(_db_state="known_supported", last_validated_version="0.3.458",
              version="22.04.202609", last_validated_image_version="22.04.202601",
              last_validated=_iso_days_ago(5)),
        prod, db,
    )

    assert r.outcome == "trusted"


def test_gate3_image_unchanged_stays_trusted(monkeypatch):
    monkeypatch.setenv("PHASE3_IMAGE_REVALIDATE_DAYS", "15")
    prod = FakeProd(
        repos={"ubuntu": {"22.04"}},
        packages={("ubuntu", "22.04"): ["aznfs_0.3.458_amd64.deb"]},
    )
    db = FakeDb()

    r = process_entry(
        entry(_db_state="known_supported", last_validated_version="0.3.458",
              version="22.04.202601", last_validated_image_version="22.04.202601",
              last_validated=_iso_days_ago(30)),
        prod, db,
    )

    assert r.outcome == "trusted"


def test_gate3_image_drift_disabled_by_env(monkeypatch):
    # PHASE3_IMAGE_REVALIDATE_DAYS=0 turns image-drift re-validation off entirely.
    monkeypatch.setenv("PHASE3_IMAGE_REVALIDATE_DAYS", "0")
    prod = FakeProd(
        repos={"ubuntu": {"22.04"}},
        packages={("ubuntu", "22.04"): ["aznfs_0.3.458_amd64.deb"]},
    )
    db = FakeDb()

    r = process_entry(
        entry(_db_state="known_supported", last_validated_version="0.3.458",
              version="22.04.202609", last_validated_image_version="22.04.202601",
              last_validated=_iso_days_ago(30)),
        prod, db,
    )

    assert r.outcome == "trusted"


def test_protect_supported_via_validated_field_no_downgrade():
    # --from-db rows carry `validated` (no _db_state); a transient Gate 1 failure
    # must still NOT downgrade a known_supported row.
    prod = FakeProd(repos={})  # repo momentarily missing
    db = FakeDb()
    r = process_entry(entry(validated="known_supported"), prod, db)
    assert r.outcome == "trusted"
    assert db.updates == []


def test_protect_supported_via_validation_status_field_no_downgrade():
    # Phase 1 artifact rows carry `validation_status`; same no-downgrade guarantee.
    prod = FakeProd(repos={})
    db = FakeDb()
    r = process_entry(entry(validation_status="known_supported"), prod, db)
    assert r.outcome == "trusted"
    assert db.updates == []


def test_reset_row_with_stale_marker_is_revalidated_not_trusted():
    # After RESET_VALIDATION a row is 'unknown' but a stale last_regressed_version
    # could still match the current package. It must NOT take the known_bad trusted
    # path (that requires a supported state) -- it must re-validate.
    prod = FakeProd(
        repos={"ubuntu": {"22.04"}},
        packages={("ubuntu", "22.04"): ["aznfs_0.3.458_amd64.deb"]},
    )
    db = FakeDb()
    r = process_entry(
        entry(validated="unknown", last_validated_version="",
              last_regressed_version="0.3.458"),
        prod, db,
    )
    assert r.outcome == "to_phase3"
    assert r.lisa_job["aznfs_version"] == "0.3.458"


# ---------------------------------------------------------------------------
# Gate 3 -> to_phase3 (validation needed)
# ---------------------------------------------------------------------------
def test_first_validation_emits_lisa_job():
    prod = FakeProd(
        repos={"ubuntu": {"22.04"}},
        packages={("ubuntu", "22.04"): ["aznfs_0.3.2_amd64.deb"]},
    )
    db = FakeDb()

    r = process_entry(entry(last_validated_version=""), prod, db)

    assert r.outcome == "to_phase3"
    assert r.lisa_job is not None
    job = r.lisa_job
    # Field names match Phase 3's LisaJob dataclass exactly (consumed directly).
    assert job["aznfs_version"] == "0.3.2"
    assert job["image"] == "ubuntu-22_04-lts"
    assert job["version"] == "22.04.202506"
    assert job["arch"] == "x86_64"
    assert job["distro_label"] == "Ubuntu 22.04"
    assert job["aznfs_package_url"].startswith("https://")
    assert job["aznfs_package_url"].endswith("/ubuntu/22.04/prod/pool/main/a/aznfs/aznfs_0.3.2_amd64.deb")
    # Dropped legacy fields must NOT appear (Phase 3 would ignore them anyway).
    assert "distro_info" not in job
    assert "download_url" not in job
    assert "repository" not in job
    assert db.updates == []  # LISA path leaves validation_state unchanged


def test_newer_prod_version_than_db_needs_validation_picks_numeric_max():
    # 0.3.18 must beat 0.3.9 (numeric, not lexical) and beat the DB's 0.3.9.
    prod = FakeProd(
        repos={"ubuntu": {"22.04"}},
        packages={("ubuntu", "22.04"): [
            "aznfs_0.3.9_amd64.deb",
            "aznfs_0.3.18_amd64.deb",
        ]},
    )
    db = FakeDb()

    r = process_entry(entry(last_validated_version="0.3.9"), prod, db)

    assert r.outcome == "to_phase3"
    assert r.lisa_job["aznfs_version"] == "0.3.18"
    assert db.updates == []  # LISA path leaves validation_state unchanged


def test_series_filter_ignores_non_0_3_lineages():
    # Prod carries 0.3.x AND newer 1.x/2.x/3.x lineages; only 0.3.x is tracked,
    # so the latest must be 0.3.458 (numeric-max within the series), never 3.0.18.
    prod = FakeProd(
        repos={"ubuntu": {"22.04"}},
        packages={("ubuntu", "22.04"): [
            "aznfs_0.3.46_amd64.deb",
            "aznfs_0.3.458_amd64.deb",
            "aznfs_1.0.4_amd64.deb",
            "aznfs_2.1.3_amd64.deb",
            "aznfs_3.0.18_amd64.deb",
        ]},
    )
    db = FakeDb()

    r = process_entry(entry(last_validated_version="0.3.46"), prod, db)

    assert r.outcome == "to_phase3"
    assert r.lisa_job["aznfs_version"] == "0.3.458"
    assert r.lisa_job["aznfs_package_url"].endswith("aznfs_0.3.458_amd64.deb")


def test_only_non_series_builds_is_pending_publish(monkeypatch):
    # Repo exists and aznfs is published, but only on the untracked 3.0.x/1.x line.
    # Ubuntu is supported, so the (no in-series build) case is pending_publish.
    monkeypatch.setattr(
        "src.phase2.orchestrator._packages_csv_mentions_distro", lambda label: True
    )
    prod = FakeProd(
        repos={"ubuntu": {"22.04"}},
        packages={("ubuntu", "22.04"): [
            "aznfs_3.0.18_amd64.deb",
            "aznfs_1.0.4_amd64.deb",
        ]},
    )
    db = FakeDb()

    r = process_entry(entry(architecture="x86_64"), prod, db)

    assert r.outcome == "pending_publish"
    assert db.updates[-1][1] == KNOWN_UNSUPPORTED


def test_yum_x0_release_probes_both_pockets_and_maps_arch():
    # RHEL 9.0 is a publish target, so /rhel/9/ and /rhel/9.0/ are both tried;
    # the x86_64 image maps to the x86_64 rpm.
    prod = FakeProd(
        repos={"rhel": {"9"}},
        packages={("rhel", "9"): [
            "aznfs-0.3.2-1.x86_64.rpm",
            "aznfs-0.3.2-1.aarch64.rpm",
        ]},
    )
    db = FakeDb()

    r = process_entry(
        entry(publisher="RedHat", distro_label="RHEL 9.0", family="yum",
              architecture="x86_64", image="RHEL", sku="9-lvm"),
        prod, db,
    )

    assert r.outcome == "to_phase3"
    assert r.lisa_job["aznfs_version"] == "0.3.2"
    assert r.lisa_job["arch"] == "x86_64"
    assert r.lisa_job["aznfs_package_url"].endswith("/rhel/9/prod/Packages/a/aznfs-0.3.2-1.x86_64.rpm")


# ---------------------------------------------------------------------------
# run_phase2 aggregation -> exactly one summary mail with every distro + reason
# ---------------------------------------------------------------------------
def test_run_phase2_buckets_writes_jobs_and_single_summary(tmp_path, monkeypatch):
    # RHEL 9 is supported but unpublished -> pending_publish (csv stubbed present).
    monkeypatch.setattr(
        "src.phase2.orchestrator._packages_csv_mentions_distro", lambda label: True
    )
    prod = FakeProd(
        repos={"ubuntu": {"22.04"}, "debian": {"11"}, "rhel": {"9"}},
        packages={("ubuntu", "22.04"): ["aznfs_0.3.2_amd64.deb"]},
    )
    db, notifier = FakeDb(), FakeNotifier()
    out = tmp_path / "lisa_jobs.json"

    entries = [
        entry(last_validated_version=""),                                   # to_phase3
        entry(distro_label="Ubuntu 22.04", last_validated_version="0.3.2",
              sku="other"),                                                  # trusted
        entry(publisher="RedHat", distro_label="RHEL 9", family="yum",
              image="RHEL", sku="9-lvm"),                                    # pending_publish (supported, no pkg)
        entry(publisher="Debian", distro_label="Debian 11", sku="deb"),     # known_unsupported (repo exists, AzNFS-unsupported)
        entry(publisher="BellLabs", distro_label="Plan9 4", sku="x"),       # known_unsupported (no prod repo)
    ]

    jobs = run_phase2(entries, prod, db, notifier, lisa_jobs_path=str(out))

    assert len(jobs) == 1
    # Exactly one mail (the summary) for the whole run.
    assert len(notifier.summaries) == 1
    s = notifier.summaries[-1]
    assert s["processed"] == 5
    # Buckets are now lists of dicts (one table per outcome, arch its own column).
    assert [r["label"] for r in s["to_phase3"]] == ["Ubuntu 22.04"]
    assert s["to_phase3"][0]["arch"] == "x86_64"
    assert s["to_phase3"][0]["url"].endswith("aznfs_0.3.2_amd64.deb")
    assert [r["label"] for r in s["trusted"]] == ["Ubuntu 22.04"]
    assert [r["label"] for r in s["pending_publish"]] == ["RHEL 9"]
    assert "publish" in s["pending_publish"][0]["reason"].lower()
    # Debian (repo exists but AzNFS-unsupported) + Plan9 (no repo) are both unsupported.
    unsupported = {r["label"]: r["reason"] for r in s["unsupported"]}
    assert sorted(unsupported) == ["Debian 11", "Plan9 4"]
    assert "not supported by AzNFS" in unsupported["Debian 11"]
    assert unsupported["Plan9 4"] == "prod repo is missing"
    assert s["errors"] == []

    written = json.loads(out.read_text())
    assert written[0]["aznfs_package_url"].endswith("aznfs_0.3.2_amd64.deb")


def test_run_phase2_swallows_per_entry_errors_into_summary():
    prod = FakeProd(raise_on_resolve=True)
    db, notifier = FakeDb(), FakeNotifier()

    jobs = run_phase2([entry()], prod, db, notifier, lisa_jobs_path=None)

    assert jobs == []
    # Still exactly one summary; the failed entry shows up in the errors bucket.
    assert len(notifier.summaries) == 1
    s = notifier.summaries[-1]
    assert s["processed"] == 1
    assert s["errors"] and "error" in s["errors"][0][1].lower()


def test_run_phase2_no_entries_sends_no_email():
    # Like Phase 1 (silent with no new distros), Phase 2 must NOT e-mail when
    # there are no entries to process (empty Phase 1 hand-off).
    prod = FakeProd()
    db, notifier = FakeDb(), FakeNotifier()

    jobs = run_phase2([], prod, db, notifier, lisa_jobs_path=None)

    assert jobs == []
    assert notifier.summaries == []  # no e-mail


def test_run_phase2_dedups_jobs_by_url_keeping_latest_version(tmp_path):
    # Many RHEL 9 SKUs (9.0 .. 9.8) all resolve to rhel/9 -> ONE x86 job
    # (the latest marketplace version) + ONE arm64 job (distinct url). Rocky 8
    # and Rocky 9 are distinct urls -> two more jobs. Goal: distinct urls only.
    prod = FakeProd(
        repos={"rhel": {"9"}, "rocky": {"8", "9"}},
        packages={
            ("rhel", "9"): ["aznfs-0.3.458-1.x86_64.rpm", "aznfs-0.3.458-1.aarch64.rpm"],
            ("rocky", "8"): ["aznfs-0.3.458-1.x86_64.rpm"],
            ("rocky", "9"): ["aznfs-0.3.458-1.x86_64.rpm"],
        },
    )
    db, notifier = FakeDb(), FakeNotifier()
    out = tmp_path / "lisa_jobs.json"

    def c(**kw):
        base = dict(publisher="RedHat", image="RHEL", family="yum",
                    region="eastus", architecture="x86_64", distro_label="RHEL 9",
                    last_validated_version="")
        base.update(kw)
        return base

    def rk(**kw):
        base = dict(publisher="resf", image="rockylinux-x86_64", family="yum",
                    region="eastus", architecture="x86_64", last_validated_version="")
        base.update(kw)
        return base

    entries = [
        c(sku="9_0", version="9.0.2022010100"),
        c(sku="9_8", version="9.8.2026010100"),
        c(sku="9-lvm-gen2", version="9.8.2026062413"),             # latest x86 -> representative
        c(sku="9-arm64", version="9.8.2026070101", architecture="arm64"),
        rk(sku="8-base", version="8.9.20231119", distro_label="Rocky 8"),
        rk(sku="9-base", version="9.6.20250531", distro_label="Rocky 9"),
    ]

    jobs = run_phase2(entries, prod, db, notifier, lisa_jobs_path=str(out))

    urls = [j["aznfs_package_url"] for j in jobs]
    assert len(urls) == len(set(urls))            # every url distinct
    assert len(jobs) == 4                         # RHEL9 x86, RHEL9 arm, Rocky8, Rocky9
    rhel = [j for j in jobs if j["distro_label"] == "RHEL 9"]
    x86 = next(j for j in rhel if j["arch"] == "x86_64")
    assert x86["version"] == "9.8.2026062413"     # latest version kept
    assert {j["arch"] for j in rhel} == {"x86_64", "arm64"}
    assert any(j["aznfs_package_url"].endswith("/rocky/8/prod/Packages/a/aznfs-0.3.458-1.x86_64.rpm") for j in jobs)
    assert any(j["aznfs_package_url"].endswith("/rocky/9/prod/Packages/a/aznfs-0.3.458-1.x86_64.rpm") for j in jobs)
    # The summary's to_phase3 table mirrors the deduped jobs (one row per url).
    assert len(notifier.summaries[-1]["to_phase3"]) == 4


def _at(day, hour=0):
    return datetime(2026, 9, day, hour, tzinfo=timezone.utc)


def test_gate3_picks_the_latest_published_not_the_highest_number():
    # Real PMC ordering: 0.3.49 shipped AFTER 0.3.459, so a numeric max would
    # keep validating 0.3.459 and never notice the build that superseded it.
    prod = FakeProd(
        repos={"rhel": {"9"}},
        packages={("rhel", "9"): [
            "aznfs-0.3.459-1.x86_64.rpm",
            "aznfs-0.3.49-1.x86_64.rpm",
        ]},
        published={
            "aznfs-0.3.459-1.x86_64.rpm": _at(2, 7),
            "aznfs-0.3.49-1.x86_64.rpm": _at(2, 14),
        },
    )

    r = process_entry(
        entry(publisher="RedHat", distro_label="RHEL 9.0", family="yum",
              architecture="x86_64", image="RHEL", sku="9-lvm"),
        prod, FakeDb(),
    )

    assert r.outcome == "to_phase3"
    assert r.lisa_job["aznfs_version"] == "0.3.49"


def test_a_lower_version_published_later_still_triggers_validation():
    # Already validated 0.3.459; PMC then published 0.3.49. The package CHANGED
    # even though the number went down, so it must be re-validated.
    prod = FakeProd(
        repos={"rhel": {"9"}},
        packages={("rhel", "9"): [
            "aznfs-0.3.459-1.x86_64.rpm",
            "aznfs-0.3.49-1.x86_64.rpm",
        ]},
        published={
            "aznfs-0.3.459-1.x86_64.rpm": _at(2, 7),
            "aznfs-0.3.49-1.x86_64.rpm": _at(2, 14),
        },
    )

    r = process_entry(
        entry(publisher="RedHat", distro_label="RHEL 9.0", family="yum",
              architecture="x86_64", image="RHEL", sku="9-lvm",
              last_validated_version="0.3.459"),
        prod, FakeDb(),
    )

    assert r.outcome == "to_phase3"
    assert r.lisa_job["aznfs_version"] == "0.3.49"


def test_unchanged_latest_publication_stays_trusted():
    prod = FakeProd(
        repos={"rhel": {"9"}},
        packages={("rhel", "9"): ["aznfs-0.3.49-1.x86_64.rpm"]},
        published={"aznfs-0.3.49-1.x86_64.rpm": _at(2, 14)},
    )

    r = process_entry(
        entry(publisher="RedHat", distro_label="RHEL 9.0", family="yum",
              architecture="x86_64", image="RHEL", sku="9-lvm",
              last_validated_version="0.3.49"),
        prod, FakeDb(),
    )

    assert r.outcome == "trusted"


def test_pocket_choice_uses_publication_time_not_version_number():
    # /rhel/9/ has the higher NUMBER, /rhel/9.0/ has the newer PUBLICATION.
    prod = FakeProd(
        repos={"rhel": {"9", "9.0"}},
        packages={
            ("rhel", "9"): ["aznfs-0.3.459-1.x86_64.rpm"],
            ("rhel", "9.0"): ["aznfs-0.3.49-1.x86_64.rpm"],
        },
        published={
            "aznfs-0.3.459-1.x86_64.rpm": _at(2, 7),
            "aznfs-0.3.49-1.x86_64.rpm": _at(3, 11),
        },
    )

    r = process_entry(
        entry(publisher="RedHat", distro_label="RHEL 9.0", family="yum",
              architecture="x86_64", image="RHEL", sku="9-lvm"),
        prod, FakeDb(),
    )

    assert r.lisa_job["aznfs_package_url"].endswith(
        "/rhel/9.0/prod/Packages/a/aznfs-0.3.49-1.x86_64.rpm")


def test_unreachable_pmc_records_no_verdict_and_is_reported_as_an_error():
    # A timeout is not a 404: absence is unproven, so the stored verdict stands.
    class ExplodingProd:
        def resolve_repo(self, distro, candidates, family=""):
            raise pmc_packages.ProbeError("read timed out")

        def list_packages(self, distro, version, family):  # pragma: no cover
            raise AssertionError("not reached")

    db, notifier = FakeDb(), FakeNotifier()

    run_phase2([entry(distro_label="Ubuntu 24.04")], ExplodingProd(), db, notifier)

    assert db.updates == []  # the stored verdict is left untouched
    summary = notifier.summaries[-1]
    assert summary["unsupported"] == []
    assert "PMC unreachable" in summary["errors"][0][1]
def test_phase2_tags_its_verdicts_as_gate_decisions():
    # The tag is what lets a later run re-check this cheap lookup instead of
    # treating it as a permanent verdict.
    prod = FakeProd(repos={}, packages={})
    db = FakeDb()

    r = process_entry(entry(distro_label="Ubuntu 24.04"), prod, db)

    assert r.outcome == "known_unsupported"
    assert db.sources == ["gate"]
