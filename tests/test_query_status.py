from __future__ import annotations

import db_manager
import query_status


def _db(tmp_path) -> str:
    db = str(tmp_path / "m.db")
    db_manager.initialize(db, "db/schema.sql")
    rows = [
        (("Canonical", "ubuntu-24_04-lts", "server", "eastus", "x86_64"),
         "24.04.1", "Ubuntu 24.04", "known_supported", ""),
        (("Canonical", "ubuntu-24_04-lts", "server-arm64", "eastus", "arm64"),
         "24.04.1", "Ubuntu 24.04", "unknown", ""),
        # Debian 13, not 11: the fixture needs a distro IN the support matrix,
        # since out-of-matrix releases are no longer reported at all.
        (("Debian", "debian-13", "13", "eastus", "x86_64"),
         "13.0.1", "Debian 13", "known_unsupported", "prod repo is missing"),
        (("OpenLogic", "centos", "7_9", "eastus", "x86_64"),
         "7.9.1", "CentOS 7", "known_unsupported", "EOL"),
    ]
    for ident, version, label, state, reason in rows:
        db_manager.check_and_upsert(db, *ident[:3], version, ident[3], ident[4], "apt", label)
        if state != "unknown":
            db_manager.set_validation_state(db, ident, state, reason=reason)
    return db


def test_buckets_match_the_monthly_digest_rollup(tmp_path):
    buckets = query_status.load_buckets(_db(tmp_path))

    assert [d["distro_label"] for d in buckets["known_supported"]] == ["Ubuntu 24.04"]
    assert [d["distro_label"] for d in buckets["unknown"]] == ["Ubuntu 24.04"]
    unsupported = buckets["known_unsupported"]
    assert [d["distro_label"] for d in unsupported] == ["Debian 13"]  # CentOS excluded
    assert unsupported[0]["reason"] == "prod repo is missing"


def test_state_and_distro_filters(tmp_path):
    db = _db(tmp_path)

    only_unsupported = query_status.load_buckets(db, states=("known_unsupported",))
    assert set(only_unsupported) == {"known_unsupported"}

    ubuntu = query_status.load_buckets(db, distro="ubuntu")
    assert ubuntu["known_unsupported"] == []
    assert [d["distro_label"] for d in ubuntu["known_supported"]] == ["Ubuntu 24.04"]


def test_include_excluded_restores_filtered_distros(tmp_path):
    buckets = query_status.load_buckets(_db(tmp_path), include_excluded=True)

    assert [d["distro_label"] for d in buckets["known_unsupported"]] == ["CentOS 7", "Debian 13"]


def test_skus_listing_reports_per_row_state(tmp_path):
    rows = query_status.matching_skus(_db(tmp_path), distro="ubuntu")

    assert [(r["sku"], r["state"]) for r in rows] == [
        ("server-arm64", "unknown"),
        ("server", "known_supported"),
    ]


def test_text_output_lists_counts_and_reasons(tmp_path):
    text = query_status.render_text(query_status.load_buckets(_db(tmp_path)))

    assert "[Known supported] (1)" in text
    assert "[Known unsupported] (1)" in text
    assert "prod repo is missing" in text


def test_main_reports_missing_database(tmp_path, capsys):
    assert query_status.main(["--db", str(tmp_path / "nope.db")]) == 2
    assert "Database not found" in capsys.readouterr().err


def test_reasons_are_redacted_before_they_reach_the_published_page(tmp_path):
    # Phase 3 copies Azure errors verbatim into `reason`; STATUS.md is readable
    # by anyone, so subscription/principal identifiers must not survive.
    db = str(tmp_path / "m.db")
    db_manager.initialize(db, "db/schema.sql")
    ident = ("resf", "rockylinux-aarch64", "8", "eastus", "arm64")
    db_manager.check_and_upsert(db, *ident[:3], "8.1", ident[3], ident[4], "yum", "Rocky 8")
    db_manager.set_validation_state(
        db, ident, "known_unsupported",
        reason=("deployment failed: client 'ea2ea2c0-c588-498a-984e-a12e390743b5' with "
                "object id 'd9e60338-84b5-4168-ac3b-f39bf22470a3' cannot act over scope "
                "'/subscriptions/8ffe006d-4aa2-4eb6-bc3c-f33092ef804a/providers/x'"),
    )

    reason = query_status.load_buckets(db)["known_unsupported"][0]["reason"]

    assert "ea2ea2c0" not in reason
    assert "d9e60338" not in reason
    assert "8ffe006d" not in reason
    assert "deployment failed" in reason  # the useful part survives


def test_markdown_is_deterministic_so_the_page_only_changes_with_the_data(tmp_path):
    # A baked-in timestamp would rewrite STATUS.md on every run and bury real
    # changes under commit churn.
    buckets = query_status.load_buckets(_db(tmp_path))

    assert query_status.render_markdown(buckets) == query_status.render_markdown(buckets)


def test_out_of_matrix_distros_are_not_reported(tmp_path):
    # They are scanned and stored but never validated, so listing them as
    # "not yet validated" promises a backlog that does not exist.
    db = _db(tmp_path)
    db_manager.check_and_upsert(db, "Debian", "debian-11", "11", "11.0.1",
                                "eastus", "x86_64", "apt", "Debian 11")

    buckets = query_status.load_buckets(db)
    every = [d["distro_label"] for rows in buckets.values() for d in rows]

    assert "Debian 11" not in every


def test_include_excluded_still_shows_them(tmp_path):
    db = _db(tmp_path)
    db_manager.check_and_upsert(db, "Debian", "debian-11", "11", "11.0.1",
                                "eastus", "x86_64", "apt", "Debian 11")

    buckets = query_status.load_buckets(db, include_excluded=True)
    every = [d["distro_label"] for rows in buckets.values() for d in rows]

    assert "Debian 11" in every


def test_the_page_and_the_monthly_digest_stay_in_step(tmp_path):
    # Both read the same rollup; filtering in one and not the other would let
    # the e-mail and the page disagree about what is outstanding.
    import db_manager, status_rollup
    db = _db(tmp_path)
    page = query_status.load_buckets(db)
    digest = status_rollup.buckets_by_state(db_manager.get_all_records(db))

    assert {s: [d["distro_label"] for d in rows] for s, rows in page.items() if rows} == \
           {s: [d["distro_label"] for d in rows] for s, rows in digest.items() if rows}
