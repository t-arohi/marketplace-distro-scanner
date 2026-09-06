from __future__ import annotations

import json

import pytest

from src.phase2 import run


# ---------------------------------------------------------------------------
# entries_from_db: build Phase 2 entries straight from the DB for a selection
# ---------------------------------------------------------------------------
class _DbWithRecords:
    def __init__(self, rows):
        self._rows = rows

    def get_all_records(self, db_path):
        return list(self._rows)


def test_entries_from_db_filters_and_dedups_newest_per_distro_arch():
    rows = [
        # Rocky 8: two SKUs, x86_64 -> keep the newest version.
        {"publisher": "resf", "image": "rockylinux-x86_64", "sku": "8-base",
         "region": "eastus", "architecture": "x86_64", "family": "yum",
         "distro_label": "Rocky 8", "version": "8.9.20231119"},
        {"publisher": "resf", "image": "rockylinux-x86_64", "sku": "8-lvm",
         "region": "eastus", "architecture": "x86_64", "family": "yum",
         "distro_label": "Rocky 8", "version": "8.9.20240615"},
        # Ubuntu 22.04 arm64 (kept) + a RHEL row that must be filtered out.
        {"publisher": "Canonical", "image": "0001-com-ubuntu-server-jammy",
         "sku": "22_04-lts-arm64", "region": "eastus", "architecture": "arm64",
         "family": "apt", "distro_label": "Ubuntu 22.04", "version": "22.04.1"},
        {"publisher": "RedHat", "image": "RHEL", "sku": "9-lvm", "region": "eastus",
         "architecture": "x86_64", "family": "yum", "distro_label": "RHEL 9",
         "version": "9.5"},
    ]
    db = _DbWithRecords(rows)
    wanted = {"rocky 8", "ubuntu 22.04"}  # already casefolded

    out = run.entries_from_db(db, "db", wanted)

    # RHEL filtered out; Rocky 8 collapsed to ONE x86_64 entry (newest version).
    labels = sorted((e["distro_label"], e["architecture"]) for e in out)
    assert labels == [("Rocky 8", "x86_64"), ("Ubuntu 22.04", "arm64")]
    rocky = next(e for e in out if e["distro_label"] == "Rocky 8")
    assert rocky["version"] == "8.9.20240615"  # newest kept
    assert rocky["sku"] == "8-lvm"


def test_entries_from_db_empty_selection_returns_all_deduped():
    rows = [
        {"distro_label": "Ubuntu 22.04", "architecture": "x86_64", "version": "1"},
        {"distro_label": "Ubuntu 22.04", "architecture": "x86_64", "version": "2"},
        {"distro_label": "Rocky 9", "architecture": "arm64", "version": "9"},
    ]
    out = run.entries_from_db(_DbWithRecords(rows), "db", set())
    keys = sorted((e["distro_label"], e["architecture"]) for e in out)
    assert keys == [("Rocky 9", "arm64"), ("Ubuntu 22.04", "x86_64")]


def test_entries_from_db_excludes_restricted_and_plan_offers():
    # For one (distro_label, arch) the DB carries a NEWER restricted/plan offer
    # and an OLDER plan-free plain-server offer. Without exclusion the dedup keeps
    # the newest (advanced-sla) -> non-deployable. With exclusion it must fall back
    # to the plain-server image even though its version is older.
    rows = [
        # newest version, but restricted audience -> must be dropped
        {"publisher": "Canonical", "image": "0001-com-ubuntu-pro-advanced-sla",
         "sku": "20_04", "architecture": "x86_64", "family": "apt",
         "distro_label": "Ubuntu 20.04", "version": "20.04.202605150"},
        # plan-bearing pro -> must be dropped
        {"publisher": "Canonical", "image": "0001-com-ubuntu-pro-focal",
         "sku": "pro-20_04-lts", "architecture": "x86_64", "family": "apt",
         "distro_label": "Ubuntu 20.04", "version": "20.04.202605120"},
        # confidential-vm (token in the SKU) -> must be dropped
        {"publisher": "Canonical", "image": "0001-com-ubuntu-confidential-vm-focal",
         "sku": "20_04-lts-cvm", "architecture": "x86_64", "family": "apt",
         "distro_label": "Ubuntu 20.04", "version": "20.04.202605270"},
        # older, but plan-free plain server -> the one we want
        {"publisher": "Canonical", "image": "0001-com-ubuntu-server-focal-daily",
         "sku": "20_04-daily-lts", "architecture": "x86_64", "family": "apt",
         "distro_label": "Ubuntu 20.04", "version": "20.04.202505230"},
    ]
    db = _DbWithRecords(rows)
    excl = ("advanced-sla", "pro", "cvm", "confidential", "minimal", "fips")

    out = run.entries_from_db(db, "db", {"ubuntu 20.04"}, exclude_substrings=excl)

    assert len(out) == 1
    assert out[0]["image"] == "0001-com-ubuntu-server-focal-daily"
    assert out[0]["sku"] == "20_04-daily-lts"


def test_entries_from_db_drops_distro_with_only_excluded_offers():
    # If every offer for a (distro_label, arch) is excluded, that pair is dropped
    # entirely rather than falling back to a non-deployable image.
    rows = [
        {"publisher": "Canonical", "image": "0001-com-ubuntu-pro-advanced-sla-airdig",
         "sku": "18_04", "architecture": "x86_64", "family": "apt",
         "distro_label": "Ubuntu 18.04", "version": "18.04.202606050"},
    ]
    out = run.entries_from_db(
        _DbWithRecords(rows), "db", {"ubuntu 18.04"},
        exclude_substrings=("advanced-sla", "pro"),
    )
    assert out == []


# ---------------------------------------------------------------------------
# Phase 1 module fakes (stand in for scripts/notifier.py + scripts/db_manager.py)
# ---------------------------------------------------------------------------
class FakeNotifierMod:
    """Phase 2 calls only send_phase2_summary (one mail per run)."""

    def __init__(self):
        self.summaries = []

    def send_phase2_summary(self, processed, unsupported=None, pending_publish=None,
                            to_phase3=None, trusted=None, skipped=0, errors=None, recipients=None):
        self.summaries.append({
            "processed": processed,
            "unsupported": unsupported or [],
            "pending_publish": pending_publish or [],
            "to_phase3": to_phase3 or [],
            "trusted": trusted or [],
            "errors": errors or [],
        })


class FakeDbMod:
    def __init__(self, matched=True, records=None, pending=None, supported=None,
                 unsupported=None, probe_failed=None):
        self.calls = []
        self.matched = matched
        self.records = records or {}     # identity tuple -> row dict
        self.pending = pending or []     # rows currently pending_publish
        self.supported = supported or [] # rows currently known_supported
        self.unsupported = unsupported or []  # rows currently known_unsupported
        self.probe_failed = probe_failed or []  # rows flagged "PMC unreachable"
        self.marked: list[tuple] = []

    def mark_probe_failed(self, db_path, identity):
        self.marked.append(identity)
        return True

    def get_rows_by_verdict_source(self, db_path, source):
        return list(self.probe_failed) if source == "probe_error" else []

    def set_validation_state(self, db_path, identity, state, last_validated_version=None,
                             reason=None, verdict_source=None):
        self.calls.append((db_path, identity, state, last_validated_version))
        return self.matched

    def get_image_record(self, db_path, publisher, image, sku, region, architecture):
        return self.records.get((publisher, image, sku, region, architecture), {})

    def get_rows_by_state(self, db_path, state):
        if state == "pending_publish":
            return list(self.pending)
        if state == "known_supported":
            return list(self.supported)
        if state == "known_unsupported":
            return list(self.unsupported)
        return []


class FakeProd:
    def __init__(self, repos=None, packages=None):
        self.repos = repos or {}
        self.packages = packages or {}

    def resolve_repo(self, distro, candidates, family=""):
        present = self.repos.get(distro, set())
        for v in candidates:
            if v in present:
                return v
        return None

    def list_packages(self, distro, version, family):
        return list(self.packages.get((distro, version), []))


def _entry(**kw):
    base = {
        "publisher": "Canonical",
        "image": "ubuntu-22_04-lts",
        "sku": "server",
        "version": "22.04.202506",
        "region": "eastus",
        "architecture": "x86_64",
        "family": "apt",
        "distro_label": "Ubuntu 22.04",
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Notifier adapter: the only call is notify_summary, passed straight through
# ---------------------------------------------------------------------------
def test_notifier_adapter_summary_passes_through_with_reasons():
    mod = FakeNotifierMod()
    ad = run.Phase1NotifierAdapter(mod)

    ad.notify_summary(
        processed=4,
        unsupported=[("Plan9 4", "repo is missing")],
        pending_publish=[("Debian 11", "no AzNFS packages are found (amd64); please publish manually then re-run Phase 2")],
        trusted=["RHEL 9"],
        to_phase3=["Ubuntu 22.04"],
        errors=[],
    )

    s = mod.summaries[-1]
    assert s["processed"] == 4
    assert s["unsupported"] == [("Plan9 4", "repo is missing")]
    assert s["pending_publish"][0][0] == "Debian 11"
    assert "publish" in s["pending_publish"][0][1].lower()
    assert s["trusted"] == ["RHEL 9"]
    assert s["to_phase3"] == ["Ubuntu 22.04"]
    assert s["errors"] == []


# ---------------------------------------------------------------------------
# DB adapter
# ---------------------------------------------------------------------------
def test_db_adapter_forwards_path_identity_state():
    mod = FakeDbMod()
    ad = run.Phase1DbAdapter(mod, "/tmp/marketplace.db")
    ident = ("Canonical", "ubuntu-22_04-lts", "server", "eastus", "x86_64")

    ad.set_validation_state(ident, "known_supported")

    assert mod.calls == [("/tmp/marketplace.db", ident, "known_supported", None)]


def test_db_adapter_warns_when_no_row(caplog):
    mod = FakeDbMod(matched=False)
    ad = run.Phase1DbAdapter(mod, "db")
    with caplog.at_level("WARNING"):
        ad.set_validation_state(("p", "i", "s", "r", "a"), "known_unsupported")
    assert "No DB row matched" in caplog.text


# ---------------------------------------------------------------------------
# Input loading
# ---------------------------------------------------------------------------
def test_load_entries_rejects_non_list(tmp_path):
    p = tmp_path / "needs.json"
    p.write_text(json.dumps({"not": "a list"}))
    with pytest.raises(ValueError):
        run.load_entries(str(p))


def test_load_entries_reads_list(tmp_path):
    p = tmp_path / "needs.json"
    p.write_text(json.dumps([{"distro_label": "Ubuntu 22.04"}]))
    assert run.load_entries(str(p)) == [{"distro_label": "Ubuntu 22.04"}]


# ---------------------------------------------------------------------------
# enrich_and_merge: DB last_validated_version + pending_publish re-entry
# ---------------------------------------------------------------------------
def test_enrich_adds_last_validated_version_from_db():
    ident = ("Canonical", "ubuntu-22_04-lts", "server", "eastus", "x86_64")
    db = FakeDbMod(records={ident: {"last_validated_version": "0.3.2"}})

    out = run.enrich_and_merge([_entry()], db, "db")

    assert out[0]["last_validated_version"] == "0.3.2"


def test_enrich_carries_last_regressed_version_from_db():
    # Gate 3 needs the regression marker to avoid re-testing a known-bad package.
    ident = ("Canonical", "ubuntu-22_04-lts", "server", "eastus", "x86_64")
    db = FakeDbMod(records={ident: {"validated": "known_supported",
                                    "last_validated_version": "0.3.400",
                                    "last_regressed_version": "0.3.458"}})

    out = run.enrich_and_merge([_entry()], db, "db")

    assert out[0]["last_validated_version"] == "0.3.400"
    assert out[0]["last_regressed_version"] == "0.3.458"


def test_enrich_carries_image_version_and_timestamp_from_db():
    # Gate 3's image-drift check needs the validated image version + timestamp.
    ident = ("Canonical", "ubuntu-22_04-lts", "server", "eastus", "x86_64")
    db = FakeDbMod(records={ident: {"validated": "known_supported",
                                    "last_validated_version": "0.3.458",
                                    "last_validated_image_version": "22.04.202601",
                                    "last_validated": "2026-08-01T00:00:00Z"}})

    out = run.enrich_and_merge([_entry()], db, "db")

    assert out[0]["last_validated_image_version"] == "22.04.202601"
    assert out[0]["last_validated"] == "2026-08-01T00:00:00Z"


def test_refeed_dedups_supported_to_one_rep_per_distro_arch():
    # Two SKUs of the same release+arch -> only the newest-version rep is re-fed
    # (matches Phase 3's per-URL dedup; avoids re-validating the same package per SKU).
    rows = [
        {"publisher": "RedHat", "image": "rhel", "sku": "9_0", "version": "9.0.1",
         "region": "eastus", "architecture": "x86_64", "family": "yum",
         "distro_label": "RHEL 9", "last_validated_version": "0.3.400"},
        {"publisher": "RedHat", "image": "rhel", "sku": "9-lvm", "version": "9.8.9",
         "region": "eastus", "architecture": "x86_64", "family": "yum",
         "distro_label": "RHEL 9", "last_validated_version": "0.3.400"},
    ]
    db = FakeDbMod(supported=rows)

    out = run.enrich_and_merge([], db, "db")

    assert len(out) == 1
    assert out[0]["sku"] == "9-lvm"            # newest marketplace version wins
    assert out[0]["_db_state"] == "known_supported"


def test_refeed_excludes_policy_distros_and_offers():
    # An excluded distro (CentOS) / offer (advanced-sla) must not be re-fed,
    # matching Phase 1's marketplace exclusions.
    rows = [
        {"publisher": "OpenLogic", "image": "centos", "sku": "7", "version": "7.9",
         "region": "eastus", "architecture": "x86_64", "family": "yum",
         "distro_label": "CentOS 7", "last_validated_version": "0.3.400"},
        {"publisher": "Canonical", "image": "0001-com-ubuntu-pro-advanced-sla",
         "sku": "22_04", "version": "22.04.1", "region": "eastus",
         "architecture": "x86_64", "family": "apt", "distro_label": "Ubuntu 22.04",
         "last_validated_version": "0.3.400"},
    ]
    db = FakeDbMod(supported=rows)

    out = run.enrich_and_merge([], db, "db")

    assert out == []                          # both dropped by the exclusion policy


def test_load_exclusions_reads_env_not_config(monkeypatch):
    # Must not depend on `import config` (which reads AZURE_SUBSCRIPTION_ID at
    # import and would raise in a bare manual run, silently disabling the re-feed).
    monkeypatch.delenv("EXCLUDED_DISTRO_PREFIXES", raising=False)
    monkeypatch.delenv("EXCLUDED_OFFER_SUBSTRINGS", raising=False)
    assert run._load_exclusions() == (("centos",), ("advanced-sla",))
    monkeypatch.setenv("EXCLUDED_DISTRO_PREFIXES", "centos, Fedora")
    monkeypatch.setenv("EXCLUDED_OFFER_SUBSTRINGS", "pro,cvm")
    assert run._load_exclusions() == (("centos", "fedora"), ("pro", "cvm"))


def test_enrich_merges_pending_publish_rows_and_dedupes():
    e = _entry()
    dup_row = {**e, "last_validated_version": ""}        # same identity -> not duplicated
    extra_row = {
        "publisher": "resf", "image": "rockylinux-x86_64", "sku": "9-base",
        "region": "eastus", "architecture": "x86_64",
        "family": "yum", "distro_label": "Rocky 9", "last_validated_version": "",
    }
    db = FakeDbMod(pending=[dup_row, extra_row])

    out = run.enrich_and_merge([e], db, "db")

    assert len(out) == 2
    assert {r["distro_label"] for r in out} == {"Ubuntu 22.04", "Rocky 9"}


def _ident(e):
    return (e["publisher"], e["image"], e["sku"], e["region"], e["architecture"])


def test_enrich_skips_lisa_verdicts_but_rechecks_the_rest():
    # A reused Phase 1 artifact still lists images Phase 2 already handled. The DB
    # state is authoritative: known_unsupported rows a Phase 3 VM run decided are
    # NOT re-dispatched. known_supported flows on (tagged _db_state) so Gate 3 can
    # re-check the prod AzNFS version, and so does a known_unsupported row Phase 2
    # decided itself -- that verdict is just a repo/package lookup and goes stale.
    e_supported = _entry(sku="supported")
    e_gate = _entry(sku="gate-unsupported")
    e_lisa = _entry(sku="lisa-unsupported")
    e_fresh = _entry(sku="fresh")
    db = FakeDbMod(records={
        _ident(e_supported): {"validated": "known_supported", "last_validated_version": "0.3.458"},
        _ident(e_gate): {"validated": "known_unsupported", "verdict_source": "gate"},
        _ident(e_lisa): {"validated": "known_unsupported", "verdict_source": "lisa"},
        _ident(e_fresh): {"validated": "unknown"},
    })

    out = run.enrich_and_merge([e_supported, e_gate, e_lisa, e_fresh], db, "db")

    assert [r["sku"] for r in out] == ["supported", "gate-unsupported", "fresh"]
    supported = next(r for r in out if r["sku"] == "supported")
    assert supported["_db_state"] == "known_supported"
    assert supported["last_validated_version"] == "0.3.458"


def test_enrich_keeps_pending_publish_artifact_entry():
    # An image whose DB state is pending_publish is NOT terminal -- it must keep
    # flowing so it re-checks prod for the (now hopefully published) package.
    e = _entry()
    db = FakeDbMod(records={_ident(e): {"validated": "pending_publish"}})

    out = run.enrich_and_merge([e], db, "db")

    assert len(out) == 1
    assert out[0]["sku"] == "server"


def test_enrich_refeeds_known_supported_rows_for_recheck():
    # Even when Phase 1 does not re-emit an already-validated distro (its
    # marketplace image did not change), enrich re-feeds every known_supported
    # DB row so Gate 3 re-checks the prod AzNFS version. Rows are tagged
    # _db_state and carry last_validated_version.
    supported_row = {
        "publisher": "Canonical", "image": "ubuntu-24_04-lts", "sku": "server",
        "region": "eastus", "architecture": "x86_64", "family": "apt",
        "distro_label": "Ubuntu 24.04", "last_validated_version": "0.3.400",
    }
    db = FakeDbMod(supported=[supported_row])

    out = run.enrich_and_merge([], db, "db")

    assert len(out) == 1
    assert out[0]["distro_label"] == "Ubuntu 24.04"
    assert out[0]["_db_state"] == "known_supported"
    assert out[0]["last_validated_version"] == "0.3.400"


def test_enrich_refeed_dedupes_supported_against_incoming_entry():
    # A known_supported distro that IS in the incoming artifact (e.g. its image
    # updated) is not duplicated by the re-feed.
    e = _entry()
    db = FakeDbMod(
        records={_ident(e): {"validated": "known_supported", "last_validated_version": "0.3.2"}},
        supported=[{**e, "last_validated_version": "0.3.2"}],
    )

    out = run.enrich_and_merge([e], db, "db")

    assert len(out) == 1
    assert out[0]["_db_state"] == "known_supported"


# ---------------------------------------------------------------------------
# End-to-end run() with injected fakes (no network, no Phase 1 modules)
# ---------------------------------------------------------------------------
def test_run_end_to_end_to_phase3_writes_lisa_jobs(tmp_path):
    notifier_mod = FakeNotifierMod()
    db_mod = FakeDbMod()
    out = tmp_path / "lisa_jobs.json"
    prod = FakeProd(
        repos={"ubuntu": {"22.04"}},
        packages={("ubuntu", "22.04"): ["aznfs_0.3.2_amd64.deb"]},
    )

    jobs = run.run(
        entries=[_entry()],
        prod=prod,
        notifier_obj=run.Phase1NotifierAdapter(notifier_mod),
        db=run.Phase1DbAdapter(db_mod, "marketplace.db"),
        lisa_jobs_path=str(out),
    )

    assert len(jobs) == 1
    written = json.loads(out.read_text())
    assert written[0]["aznfs_package_url"].endswith("aznfs_0.3.2_amd64.deb")
    # The LISA (to_phase3) path leaves validation_state UNCHANGED -- only
    # known_supported / known_unsupported / unknown ever persist, and Phase 3
    # sets the verdict. So no set_validation_state call is made here.
    assert db_mod.calls == []
    # Exactly one mail: the summary, with this distro in the to_phase3 bucket.
    assert len(notifier_mod.summaries) == 1
    to_phase3 = notifier_mod.summaries[-1]["to_phase3"]
    assert [r["label"] for r in to_phase3] == ["Ubuntu 22.04"]
    assert to_phase3[0]["arch"] == "x86_64"
    assert to_phase3[0]["url"].endswith("aznfs_0.3.2_amd64.deb")


def test_run_end_to_end_trusted(tmp_path):
    notifier_mod = FakeNotifierMod()
    db_mod = FakeDbMod()
    out = tmp_path / "lisa_jobs.json"
    prod = FakeProd(
        repos={"ubuntu": {"22.04"}},
        packages={("ubuntu", "22.04"): ["aznfs_0.3.2_amd64.deb"]},
    )

    jobs = run.run(
        entries=[_entry(last_validated_version="0.3.2")],
        prod=prod,
        notifier_obj=run.Phase1NotifierAdapter(notifier_mod),
        db=run.Phase1DbAdapter(db_mod, "marketplace.db"),
        lisa_jobs_path=str(out),
    )

    assert jobs == []
    # Gate 3 still records the trusted verdict in the DB (known_supported)...
    assert db_mod.calls[-1][2] == "known_supported"
    # ...but a trusted-only run is NOT actionable, so no summary e-mail is sent
    # (the daily known_supported re-check would otherwise mail every run).
    assert notifier_mod.summaries == []


def test_phase2_validates_whatever_it_is_handed():
    # Scope is Phase 1's job. Phase 2 must not second-guess its input, so a
    # manually selected out-of-matrix distro flows through untouched.
    entries = [_entry(sku="in", distro_label="Ubuntu 24.04"),
               _entry(sku="debian", distro_label="Debian 12")]

    out = run.enrich_and_merge(entries, FakeDbMod(), "db")

    assert [r["distro_label"] for r in out] == ["Ubuntu 24.04", "Debian 12"]
def _db_row(sku, label="Ubuntu 24.04", source="", version="24.04.1"):
    return {"publisher": "Canonical", "image": "ubuntu-24_04-lts", "sku": sku,
            "region": "eastus", "architecture": "x86_64", "family": "apt",
            "distro_label": label, "version": version, "verdict_source": source}


def test_gate_unsupported_rows_are_refed_for_recheck():
    # A "prod repo is missing" verdict is only a lookup: it must be re-checked so
    # a distro that gained AzNFS support (or was hit by an unreachable PMC) heals.
    db = FakeDbMod(unsupported=[_db_row("server", source="gate")])

    out = run.enrich_and_merge([], db, "db")

    assert [r["sku"] for r in out] == ["server"]
    assert out[0]["_db_state"] == "known_unsupported"


def test_lisa_unsupported_rows_are_not_refed():
    # The suite really failed on a VM -- re-feeding would re-provision it daily.
    db = FakeDbMod(unsupported=[_db_row("server", source="lisa")])

    assert run.enrich_and_merge([], db, "db") == []


def test_unsupported_refeed_keeps_one_rep_per_distro_and_arch():
    # Deployability outranks recency: a 'minimal' build is a special-purpose
    # image, and picking it over the plain server SKU is how the pipeline ended
    # up validating images it could not deploy.
    db = FakeDbMod(unsupported=[
        _db_row("server", version="24.04.1"),
        _db_row("minimal", version="24.04.9"),
    ])

    out = run.enrich_and_merge([], db, "db")

    assert [r["sku"] for r in out] == ["server"]


def test_refeed_prefers_the_newest_of_two_equally_deployable_skus():
    db = FakeDbMod(unsupported=[
        _db_row("server", version="24.04.1"),
        _db_row("server", version="24.04.9"),
    ])

    out = run.enrich_and_merge([], db, "db")

    assert [r["version"] for r in out] == ["24.04.9"]


def _probe_row(sku="server", label="Ubuntu 24.04", validated="unknown"):
    return {"publisher": "Canonical", "image": "ubuntu-24_04-lts", "sku": sku,
            "region": "eastus", "architecture": "x86_64", "family": "apt",
            "distro_label": label, "version": "24.04.1", "validated": validated,
            "verdict_source": "probe_error"}


def test_a_row_flagged_probe_error_is_retried():
    # Nothing else re-feeds `unknown`, so without the marker a release stranded
    # by one unreachable run would wait for its image to change.
    db = FakeDbMod(probe_failed=[_probe_row()])

    out = run.enrich_and_merge([], db, "db")

    assert [r["sku"] for r in out] == ["server"]


def test_probe_error_retry_keeps_the_untouched_verdict():
    # The marker does not overwrite `validated`, so a previously supported row
    # is re-fed still tagged supported and cannot be silently downgraded.
    db = FakeDbMod(probe_failed=[_probe_row(validated="known_supported")])

    out = run.enrich_and_merge([], db, "db")

    assert out[0]["_db_state"] == "known_supported"


def test_probe_error_retry_honours_the_exclusion_policy():
    row = _probe_row()
    row["image"] = "0001-com-ubuntu-pro-advanced-sla"
    db = FakeDbMod(probe_failed=[row])

    assert run.enrich_and_merge([], db, "db") == []


def test_probe_error_retry_is_deduped_against_the_handoff():
    # Phase 1 re-emitting the same image must not queue it twice.
    row = _probe_row()
    entry = _entry(sku="server", distro_label="Ubuntu 24.04")
    entry.update({"publisher": row["publisher"], "image": row["image"],
                  "region": row["region"], "architecture": row["architecture"]})
    db = FakeDbMod(probe_failed=[row])

    out = run.enrich_and_merge([entry], db, "db")

    assert len(out) == 1


def _ver_row(sku, version, validated="known_unsupported", source="gate"):
    return {"publisher": "RedHat", "image": "RHEL", "sku": sku, "region": "eastus",
            "architecture": "x86_64", "family": "yum", "distro_label": "RHEL 9",
            "version": version, "validated": validated, "verdict_source": source}


def test_representative_is_the_numerically_newest_image():
    # '9.10.x' is newer than '9.8.x' but sorts BELOW it as a string, so a lexical
    # compare would re-feed a stale image.
    db = FakeDbMod(unsupported=[_ver_row("old", "9.8.2026062413"),
                                _ver_row("new", "9.10.2026062413")])

    out = run.enrich_and_merge([], db, "db")

    assert [r["sku"] for r in out] == ["new"]


def test_from_db_representative_is_numerically_newest():
    db = FakeDbMod()
    db.get_all_records = lambda path: [_ver_row("old", "9.8.2026062413", "unknown", ""),
                                       _ver_row("new", "9.10.2026062413", "unknown", "")]

    entries = run.entries_from_db(db, "db", {"rhel 9"})

    assert [e["sku"] for e in entries] == ["new"]
