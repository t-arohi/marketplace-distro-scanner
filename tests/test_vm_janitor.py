from __future__ import annotations

import sys
import types
from datetime import datetime, timezone

import pytest

import vm_janitor


@pytest.fixture(autouse=True)
def _subscription(monkeypatch):
    # Per-test and restored afterwards: vm_janitor reads this at call time, and
    # setting it at import would leak into every other module in the run.
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "00000000-0000-0000-0000-000000000000")


def _vm(name, created):
    return {"name": name, "id": f"/subscriptions/s/rg/r/{name}", "created": created}


def test_zero_hours_sweeps_everything(monkeypatch):
    # Straight after a run Phase 3 holds the only slot, so nothing is in use.
    vms = [_vm("old", "2026-08-26T11:35:24+00:00"), _vm("new", "2026-09-04T21:00:00+00:00")]
    monkeypatch.setattr(vm_janitor, "_az", lambda *a: vms)

    assert [v["name"] for v in vm_janitor.stale_vms("rg", 0)] == ["old", "new"]


def test_an_age_cutoff_protects_a_running_environment(monkeypatch):
    # A scheduled sweep must not delete a VM a live run is still using.
    from datetime import datetime, timedelta, timezone
    recent = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    vms = [_vm("old", "2026-08-26T11:35:24+00:00"), _vm("live", recent)]
    monkeypatch.setattr(vm_janitor, "_az", lambda *a: vms)

    assert [v["name"] for v in vm_janitor.stale_vms("rg", 3)] == ["old"]


def test_an_unparseable_creation_time_is_swept_with_no_cutoff(monkeypatch):
    # Nothing to protect when the group is known idle, so do not leak it.
    monkeypatch.setattr(vm_janitor, "_az", lambda *a: [_vm("odd", "not-a-date")])

    assert [v["name"] for v in vm_janitor.stale_vms("rg", 0)] == ["odd"]


def test_an_undatable_vm_is_kept_when_a_cutoff_is_in_force(monkeypatch):
    # The cutoff exists to protect a running environment. Deleting a VM we
    # cannot date could kill a live test; keeping it only costs money, and the
    # survivor count raises an alert so it cannot be missed.
    monkeypatch.setattr(vm_janitor, "_az", lambda *a: [_vm("odd", "not-a-date")])

    assert vm_janitor.stale_vms("rg", 3) == []


def test_dry_run_deletes_nothing(monkeypatch):
    calls = []

    def fake_az(*args):
        calls.append(args)
        return [_vm("old", "2026-08-26T11:35:24+00:00")]

    monkeypatch.setattr(vm_janitor, "_az", fake_az)
    result = vm_janitor.sweep("rg", 0, dry_run=True)

    assert result["deleted_vms"] == 0
    assert not any("delete" in a for call in calls for a in call)


def test_sweep_deletes_vms_then_their_orphans(monkeypatch):
    # Order matters twice over: a disk or NIC cannot be removed while its VM
    # holds it, and a private endpoint's NIC cannot be removed at all until the
    # endpoint is gone (Azure rejects it with NicInUseWithPrivateEndpoint).
    seen = []

    def fake_az(*args):
        seen.append(args[:3])
        if args[:2] == ("vm", "list"):
            return [_vm("old", "2026-08-26T11:35:24+00:00")] if len(seen) == 1 else []
        if args[:3] == ("storage", "account", "list"):
            return [{"name": "lisascaqty6dog2w", "id": "/id/acct"}]
        if "list" in args[:3]:
            return ["/id/one"]
        return None

    monkeypatch.setattr(vm_janitor, "_az", fake_az)
    result = vm_janitor.sweep("rg", 0)

    assert result["deleted_vms"] == 1
    assert result["remaining"] == 0
    assert result["failures"] == 0
    assert seen.index(("vm", "delete", "--yes")) < seen.index(("network", "private-endpoint", "list"))
    assert (seen.index(("network", "private-endpoint", "delete"))
            < seen.index(("network", "nic", "list")))


def test_one_stubborn_resource_does_not_abandon_the_sweep(monkeypatch):
    # A single failure used to abort everything, leaving 106 disks and IPs.
    def fake_az(*args):
        if args[:2] == ("vm", "list"):
            return []
        if args[:3] == ("storage", "account", "list"):
            return [{"name": "lisascaqty6dog2w", "id": "/id/acct"}]
        if "list" in args[:3]:
            return ["/id/one"]
        if args[:3] == ("network", "nic", "delete"):
            raise RuntimeError("NicInUseWithPrivateEndpoint")
        return None

    monkeypatch.setattr(vm_janitor, "_az", fake_az)
    result = vm_janitor.sweep("rg", 0)

    assert result["failures"] == 1
    assert result["orphans"]["disks"] == 1        # reached despite the NIC failure
    assert result["orphans"]["storage"] == 1


def test_survivors_raise_an_alert(monkeypatch):
    # A silent cleanup failure is what let 103 VMs accumulate unnoticed.
    monkeypatch.setattr(vm_janitor, "sweep",
                        lambda *a, **k: {"deleted_vms": 0, "eligible": 0,
                                         "orphans": {}, "remaining": 7})
    alerts = []
    monkeypatch.setattr(vm_janitor, "_alert", lambda rg, detail: alerts.append(detail))

    assert vm_janitor.main(["--resource-group", "rg", "--alert"]) == 0
    assert "7 eligible VM(s) survived the sweep" in alerts[0]


def test_a_failed_sweep_alerts_and_reports_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("az exploded")

    monkeypatch.setattr(vm_janitor, "sweep", boom)
    alerts = []
    monkeypatch.setattr(vm_janitor, "_alert", lambda rg, detail: alerts.append(detail))

    assert vm_janitor.main(["--resource-group", "rg", "--alert"]) == 1
    assert "az exploded" in alerts[0]


def test_a_clean_sweep_stays_quiet(monkeypatch):
    monkeypatch.setattr(vm_janitor, "sweep",
                        lambda *a, **k: {"deleted_vms": 3, "eligible": 3,
                                         "orphans": {}, "remaining": 0})
    alerts = []
    monkeypatch.setattr(vm_janitor, "_alert", lambda rg, detail: alerts.append(detail))

    assert vm_janitor.main(["--resource-group", "rg", "--alert"]) == 0
    assert alerts == []


def test_shared_storage_account_is_never_deleted(monkeypatch):
    # LISA's shared account is lisas<location><subscription-suffix>, which the
    # old "lisa" prefix matched. It is reused across runs and must survive.
    deleted = []

    def fake_az(*args):
        if args[:2] == ("vm", "list"):
            return []
        if args[:3] == ("storage", "account", "list"):
            # chinanorth3 is the awkward one: a location with a digit that
            # also starts with c, so it matches the lisasc prefix.
            return [{"name": "lisascentralindi92ef804a", "id": "/id/shared"},
                    {"name": "lisaschinanorth392ef804a", "id": "/id/shared-digit"},
                    {"name": "lisascaqty6dog2w", "id": "/id/transient"}]
        if "list" in args[:3]:
            return []
        if args[:3] == ("storage", "account", "delete"):
            deleted.append(args[-1])
        return None

    monkeypatch.setattr(vm_janitor, "_az", fake_az)
    result = vm_janitor.sweep("rg", 0)

    assert deleted == ["/id/transient"]
    assert result["orphans"]["storage"] == 1


def test_orphan_groups_are_swept_when_no_rg_is_pinned(monkeypatch):
    # Unpinned is the normal setup, and LISA deletes its own group -- so a
    # tagged group still standing means that cleanup did not happen.
    deleted = []

    def fake_az(*args):
        if args[:2] == ("group", "list"):
            return ["lisa-20260905-1-e0", "lisa-20260905-1-e1"]
        if args[:3] == ("group", "delete", "--name"):
            deleted.append(args[3])
        return None

    monkeypatch.setattr(vm_janitor, "_az", fake_az)
    result = vm_janitor.sweep_orphan_groups(0)

    assert deleted == ["lisa-20260905-1-e0", "lisa-20260905-1-e1"]
    assert result["deletions_requested"] == 2


def test_a_group_holding_a_fresh_vm_is_left_alone(monkeypatch):
    # Guards a concurrently running environment when a cutoff is given.
    fresh = datetime.now(timezone.utc).isoformat()

    def fake_az(*args):
        if args[:2] == ("group", "list"):
            return ["lisa-live-e0"]
        if args[:2] == ("vm", "list"):
            return [fresh]
        return None

    monkeypatch.setattr(vm_janitor, "_az", fake_az)
    assert vm_janitor.orphan_groups(24) == []


def test_an_orphan_group_alerts_even_when_deletion_succeeds(monkeypatch):
    # The group existing at all means LISA's cleanup did not run -- worth
    # knowing about even though the janitor tidied up after it.
    alerts = []

    def fake_az(*args):
        if args[:2] == ("group", "list"):
            return ["lisa-orphan-e0"]
        return None

    monkeypatch.setattr(vm_janitor, "_az", fake_az)
    monkeypatch.setattr(vm_janitor, "_alert", lambda scope, detail: alerts.append(detail))
    assert vm_janitor.main(["--older-than-hours", "0", "--alert"]) == 0
    assert len(alerts) == 1
    assert "outlived" in alerts[0]


def test_a_cutoff_leaves_live_resources_alone(monkeypatch):
    # With a cutoff protecting a running environment, a private endpoint and a
    # storage account carry no "detached" state and the live test needs both --
    # deleting them would break the very run the cutoff exists to protect.
    fresh = datetime.now(timezone.utc).isoformat()
    calls = []

    def fake_az(*args):
        calls.append(args[:3])
        if args[:2] == ("vm", "list"):
            return [_vm("live", fresh)]
        if "list" in args[:3]:
            return ["/id/one"]
        return None

    monkeypatch.setattr(vm_janitor, "_az", fake_az)
    result = vm_janitor.sweep("rg", 24)

    assert ("network", "private-endpoint", "list") not in calls
    assert ("storage", "account", "list") not in calls
    assert result["orphans"]["private_endpoints"] == 0
    assert result["orphans"]["storage"] == 0
    assert result["deleted_vms"] == 0            # the live VM was kept


def test_kept_vms_do_not_raise_a_false_alert(monkeypatch):
    # remaining used to count every VM in the group, so the ones the cutoff
    # deliberately keeps looked like a cleanup failure.
    fresh = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(vm_janitor, "_az",
                        lambda *a: [_vm("live", fresh)] if a[:2] == ("vm", "list") else None)
    alerts = []
    monkeypatch.setattr(vm_janitor, "_alert", lambda scope, detail: alerts.append(detail))

    assert vm_janitor.main(["--resource-group", "rg",
                            "--older-than-hours", "24", "--alert"]) == 0
    assert alerts == []


def test_one_undeletable_vm_does_not_stop_the_rest(monkeypatch):
    # A bulk `az vm delete --ids` aborted everything if any single VM failed.
    deleted = []

    def fake_az(*args):
        if args[:2] == ("vm", "list"):
            return [_vm("bad", "2026-08-26T11:35:24+00:00"),
                    _vm("good", "2026-08-26T11:35:24+00:00")] if not deleted else []
        if args[:3] == ("vm", "delete", "--yes"):
            if args[-1].endswith("bad"):
                raise RuntimeError("VMBeingDeallocated")
            deleted.append(args[-1])
            return None
        if "list" in args[:3]:
            return []
        return None

    monkeypatch.setattr(vm_janitor, "_az", fake_az)
    result = vm_janitor.sweep("rg", 0)

    assert result["deleted_vms"] == 1
    assert result["failures"] == 1
    assert any(d.endswith("good") for d in deleted)


def test_private_endpoint_nics_are_not_selected_directly(monkeypatch):
    # Azure refuses to delete them; the endpoint owns them and takes them with it.
    queries = []

    def fake_az(*args):
        if args[:3] == ("network", "nic", "list"):
            queries.append(args[-1])
        if args[:2] == ("vm", "list"):
            return []
        return None

    monkeypatch.setattr(vm_janitor, "_az", fake_az)
    vm_janitor.sweep("rg", 0)

    assert queries and "privateEndpoint==null" in queries[0]


def test_a_timestamp_without_an_offset_does_not_abort_the_sweep(monkeypatch):
    # datetime.fromisoformat happily returns a naive datetime, which cannot be
    # compared with the aware cutoff -- that TypeError would abort the sweep
    # and quietly resume the leak.
    def fake_az(*args):
        if args[:2] == ("vm", "list"):
            return [_vm("old", "2026-08-26T11:35:24")]     # no offset
        return None

    monkeypatch.setattr(vm_janitor, "_az", fake_az)
    assert [v["name"] for v in vm_janitor.stale_vms("rg", 24)] == ["old"]


def test_a_naive_timestamp_is_read_as_utc_not_local(monkeypatch):
    # Reading it as local time would shift the age by the runner's offset and
    # could keep a VM that is actually past the cutoff.
    assert vm_janitor._parse_created("2026-08-26T11:35:24") == \
        vm_janitor._parse_created("2026-08-26T11:35:24+00:00")


def test_every_az_call_is_pinned_to_the_subscription(monkeypatch):
    # These commands delete things; the CLI would otherwise use whatever
    # subscription happens to be the runner's default -- and the workflow logs
    # in with --allow-no-subscriptions, so there may not be one.
    seen = {}

    class _Proc:
        returncode = 0
        stdout = "[]"
        stderr = ""

    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub-1234")
    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(vm_janitor.subprocess, "run", fake_run)
    vm_janitor._az("vm", "delete", "--ids", "/id/one")

    assert "--subscription" in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("--subscription") + 1] == "sub-1234"


def test_no_subscription_set_still_runs(monkeypatch):
    class _Proc:
        returncode = 0
        stdout = "[]"
        stderr = ""

    seen = {}
    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)
    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(vm_janitor.subprocess, "run", fake_run)
    vm_janitor._az("vm", "list")

    assert "--subscription" not in seen["cmd"]


def test_the_alert_reads_correctly_without_a_resource_group(monkeypatch):
    # scope is a tag selector in the unpinned mode, so "the resource group ..."
    # would have been wrong.
    sent = {}
    notifier = types.ModuleType("notifier")
    notifier.notify = lambda **kw: sent.update(kw)
    monkeypatch.setitem(sys.modules, "notifier", notifier)

    vm_janitor._alert("the groups tagged created_by=aznfs-phase3", "1 group outlived its run")

    assert "the resource group the groups tagged" not in sent["plain"]
    assert "Check the groups tagged created_by=aznfs-phase3" in sent["plain"]


def test_vms_come_back_oldest_first_regardless_of_offset(monkeypatch):
    # Sorting the raw strings puts "+05:30" before "+00:00" for the same instant.
    monkeypatch.setattr(vm_janitor, "_az", lambda *a: [
        _vm("newer", "2026-08-26T12:00:00+00:00"),
        _vm("oldest", "2026-08-26T11:00:00-05:00"),   # 16:00Z -> actually newest
        _vm("unreadable", "not-a-date"),
    ] if a[:2] == ("vm", "list") else None)

    order = [v["name"] for v in vm_janitor.stale_vms("rg", 0)]
    assert order[0] == "unreadable"          # unparseable counts as very old
    assert order.index("newer") < order.index("oldest")


def test_a_group_of_undatable_vms_is_kept(monkeypatch):
    # Same reasoning one level up: assume one of them is live rather than
    # delete the whole group out from under a running environment.
    def fake_az(*args):
        if args[:2] == ("group", "list"):
            return ["lisa-odd-e0"]
        if args[:2] == ("vm", "list"):
            return ["not-a-date"]
        return None

    monkeypatch.setattr(vm_janitor, "_az", fake_az)
    assert vm_janitor.orphan_groups(24) == []


def test_an_empty_group_is_still_swept(monkeypatch):
    # No VMs at all is a genuine orphan, not an unknown age.
    def fake_az(*args):
        if args[:2] == ("group", "list"):
            return ["lisa-empty-e0"]
        if args[:2] == ("vm", "list"):
            return []
        return None

    monkeypatch.setattr(vm_janitor, "_az", fake_az)
    assert vm_janitor.orphan_groups(24) == ["lisa-empty-e0"]


def test_dry_run_reports_the_same_shape_as_a_real_sweep(monkeypatch):
    monkeypatch.setattr(vm_janitor, "_az",
                        lambda *a: [_vm("old", "2026-08-26T11:35:24+00:00")]
                        if a[:2] == ("vm", "list") else None)

    dry = vm_janitor.sweep("rg", 0, dry_run=True)
    real = vm_janitor.sweep("rg", 0)

    assert dry.keys() == real.keys()
    assert dry["orphans"].keys() == real["orphans"].keys()


def test_a_stalled_group_deletion_is_reported_again_next_run(monkeypatch):
    # --no-wait means an accepted request is not proof of completion, so the
    # sweep must stay idempotent: the same orphan is found and alerted again.
    alerts = []
    monkeypatch.setattr(vm_janitor, "_alert", lambda scope, detail: alerts.append(detail))
    monkeypatch.setattr(vm_janitor, "_az",
                        lambda *a: ["lisa-stuck-e0"] if a[:2] == ("group", "list") else None)

    for _ in range(2):                       # the delete never actually lands
        assert vm_janitor.main(["--older-than-hours", "0", "--alert"]) == 0

    assert len(alerts) == 2
    assert all("outlived" in a for a in alerts)


def test_azure_100ns_timestamps_parse():
    # Real values from `az vm list`: 7 fractional digits. fromisoformat only
    # accepts 6 before Python 3.11, and this package supports 3.10.
    parsed = vm_janitor._parse_created("2025-06-24T06:34:18.5885152+00:00")
    assert parsed is not None
    assert parsed.year == 2025 and parsed.microsecond == 588515


def test_a_kept_undatable_vm_still_alerts(monkeypatch):
    # Keeping it is the safe choice, but it must not sit there unnoticed --
    # a silent leak is exactly what this script exists to prevent.
    alerts = []
    monkeypatch.setattr(vm_janitor, "_alert", lambda scope, detail: alerts.append(detail))
    monkeypatch.setattr(vm_janitor, "_az",
                        lambda *a: [_vm("odd", "not-a-date")]
                        if a[:2] == ("vm", "list") else None)

    assert vm_janitor.main(["--resource-group", "rg",
                            "--older-than-hours", "24", "--alert"]) == 0
    assert len(alerts) == 1
    # Must not read "0 VM(s) still present" while one is in fact running:
    # remaining counts only the ELIGIBLE survivors.
    assert "eligible VM(s) survived" in alerts[0]
    assert "1 were left running because their age could not be read" in alerts[0]


def test_dry_run_predicts_the_alert_a_real_run_would_raise(monkeypatch):
    # Zeroing undatable made a dry run look clean while a real one would alert.
    monkeypatch.setattr(vm_janitor, "_az",
                        lambda *a: [_vm("odd", "not-a-date")]
                        if a[:2] == ("vm", "list") else None)

    dry = vm_janitor.sweep("rg", 24, dry_run=True)
    real = vm_janitor.sweep("rg", 24)

    assert dry["undatable"] == real["undatable"] == 1


def test_a_negative_cutoff_is_rejected():
    # '--older-than-hours -24' silently took the aggressive path and deleted
    # everything, the opposite of what the typo intended.
    for bad in ("-24", "nan", "inf", "-0.5"):
        with pytest.raises(SystemExit):
            vm_janitor.main(["--resource-group", "rg", "--older-than-hours", bad])


def test_a_dry_run_alert_does_not_claim_a_sweep_happened(monkeypatch):
    alerts = []
    monkeypatch.setattr(vm_janitor, "_alert", lambda scope, detail: alerts.append(detail))
    monkeypatch.setattr(vm_janitor, "_az",
                        lambda *a: [_vm("vm1", "2026-08-01T00:00:00+00:00")]
                        if a[:2] == ("vm", "list") else None)

    vm_janitor.main(["--resource-group", "rg", "--dry-run", "--alert"])

    assert alerts[0].startswith("[dry run]")
    assert "nothing was actually deleted" in alerts[0]
    # The candidates would be DELETED, not survive -- saying otherwise inverts
    # the meaning of the alert.
    assert "survive" not in alerts[0]
    assert "1 VM(s) would be deleted" in alerts[0]


def test_a_dry_run_group_alert_does_not_claim_deletions_were_attempted(monkeypatch):
    # The VM path was fixed for this; the group path had the same flaw.
    alerts = []
    monkeypatch.setattr(vm_janitor, "_alert", lambda scope, detail: alerts.append(detail))
    monkeypatch.setattr(vm_janitor, "_az",
                        lambda *a: ["lisa-orphan-e0"] if a[:2] == ("group", "list") else None)

    vm_janitor.main(["--dry-run", "--alert"])

    assert alerts[0].startswith("[dry run]")
    assert "could not be deleted" not in alerts[0]


def test_the_subscription_env_var_does_not_leak_between_tests(monkeypatch):
    # It used to be set at import time, mutating the whole test process.
    import os
    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)
    assert "AZURE_SUBSCRIPTION_ID" not in os.environ


def test_undatable_is_not_reported_without_a_cutoff(monkeypatch):
    # At cutoff 0 an undatable VM is deleted, not kept, so reporting it as
    # "left running" would double-count the very VM being removed.
    alerts = []
    monkeypatch.setattr(vm_janitor, "_alert", lambda scope, detail: alerts.append(detail))
    monkeypatch.setattr(vm_janitor, "_az",
                        lambda *a: [_vm("odd", "not-a-date")]
                        if a[:2] == ("vm", "list") else None)

    vm_janitor.main(["--resource-group", "rg", "--older-than-hours", "0",
                     "--dry-run", "--alert"])

    assert "1 VM(s) would be deleted" in alerts[0]
    assert "1 left running" not in alerts[0]
