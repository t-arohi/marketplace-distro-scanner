from __future__ import annotations

import sqlite3

import pytest

import db_manager


def test_reset_clears_all_validation_markers(tmp_path):
    # RESET_VALIDATION must clear last_regressed_version + last_validated_image_version
    # too, otherwise a reset 'unknown' row keeps a stale regression marker and Gate 3
    # can trust it into known_supported without a LISA run.
    db = str(tmp_path / "m.db")
    db_manager.initialize(db, "db/schema.sql")
    ident = ("RedHat", "rhel", "9_0", "eastus", "x86_64")
    db_manager.check_and_upsert(db, *ident[:3], "9.0.1", ident[3], ident[4], "yum", "RHEL 9")
    db_manager.set_validation_state(db, ident, "known_supported", last_validated_version="0.3.458")
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE images SET last_regressed_version='0.3.500', last_validated_image_version='9.0.1'"
    )
    conn.commit()
    conn.close()

    db_manager.reset_validation_to_unknown(db)

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT validated, last_validated_version, last_regressed_version, "
        "last_validated_image_version FROM images"
    ).fetchone()
    conn.close()
    assert row == ("unknown", "", "", "")


def test_reset_respects_exclude_states_but_still_clears_markers(tmp_path):
    # An excluded row is left untouched; every other row is reset AND has
    # all its validation markers cleared.
    db = str(tmp_path / "m.db")
    db_manager.initialize(db, "db/schema.sql")
    keep = ("RedHat", "rhel", "9_1", "eastus", "x86_64")
    reset = ("Canonical", "ubuntu-22_04-lts", "server", "eastus", "x86_64")
    db_manager.check_and_upsert(db, *keep[:3], "9.1.0", keep[3], keep[4], "yum", "RHEL 9")
    db_manager.check_and_upsert(db, *reset[:3], "22.04.1", reset[3], reset[4], "apt", "Ubuntu 22.04")
    db_manager.set_validation_state(db, keep, "known_unsupported")
    db_manager.set_validation_state(db, reset, "known_supported", last_validated_version="0.3.458")
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE images SET last_regressed_version='0.3.500', last_validated_image_version='22.04.1' "
        "WHERE sku='server'"
    )
    conn.commit()
    conn.close()

    db_manager.reset_validation_to_unknown(db, exclude_states=("known_unsupported",))

    conn = sqlite3.connect(db)
    kept = conn.execute("SELECT validated FROM images WHERE sku='9_1'").fetchone()[0]
    was_reset = conn.execute(
        "SELECT validated, last_regressed_version, last_validated_image_version "
        "FROM images WHERE sku='server'"
    ).fetchone()
    conn.close()
    assert kept == "known_unsupported"            # excluded row untouched
    assert was_reset == ("unknown", "", "")         # reset + markers cleared


def test_version_tuple_orders_rolling_minors_numerically():
    # '9.10' is newer than '9.8' but sorts below it as a string.
    assert db_manager.version_tuple("9.10.2026062413") > db_manager.version_tuple("9.8.2026062413")
    assert db_manager.version_tuple("24.04.202608280") > db_manager.version_tuple("24.04.202608260")
    assert db_manager.version_tuple("latest") == (0,)
    assert db_manager.version_tuple("") == (0,)


def test_upsert_sees_a_rolling_minor_as_an_update(tmp_path):
    db = str(tmp_path / "m.db")
    db_manager.initialize(db, "db/schema.sql")
    args = ("RedHat", "RHEL", "9-lvm", "eastus")
    assert db_manager.check_and_upsert(db, *args[:3], "9.8.2026062413", args[3],
                                       "x86_64", "yum", "RHEL 9") == db_manager.NEW
    # A string compare ranks 9.10 BELOW 9.8, so this update was silently dropped.
    assert db_manager.check_and_upsert(db, *args[:3], "9.10.2026062413", args[3],
                                       "x86_64", "yum", "RHEL 9") == db_manager.UPDATED

    row = db_manager.get_image_record(db, "RedHat", "RHEL", "9-lvm", "eastus", "x86_64")
    assert row["version"] == "9.10.2026062413"


def test_reset_clears_every_trace_of_the_old_verdict(tmp_path):
    # A reset must leave the row indistinguishable from a first scan, or Gate 3
    # can trust a stale marker and Phase 2 can skip on a stale verdict_source.
    db = str(tmp_path / "m.db")
    db_manager.initialize(db, "db/schema.sql")
    db_manager.check_and_upsert(db, "RedHat", "RHEL", "9-lvm", "9.0.1", "eastus",
                                "x86_64", "yum", "RHEL 9.0")
    ident = ("RedHat", "RHEL", "9-lvm", "eastus", "x86_64")
    db_manager.set_validation_state(db, ident, "known_unsupported",
                                    last_validated_version="0.3.458",
                                    reason="prod repo is missing", verdict_source="lisa")

    assert db_manager.reset_validation_to_unknown(db) == 1

    row = db_manager.get_image_record(db, *ident)
    assert row["validated"] == "unknown"
    assert row["reason"] == ""
    assert row["verdict_source"] == ""
    assert row["last_validated_version"] == ""


def test_legacy_pending_validation_rows_are_released(tmp_path):
    # An older Phase 2 wrote 'pending_validation'; nothing sets or clears it now,
    # and Phase 2 skipped it -- so those rows could never be validated again.
    db = str(tmp_path / "m.db")
    db_manager.initialize(db, "db/schema.sql")
    db_manager.check_and_upsert(db, "RedHat", "RHEL", "9-lvm", "9.0.1", "eastus",
                                "x86_64", "yum", "RHEL 9.0")
    ident = ("RedHat", "RHEL", "9-lvm", "eastus", "x86_64")
    conn = sqlite3.connect(db)          # the state is no longer writable via the API
    conn.execute("UPDATE images SET validated='pending_validation'")
    conn.commit()
    conn.close()

    db_manager.initialize(db, "db/schema.sql")

    assert db_manager.get_image_record(db, *ident)["validated"] == "unknown"


def test_releasing_a_stranded_row_clears_its_validation_markers(tmp_path):
    # A surviving last_validated_version would let Gate 3 trust the row without
    # ever running it -- the opposite of releasing it for validation.
    db = str(tmp_path / "m.db")
    db_manager.initialize(db, "db/schema.sql")
    db_manager.check_and_upsert(db, "RedHat", "RHEL", "8-LVM", "8.9.1", "eastus",
                                "x86_64", "yum", "RHEL 8")
    ident = ("RedHat", "RHEL", "8-LVM", "eastus", "x86_64")
    conn = sqlite3.connect(db)
    conn.execute("""UPDATE images SET validated='pending_validation',
                        last_validated_version='0.3.458', reason='stale',
                        verdict_source='gate', last_regressed_version='0.3.500',
                        last_validated_image_version='8.9.0'""")
    conn.commit()
    conn.close()

    db_manager.initialize(db, "db/schema.sql")

    row = db_manager.get_image_record(db, *ident)
    assert row["validated"] == "unknown"
    for marker in ("last_validated_version", "reason", "verdict_source",
                   "last_regressed_version", "last_validated_image_version"):
        assert row[marker] == "", f"{marker} survived the release"


def test_retired_state_cannot_be_written(tmp_path):
    db = str(tmp_path / "m.db")
    db_manager.initialize(db, "db/schema.sql")
    db_manager.check_and_upsert(db, "RedHat", "RHEL", "9-lvm", "9.0.1", "eastus",
                                "x86_64", "yum", "RHEL 9.0")
    ident = ("RedHat", "RHEL", "9-lvm", "eastus", "x86_64")
    with pytest.raises(ValueError):
        db_manager.set_validation_state(db, ident, "pending_validation")
