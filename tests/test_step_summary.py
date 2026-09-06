from __future__ import annotations

import scan_marketplace


_ROLLUP = [
    {
        "family": "debian",
        "distro_label": "Ubuntu 24.04",
        "publishers": ["Canonical"],
        "architectures": ["x86_64", "arm64"],
        "sku_count": 3,
    }
]


def test_summary_renders_a_row_per_release(tmp_path, monkeypatch):
    out = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(out))

    scan_marketplace.write_step_summary(_ROLLUP, total_tracked=42)

    text = out.read_text()
    assert "1 distro release(s) tracked" in text
    assert "| Ubuntu 24.04" in text
    assert "Canonical" in text


def test_summary_reports_an_empty_backlog(tmp_path, monkeypatch):
    out = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(out))

    scan_marketplace.write_step_summary([], total_tracked=42)

    assert "0 distro release(s) awaiting validation" in out.read_text()


def test_summary_is_a_no_op_outside_actions(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    scan_marketplace.write_step_summary(_ROLLUP, total_tracked=42)  # must not raise


def test_a_broken_summary_never_fails_the_scan(monkeypatch):
    # Reporting is not worth losing a scan over, so the writer swallows errors.
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", "/nonexistent-dir/summary.md")

    scan_marketplace.write_step_summary(_ROLLUP, total_tracked=42)  # must not raise
