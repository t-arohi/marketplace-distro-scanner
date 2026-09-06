"""The distro releases AzNFS targets - the single source of truth for scope.

Mirrors the publish targets in AZNFS-mount/packages.csv, minus the EOL CentOS
entries (CentOS-7.0 / CentOS-8.0), which Phase 1 drops upstream anyway.

Phase 1 applies this to its Phase 2 hand-off, so anything outside the matrix is
still discovered, stored and reported -- just never validated, because a missing
AzNFS package there is expected rather than a finding. Phase 2 then validates
whatever it is handed and does not re-judge scope.
"""

import re

# packages.csv column 1, verbatim, so this can be diffed against the source.
PUBLISH_TARGETS = {
    "Ubuntu": {"18.04", "20.04", "22.04", "24.04", "26.04"},
    "RHEL": {"7.0", "7.3", "8.0", "9.0", "10.0"},
    "Rocky": {"8.0", "9.0"},
    "SUSE": {"15", "16"},
    "Debian": {"13"},
    "Azure Linux": {"3.0"},
}


def _accepted_versions(family: str) -> set[str]:
    """Publish targets plus their bare-major spelling.

    The marketplace labels the same release either way ("RHEL 8" and "RHEL 8.0"
    are both the 8.0 target), so both forms are accepted. A different minor is
    NOT: 8.1 is its own release and AzNFS publishes nothing for it.
    """
    accepted = set()
    for target in PUBLISH_TARGETS[family]:
        accepted.add(target)
        major, _, minor = target.partition(".")
        if minor == "0":
            accepted.add(major)
    return accepted


SUPPORTED_UBUNTU = _accepted_versions("Ubuntu")   # {"18.04", ... } exact releases
SUPPORTED_RHEL = _accepted_versions("RHEL")       # {"7", "7.0", "7.3", "8", "8.0", ...}
SUPPORTED_ROCKY = _accepted_versions("Rocky")     # {"8", "8.0", "9", "9.0"}
SUPPORTED_SLES = _accepted_versions("SUSE")       # {"15", "16"}
SUPPORTED_DEBIAN = _accepted_versions("Debian")   # {"13"}
SUPPORTED_AZURELINUX = _accepted_versions("Azure Linux")  # {"3", "3.0"}

OUT_OF_MATRIX_REASON = "outside the AzNFS support matrix"


def _label_version(label: str) -> str:
    major, minor = major_minor(label)
    if not major:
        return ""
    return f"{major}.{minor}" if minor else major


def major_minor(label: str) -> tuple[str, str]:
    m = re.search(r"(10|\d+)(?:\.(\d+))?", label or "")
    if not m:
        return "", ""
    return m.group(1), m.group(2) or ""


def is_supported_distro(label: str) -> bool:
    """True when AzNFS publishes for exactly this distro release.

    A different minor of a supported major is NOT in scope: AzNFS publishes to
    rhel/8.0, and rhel/8.1 is a separate release carrying no AzNFS.
    """
    s = (label or "").strip().lower()
    ver = _label_version(s)
    if not ver:
        return False

    if "ubuntu" in s:
        return ver in SUPPORTED_UBUNTU
    if "debian" in s:
        return ver in SUPPORTED_DEBIAN
    if "azure linux" in s or "azurelinux" in s:
        return ver in SUPPORTED_AZURELINUX
    if "rhel" in s or "redhat" in s or "red hat" in s:
        return ver in SUPPORTED_RHEL
    if "rocky" in s:
        return ver in SUPPORTED_ROCKY
    if "sles" in s or "suse" in s:
        return ver in SUPPORTED_SLES
    return False


# SKU/offer name fragments that make an image harder or impossible to deploy.
# `pro`, `cvm` and friends carry a marketplace PLAN, so the subscription must
# accept terms first -- which this pipeline's identity cannot do. The rest are
# special-purpose builds nobody means to certify against.
DISPREFERRED_SKU_TOKENS: tuple[str, ...] = (
    "pro", "cvm", "confidential", "minimal", "cis", "fips", "byos", "raw", "daily",
)


def deployability_penalty(image: str, sku: str) -> int:
    """How many awkward markers an image carries -- lower is more deployable.

    Token-aware, so `pro` matches `ubuntu-pro` and `pro-server` but not a name
    that merely contains those letters.
    """
    tokens = set(re.split(r"[^a-z0-9]+", f"{image} {sku}".lower()))
    return sum(1 for t in DISPREFERRED_SKU_TOKENS if t in tokens)


def is_preferred_image(row: dict, cur: dict, version_tuple) -> bool:
    """True when ``row`` is a better representative for a (distro, arch) than ``cur``.

    Deployability outranks recency, deliberately: an image whose plan we cannot
    accept produces no result at all, so a slightly older image we can actually
    boot is worth more than the newest one we cannot. Across the tracked fleet
    26 distro/arch pairs have a `pro`/`raw`/`daily` SKU strictly newer than the
    best plain one, so ordering by version first would keep selecting images
    that fail on marketplace terms.

    Then newest version, numerically -- '9.10.2026...' is newer than '9.8.2026...'
    but sorts BELOW it as a string. Then name, because ties are the norm (a
    distro's SKUs are rebuilt together) and without a final tie-break the winner
    came down to DB row order, so the image under test drifted between runs and
    the results looked like regressions.

    ``version_tuple`` is injected because Phase 1 and Phase 2 each have their own
    copy and neither can import the other's.
    """
    row_penalty = deployability_penalty(row.get("image", ""), row.get("sku", ""))
    cur_penalty = deployability_penalty(cur.get("image", ""), cur.get("sku", ""))
    if row_penalty != cur_penalty:
        return row_penalty < cur_penalty

    newer = version_tuple(row.get("version", ""))
    current = version_tuple(cur.get("version", ""))
    if newer != current:
        return newer > current

    return (row.get("image", ""), row.get("sku", "")) < (cur.get("image", ""), cur.get("sku", ""))
