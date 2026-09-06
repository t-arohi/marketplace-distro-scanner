from __future__ import annotations

import pathlib
import sqlite3

import pytest

from phase3.orchestrator import record_result
from phase3 import run_phase3


# ---------------------------------------------------------------------------
# load_jobs: keeps only LisaJob fields (drops Phase 2 extras / legacy `repo`)
# ---------------------------------------------------------------------------
def test_load_jobs_filters_unknown_fields(tmp_path):
    p = tmp_path / "jobs.json"
    p.write_text(
        '[{"publisher":"redhat","image":"rhel","sku":"9_5","version":"latest",'
        '"region":"eastus","arch":"x86_64","distro_label":"RHEL 9.5",'
        '"aznfs_package_url":"https://x/aznfs-0.3.458-1.x86_64.rpm",'
        '"aznfs_version":"0.3.458","repo":"legacy-should-be-dropped"}]'
    )
    jobs = record_result.load_jobs(str(p))
    assert len(jobs) == 1
    j = jobs[0]
    assert j.image == "rhel" and j.version == "latest" and j.arch == "x86_64"
    assert j.aznfs_package_url.endswith("aznfs-0.3.458-1.x86_64.rpm")
    assert not hasattr(j, "repo")  # dropped field never becomes an attribute


def test_image_key_is_the_five_key_identity():
    j = record_result.LisaJob(
        publisher="redhat", image="rhel", sku="9_5", version="latest",
        region="eastus", arch="x86_64",
    )
    assert j.image_key() == {
        "publisher": "redhat", "image": "rhel", "sku": "9_5",
        "region": "eastus", "architecture": "x86_64",
    }


# ---------------------------------------------------------------------------
# _record_validation: matches on the 5-key identity (publisher/image/sku/region/arch)
# ---------------------------------------------------------------------------
def _make_db(tmp_path):
    db = tmp_path / "marketplace.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE images (
            publisher TEXT, image TEXT, sku TEXT, version TEXT, region TEXT,
            architecture TEXT, validated TEXT, last_modified TEXT, last_validated TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO images VALUES (?,?,?,?,?,?,?,?,?)",
        ("redhat", "rhel", "9_5", "9.5.20240101", "eastus", "x86_64",
         "pending_validation", "t0", None),
    )
    conn.commit()
    conn.close()
    return db


def test_record_validation_updates_matching_row(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    monkeypatch.setattr(record_result.config, "DB_PATH", str(db))
    monkeypatch.setattr(record_result.config, "PHASE3_SCHEMA_PATH", "/nonexistent.sql")

    record_result._record_validation(
        {"publisher": "redhat", "image": "rhel", "sku": "9_5",
         "region": "eastus", "architecture": "x86_64"},
        "known_supported",
    )

    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT validated, last_validated FROM images"
    ).fetchone()
    conn.close()
    assert row[0] == "known_supported"
    assert row[1] is not None  # last_validated stamped


def test_record_validation_stores_reason(tmp_path, monkeypatch):
    # _make_db's schema has NO reason column, so this also exercises the
    # idempotent _ensure_phase3_columns auto-migration.
    db = _make_db(tmp_path)
    monkeypatch.setattr(record_result.config, "DB_PATH", str(db))
    monkeypatch.setattr(record_result.config, "PHASE3_SCHEMA_PATH", "/nonexistent.sql")

    record_result._record_validation(
        {"publisher": "redhat", "image": "rhel", "sku": "9_5",
         "region": "eastus", "architecture": "x86_64"},
        "known_unsupported",
        reason="[Tier 4: mount] failed to mount via aznfs",
    )

    conn = sqlite3.connect(str(db))
    reason = conn.execute("SELECT reason FROM images").fetchone()[0]
    conn.close()
    assert reason == "[Tier 4: mount] failed to mount via aznfs"


def test_record_validation_stamps_last_validated_version_on_pass(tmp_path, monkeypatch):
    # _make_db's schema has NO last_validated_version column, so this also
    # exercises the idempotent _ensure_phase3_columns auto-migration. Recording
    # the version is what lets Phase 2 Gate 3 skip re-validating the same package.
    db = _make_db(tmp_path)
    monkeypatch.setattr(record_result.config, "DB_PATH", str(db))
    monkeypatch.setattr(record_result.config, "PHASE3_SCHEMA_PATH", "/nonexistent.sql")

    record_result._record_validation(
        {"publisher": "redhat", "image": "rhel", "sku": "9_5",
         "region": "eastus", "architecture": "x86_64"},
        "known_supported",
        last_validated_version="0.3.458",
    )

    conn = sqlite3.connect(str(db))
    row = conn.execute("SELECT validated, last_validated_version FROM images").fetchone()
    conn.close()
    assert row[0] == "known_supported"
    assert row[1] == "0.3.458"


def test_process_job_pass_records_validated_version(tmp_path, monkeypatch):
    # A LISA pass must stamp the validated AzNFS version so the next Phase 2 run
    # (which now re-checks known_supported) stays trusted until a NEWER package.
    db = _make_db(tmp_path)
    monkeypatch.setattr(record_result.config, "DB_PATH", str(db))
    monkeypatch.setattr(record_result.config, "PHASE3_SCHEMA_PATH", "/nonexistent.sql")

    job = record_result.LisaJob(
        publisher="redhat", image="rhel", sku="9_5", version="latest",
        region="eastus", arch="x86_64", distro_label="RHEL 9.5",
        aznfs_version="0.3.458", lisa_passed=True,
    )
    state, _ = record_result.process_job(job)

    assert state == "known_supported"
    conn = sqlite3.connect(str(db))
    v = conn.execute("SELECT last_validated_version FROM images").fetchone()[0]
    conn.close()
    assert v == "0.3.458"


def test_process_job_regression_keeps_known_supported(tmp_path, monkeypatch):
    # A newer package FAILING on an already-supported distro is a REGRESSION:
    # the distro stays known_supported, last_validated_version + the validated
    # image version stay at the last GOOD ones, and the failing version is recorded
    # in last_regressed_version (so it is not re-tested) -- NOT demoted.
    db = _make_db(tmp_path)
    monkeypatch.setattr(record_result.config, "DB_PATH", str(db))
    monkeypatch.setattr(record_result.config, "PHASE3_SCHEMA_PATH", "/nonexistent.sql")

    key = dict(publisher="redhat", image="rhel", sku="9_5", version="9.5.20260101",
               region="eastus", arch="x86_64", distro_label="RHEL 9.5")
    # First a good package passes -> known_supported at 0.3.400 on image 9.5.20260101.
    record_result.process_job(record_result.LisaJob(**key, aznfs_version="0.3.400", lisa_passed=True))
    # Then a newer package fails on a newer image -> regression.
    state, reason = record_result.process_job(
        record_result.LisaJob(**{**key, "version": "9.5.20260201"},
                              aznfs_version="0.3.458", lisa_passed=False,
                              failure_reason="[Tier 4: mount] failed")
    )

    assert state == "regression"
    assert "0.3.458 regressed" in reason and "0.3.400" in reason
    conn = sqlite3.connect(str(db))
    validated, good, regressed, img = conn.execute(
        "SELECT validated, last_validated_version, last_regressed_version, "
        "last_validated_image_version FROM images"
    ).fetchone()
    conn.close()
    assert validated == "known_supported"      # NOT demoted
    assert good == "0.3.400"                    # last GOOD package kept
    assert regressed == "0.3.458"               # failing version recorded (won't re-test)
    assert img == "9.5.20260101"                # last GOOD image kept (regression didn't touch it)


def test_process_job_pass_records_validated_image_version(tmp_path, monkeypatch):
    # A pass records the marketplace image it validated on (for image-drift re-check).
    db = _make_db(tmp_path)
    monkeypatch.setattr(record_result.config, "DB_PATH", str(db))
    monkeypatch.setattr(record_result.config, "PHASE3_SCHEMA_PATH", "/nonexistent.sql")

    record_result.process_job(record_result.LisaJob(
        publisher="redhat", image="rhel", sku="9_5", version="9.5.20260101",
        region="eastus", arch="x86_64", distro_label="RHEL 9.5",
        aznfs_version="0.3.458", lisa_passed=True,
    ))

    conn = sqlite3.connect(str(db))
    img = conn.execute("SELECT last_validated_image_version FROM images").fetchone()[0]
    conn.close()
    assert img == "9.5.20260101"


def test_process_job_image_regression_same_package_new_image(tmp_path, monkeypatch):
    # The SAME AzNFS package that previously passed now fails on a NEWER image ->
    # IMAGE regression (not package). Stays known_supported; the DB signal is
    # last_regressed_version == last_validated_version.
    db = _make_db(tmp_path)
    monkeypatch.setattr(record_result.config, "DB_PATH", str(db))
    monkeypatch.setattr(record_result.config, "PHASE3_SCHEMA_PATH", "/nonexistent.sql")

    key = dict(publisher="redhat", image="rhel", sku="9_5",
               region="eastus", arch="x86_64", distro_label="RHEL 9.5")
    record_result.process_job(record_result.LisaJob(**key, version="9.5.20260101",
                                                    aznfs_version="0.3.458", lisa_passed=True))
    state, reason = record_result.process_job(
        record_result.LisaJob(**key, version="9.5.20260201",
                              aznfs_version="0.3.458", lisa_passed=False,
                              failure_reason="[Tier 4: mount] failed")
    )

    assert state == "regression"
    assert "image regression" in reason and "9.5.20260201" in reason
    conn = sqlite3.connect(str(db))
    validated, good, regressed, img = conn.execute(
        "SELECT validated, last_validated_version, last_regressed_version, "
        "last_validated_image_version FROM images"
    ).fetchone()
    conn.close()
    assert validated == "known_supported"
    assert good == "0.3.458" and regressed == "0.3.458"   # regressed == good -> image regression
    assert img == "9.5.20260101"                          # good image kept


def test_process_job_pass_clears_regressed_marker(tmp_path, monkeypatch):
    # After a regression, a newer package that PASSES clears the regression marker
    # and advances the good baseline (auto-recovery).
    db = _make_db(tmp_path)
    monkeypatch.setattr(record_result.config, "DB_PATH", str(db))
    monkeypatch.setattr(record_result.config, "PHASE3_SCHEMA_PATH", "/nonexistent.sql")

    key = dict(publisher="redhat", image="rhel", sku="9_5", version="latest",
               region="eastus", arch="x86_64", distro_label="RHEL 9.5")
    record_result.process_job(record_result.LisaJob(**key, aznfs_version="0.3.400", lisa_passed=True))
    record_result.process_job(record_result.LisaJob(**key, aznfs_version="0.3.458", lisa_passed=False,
                                                     failure_reason="x"))
    record_result.process_job(record_result.LisaJob(**key, aznfs_version="0.3.500", lisa_passed=True))

    conn = sqlite3.connect(str(db))
    validated, good, regressed = conn.execute(
        "SELECT validated, last_validated_version, last_regressed_version FROM images"
    ).fetchone()
    conn.close()
    assert validated == "known_supported"
    assert good == "0.3.500"      # baseline advanced to the new good version
    assert regressed == ""        # regression marker cleared


def test_process_job_first_failure_is_known_unsupported(tmp_path, monkeypatch):
    # A failure on a distro that was NOT already supported is a real
    # known_unsupported (the regression path only protects supported distros).
    db = _make_db(tmp_path)  # row starts pending_validation
    monkeypatch.setattr(record_result.config, "DB_PATH", str(db))
    monkeypatch.setattr(record_result.config, "PHASE3_SCHEMA_PATH", "/nonexistent.sql")

    state, _ = record_result.process_job(
        record_result.LisaJob(
            publisher="redhat", image="rhel", sku="9_5", version="latest",
            region="eastus", arch="x86_64", distro_label="RHEL 9.5",
            aznfs_version="0.3.458", lisa_passed=False, failure_reason="[Tier 2: install] failed",
        )
    )

    assert state == "known_unsupported"
    conn = sqlite3.connect(str(db))
    validated = conn.execute("SELECT validated FROM images").fetchone()[0]
    conn.close()
    assert validated == "known_unsupported"


# ---------------------------------------------------------------------------
# run(): one summary e-mail; pass -> supported, fail -> unsupported + reason
# ---------------------------------------------------------------------------
def test_run_sends_single_summary_with_reasons(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    monkeypatch.setattr(record_result.config, "DB_PATH", str(db))
    monkeypatch.setattr(record_result.config, "PHASE3_SCHEMA_PATH", "/nonexistent.sql")

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        record_result, "_notify",
        lambda s, b, html_body=None: sent.append((s, b)),
    )

    jobs = [
        record_result.LisaJob(
            publisher="redhat", image="rhel", sku="9_5", version="latest",
            region="eastus", arch="x86_64", distro_label="RHEL 9.5",
            lisa_passed=True,
        ),
        record_result.LisaJob(
            publisher="suse", image="sles", sku="15-sp5", version="latest",
            region="eastus", arch="x86_64", distro_label="SLES 15.5",
            lisa_passed=False, failure_reason="[Tier 4: mount] failed to mount ... via aznfs",
        ),
    ]
    summary = record_result.run(jobs)

    assert summary == {"known_supported": 1, "known_unsupported": 1,
                       "regressions": 0, "infra_errors": 0}
    assert len(sent) == 1  # exactly ONE e-mail for the whole run
    subject, body = sent[0]
    assert "1 supported, 1 unsupported" in subject
    # Table a) pass: distro + arch column.
    assert "Validation successful (known_supported)" in body
    assert "RHEL 9.5" in body
    assert "arch=x86_64" in body
    # Table b) fail: distro + failing tier reason + URN.
    assert "Validation fails (kept in known_unsupported)" in body
    assert "SLES 15.5" in body
    assert "[Tier 4: mount] failed to mount" in body
    assert "suse:sles:15-sp5" in body


def test_run_no_jobs_sends_no_email(monkeypatch):
    # Like Phase 1 (silent with no new distros), Phase 3 must NOT e-mail when
    # there is nothing to validate.
    sent: list = []
    monkeypatch.setattr(
        record_result, "_notify",
        lambda s, b, html_body=None: sent.append((s, b)),
    )
    summary = record_result.run([])
    assert summary == {"known_supported": 0, "known_unsupported": 0,
                       "regressions": 0, "infra_errors": 0}
    assert sent == []  # no e-mail


# ---------------------------------------------------------------------------
# _parse_junit: extracts the failing tier from the failure message
# ---------------------------------------------------------------------------
def test_parse_junit_extracts_tier_reason(tmp_path):
    xml = tmp_path / "lisa.junit.xml"
    xml.write_text(
        '<testsuites><testsuite tests="3" failures="1" errors="0" skipped="0">'
        '<testcase name="verify_aznfs_install_lifecycle"/>'
        '<testcase name="verify_aznfs_nfs_functional">'
        '<failure message="[Tier 4: mount] failed to mount src via aznfs">trace</failure>'
        '</testcase>'
        '<testcase name="verify_aznfs_resilience"/>'
        '</testsuite></testsuites>'
    )
    total, failed, skipped, reason = run_phase3._parse_junit(xml)
    assert (total, failed, skipped) == (3, 1, 0)
    assert reason == "[Tier 4: mount] failed to mount src via aznfs"


def test_parse_junit_clean_pass_has_no_reason(tmp_path):
    xml = tmp_path / "lisa.junit.xml"
    xml.write_text(
        '<testsuite tests="3" failures="0" errors="0" skipped="0">'
        '<testcase name="verify_aznfs_install_lifecycle"/>'
        '</testsuite>'
    )
    total, failed, skipped, reason = run_phase3._parse_junit(xml)
    assert (total, failed, skipped, reason) == (3, 0, 0, "")


# The exact string a real run wrote into the DB for Rocky 8 arm64: the VM never
# deployed because the subscription had not accepted the marketplace terms.
_REAL_DEPLOY_FAILURE = (
    "verify_aznfs_install_lifecycle (lisa_0_0): deployment failed. "
    "HttpResponseError: (AuthorizationFailed) does not have permission to "
    "perform action 'Microsoft.MarketplaceOrdering/offertypes/.../agreements/write'"
)


@pytest.mark.parametrize(
    "reason",
    [
        _REAL_DEPLOY_FAILURE,
        "driver/infra error: LISA produced no junit (exit=1)",
        "QuotaExceeded: not enough vCPUs in centralindia",
        "[Tier 4: mount] failed to connect to node",
    ],
)
def test_infrastructure_failures_are_classified_as_untestable(reason):
    assert run_phase3._is_infra_failure(reason)


@pytest.mark.parametrize(
    "reason",
    [
        "[Tier 2: install] aznfs install command failed",
        "[Tier 3: footprint] aznfs registered at the wrong version",
        "verify_aznfs_install_lifecycle (lisa_0_0): failed. AssertionError: "
        "[Failed to uninstall ['aznfs']]",
    ],
)
def test_real_aznfs_failures_are_still_verdicts(reason):
    assert not run_phase3._is_infra_failure(reason)


def test_untestable_distro_keeps_its_verdict_and_is_flagged_for_retry(
    tmp_path, monkeypatch
):
    # An Azure problem must never retire a distro: the row keeps whatever state
    # it had, and only the retry marker is written.
    db = _make_db(tmp_path)
    monkeypatch.setattr(record_result.config, "DB_PATH", str(db))
    monkeypatch.setattr(record_result.config, "PHASE3_SCHEMA_PATH", "/nonexistent.sql")

    job = record_result.LisaJob(
        publisher="redhat", image="rhel", sku="9_5", version="9.5.20240101",
        region="eastus", arch="x86_64", lisa_passed=False, infra_error=True,
        failure_reason=_REAL_DEPLOY_FAILURE,
    )
    state, _ = record_result.process_job(job)

    conn = sqlite3.connect(str(db))
    validated, source = conn.execute(
        "SELECT validated, verdict_source FROM images"
    ).fetchone()
    conn.close()

    assert state == "infra_error"
    assert validated == "pending_validation"  # unchanged, NOT known_unsupported
    assert source == record_result.INFRA_ERROR


def test_a_genuine_test_failure_still_records_known_unsupported(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    monkeypatch.setattr(record_result.config, "DB_PATH", str(db))
    monkeypatch.setattr(record_result.config, "PHASE3_SCHEMA_PATH", "/nonexistent.sql")

    job = record_result.LisaJob(
        publisher="redhat", image="rhel", sku="9_5", version="9.5.20240101",
        region="eastus", arch="x86_64", lisa_passed=False,
        failure_reason="[Tier 2: install] aznfs install command failed",
    )
    state, _ = record_result.process_job(job)

    conn = sqlite3.connect(str(db))
    validated, source = conn.execute(
        "SELECT validated, verdict_source FROM images"
    ).fetchone()
    conn.close()

    assert state == "known_unsupported"
    assert validated == "known_unsupported"
    assert source == record_result.LISA_VERDICT


def _junit(tmp_path, tests, failures=0, skipped=0, message=""):
    case = (f'<testcase name="verify_aznfs_install_lifecycle">'
            f'<failure message="{message}"/></testcase>') if failures else ""
    xml = (f'<testsuite tests="{tests}" failures="{failures}" errors="0" '
           f'skipped="{skipped}">{case}</testsuite>')
    path = tmp_path / "lisa.junit.xml"
    path.write_text(xml)
    return path


def test_all_cases_skipped_is_a_verdict_not_an_endless_retry(tmp_path, monkeypatch):
    # Cases that ran and skipped are a real answer; retrying would burn a VM
    # on every run for ever.
    junit = _junit(tmp_path, tests=3, failures=0, skipped=3)
    monkeypatch.setattr(run_phase3, "_run_lisa", lambda *a, **k: junit)

    job = record_result.LisaJob(publisher="p", image="i", sku="s", version="v",
                                region="r")
    run_phase3._validate_one(job, "sub", 1)

    assert not job.lisa_passed
    assert not job.infra_error


def test_environment_producing_no_cases_is_an_infra_error(tmp_path, monkeypatch):
    junit = _junit(tmp_path, tests=0)
    monkeypatch.setattr(run_phase3, "_run_lisa", lambda *a, **k: junit)

    job = record_result.LisaJob(publisher="p", image="i", sku="s", version="v",
                                region="r")
    run_phase3._validate_one(job, "sub", 1)

    assert job.infra_error


def test_infra_error_does_not_overwrite_a_real_verdict_reason(tmp_path, monkeypatch):
    # reason is the detail behind a verdict. Writing an Azure error there would
    # leave the row asserting known_unsupported for a reason that is not why.
    db = _make_db(tmp_path)
    monkeypatch.setattr(record_result.config, "DB_PATH", str(db))
    monkeypatch.setattr(record_result.config, "PHASE3_SCHEMA_PATH", "/nonexistent.sql")

    key = {"publisher": "redhat", "image": "rhel", "sku": "9_5",
           "region": "eastus", "architecture": "x86_64"}
    record_result._record_validation(key, "known_unsupported",
                                     reason="prod repo is missing")

    record_result._mark_infra_error(key, _REAL_DEPLOY_FAILURE)

    conn = sqlite3.connect(str(db))
    validated, reason, source = conn.execute(
        "SELECT validated, reason, verdict_source FROM images"
    ).fetchone()
    conn.close()

    assert validated == "known_unsupported"
    assert reason == "prod repo is missing"  # not the deploy error
    assert source == record_result.INFRA_ERROR


def test_migrated_columns_match_the_canonical_schema(tmp_path, monkeypatch):
    # Phase 3 migrates independently of db_manager, so its column definitions
    # have to agree with db/schema.sql or the two paths build different tables.
    db = _make_db(tmp_path)  # deliberately missing the Phase 3 columns
    monkeypatch.setattr(record_result.config, "DB_PATH", str(db))

    conn = sqlite3.connect(str(db))
    record_result._ensure_phase3_columns(conn)
    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(images)")}
    conn.close()

    canonical = pathlib.Path("db/schema.sql").read_text()
    for name in ("last_checked", "reason", "verdict_source",
                 "last_validated_version", "last_regressed_version"):
        notnull, default = cols[name][3], cols[name][4]
        if f"{name} " in canonical and "NOT NULL" in canonical.split(name, 1)[1][:40]:
            assert notnull == 1, f"{name} should be NOT NULL like db/schema.sql"
            assert default is not None, f"{name} needs a default to migrate"
