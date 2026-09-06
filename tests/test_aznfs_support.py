from __future__ import annotations

import re

import aznfs_support as m


def test_publish_targets_mirror_packages_csv():
    # Column 1 of AZNFS-mount/packages.csv, minus the EOL CentOS entries.
    assert m.PUBLISH_TARGETS == {
        "Ubuntu": {"18.04", "20.04", "22.04", "24.04", "26.04"},
        "RHEL": {"7.0", "7.3", "8.0", "9.0", "10.0"},
        "Rocky": {"8.0", "9.0"},
        "SUSE": {"15", "16"},
        "Debian": {"13"},
        "Azure Linux": {"3.0"},
    }


def test_scope_is_the_publish_targets_plus_their_bare_major():
    # "RHEL 8" and "RHEL 8.0" both label the 8.0 target; 8.1 is a separate
    # release AzNFS publishes nothing for.
    assert m.SUPPORTED_RHEL == {"7", "7.0", "7.3", "8", "8.0", "9", "9.0", "10", "10.0"}
    assert m.SUPPORTED_ROCKY == {"8", "8.0", "9", "9.0"}
    assert m.SUPPORTED_SLES == {"15", "16"}
    assert m.SUPPORTED_UBUNTU == {"18.04", "20.04", "22.04", "24.04", "26.04"}
    assert m.SUPPORTED_DEBIAN == {"13"}
    assert m.SUPPORTED_AZURELINUX == {"3", "3.0"}


def test_releases_inside_the_matrix():
    for label in ("Ubuntu 22.04", "Ubuntu 26.04", "RHEL 9", "RHEL 9.0",
                  "Rocky 8", "Rocky 9.0", "SLES 15", "SLES 16", "Debian 13",
                  "Azure Linux 3", "Azure Linux 3.0"):
        assert m.is_supported_distro(label), label


def test_releases_outside_the_matrix():
    # Ubuntu interim + retired releases, and families AzNFS does not target.
    for label in ("Ubuntu 25.04", "Ubuntu 25.10", "Ubuntu 26.10", "Ubuntu 16.04",
                  "Ubuntu 14.04", "Debian 11", "Debian 12", "Debian 14",
                  "Azure Linux 2",
                  "CBL-Mariner 2", "openSUSE", "CentOS 7", "Ubuntu Core 24"):
        assert not m.is_supported_distro(label), label


def test_other_rhel_minors_are_out_of_scope():
    # AzNFS publishes to rhel/8.0; rhel/8.1 is its own pocket with no packages.
    for label in ("RHEL 8.1", "RHEL 8.6", "RHEL 9.6", "RHEL 10.2", "RHEL 6.5"):
        assert not m.is_supported_distro(label), label
    for label in ("RHEL 8", "RHEL 8.0", "RHEL 7.3", "RHEL 10.0"):
        assert m.is_supported_distro(label), label


def test_unparseable_labels_are_out_of_scope():
    for label in ("", "Debian", "SUSE Linux", "RHEL"):
        assert not m.is_supported_distro(label)


# ---------------------------------------------------------------------------
# Choosing one representative image per (distro, arch)
# ---------------------------------------------------------------------------

def _numeric(v):
    return tuple(int(p) for p in re.findall(r"\d+", v or ""))


def _img(image, sku, version):
    return {"image": image, "sku": sku, "version": version}


def test_penalty_is_token_aware_not_substring():
    # 'pro' must match ubuntu-pro, not merely appear inside another word.
    assert m.deployability_penalty("ubuntu-22_04-lts", "ubuntu-pro") == 1
    assert m.deployability_penalty("ubuntu-22_04-lts", "server") == 0
    assert m.deployability_penalty("prod-images", "server") == 0


def test_a_plain_sku_beats_a_plan_bearing_one_on_a_tie():
    plain = _img("ubuntu-26_04-lts", "server", "26.04.202609020")
    pro = _img("ubuntu-26_04-lts", "pro-server", "26.04.202609020")
    assert m.is_preferred_image(plain, pro, _numeric)
    assert not m.is_preferred_image(pro, plain, _numeric)


def test_a_plain_sku_beats_a_NEWER_plan_bearing_one():
    # The case that kept selecting images the subscription cannot deploy: pro
    # variants rebuild more often, so version-first would always pick them.
    plain = _img("UbuntuServer", "18_04-lts-arm64", "18.04.202401162")
    newer_pro = _img("0001-com-ubuntu-pro-bionic", "pro-18_04-lts-arm64", "18.04.202608260")
    assert m.is_preferred_image(plain, newer_pro, _numeric)


def test_newest_version_wins_between_equally_deployable_skus():
    older = _img("ubuntu-22_04-lts", "server", "22.04.202608010")
    newer = _img("ubuntu-22_04-lts", "server", "22.04.202609040")
    assert m.is_preferred_image(newer, older, _numeric)


def test_version_compare_is_numeric_not_lexical():
    older = _img("rhel", "9-lvm", "9.8.2026062413")
    newer = _img("rhel", "9-lvm", "9.10.2026062413")
    assert m.is_preferred_image(newer, older, _numeric)


def test_a_full_tie_is_broken_by_name_so_the_pick_never_drifts():
    a = _img("ubuntu-26_04-lts", "server", "26.04.202609020")
    b = _img("ubuntu-26_04-lts", "server-gen1", "26.04.202609020")
    assert m.is_preferred_image(a, b, _numeric)
    assert not m.is_preferred_image(b, a, _numeric)


def test_an_awkward_sku_is_still_chosen_when_it_is_the_only_one():
    # Preference, not exclusion: dropping the distro entirely would be worse
    # than testing the only image it has.
    only = _img("resf-rockylinux-aarch64", "rockylinux-aarch64-8-raw", "8.10.20260709")
    assert m.deployability_penalty(only["image"], only["sku"]) > 0
    chosen = None
    for row in [only]:
        if chosen is None or m.is_preferred_image(row, chosen, _numeric):
            chosen = row
    assert chosen is only
