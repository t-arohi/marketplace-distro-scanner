# Modifying the code - a developer how-to

A task-oriented map of "I want to change X -> edit Y". Every entry names the
exact file and symbol so you can make the change without reading the whole
codebase. Paths are relative to the repo root.

## Quick reference

| I want to... | Edit | Symbol |
|---|---|---|
| Add / remove a marketplace publisher | `scripts/config.py` (+ `src/phase2/distro_map.yaml`) | `PUBLISHERS` |
| Change the regions scanned | `scripts/config.py` | `REGIONS` |
| Change the prod content server (base URL) | repo variable `PROD_REPO_BASE` (or `pmc_packages.py`) | `PROD_BASE` |
| Change the package directory / query URL on prod | `src/phase2/pmc_packages.py` | `aznfs_dir_url()` |
| Change the version format / "latest" lineage | `src/phase2/pmc_packages.py` | `AZNFS_SERIES`, `in_series()` |
| Change the supported-distro allow-list | `src/phase2/orchestrator.py` | `_SUPPORTED_*`, `_is_aznfs_supported_distro()` |
| Change the support-matrix (packages.csv) source | `src/phase2/orchestrator.py` | `_AZNFS_PACKAGES_CSV_URL`, `_packages_csv_mentions_distro()` |
| Add a Phase 3 test suite | `phase3/testsuites/`, `phase3/runbooks/`, `phase3/run_phase3.py` | `_BASE_RUNBOOK` |
| Change what Phase 2 hands to Phase 3 | `src/phase2/orchestrator.py` | `_make_lisa_job()` |
| Exclude some offers / distros | `scripts/config.py` | `EXCLUDED_OFFER_SUBSTRINGS`, `EXCLUDED_DISTRO_PREFIXES` |
| Change e-mail recipients | repo variable `NOTIFY_RECIPIENTS` (fallback in `config.py`) | - |
| Change the daily schedule | `.github/workflows/scan-marketplace.yml` | `cron` |
| Change the DB schema / states | `db/schema.sql`, `scripts/db_manager.py` | - |

---

## 1. How to add more publishers

Phase 1 scans `region x publisher` from the Marketplace API. Two (sometimes
three) edits:

1. **`scripts/config.py` -> `PUBLISHERS`** - add the publisher's *Marketplace API
   name* (e.g. `Canonical`, `resf`, `AlmaLinux`):

   ```python
   PUBLISHERS = [
       "Canonical", "RedHat", "SUSE", "Debian", "resf", "MicrosoftCBLMariner",
       "AlmaLinux",          # <-- new
   ]
   ```

2. **`src/phase2/distro_map.yaml`** - so the new publisher's images resolve to a
   PMC `<distro>` path segment. Add under `publishers:` (fallback) and/or
   `labels:` (keyword match, checked first):

   ```yaml
   labels:
     - [almalinux, alma, yum]     # distro_label keyword -> [segment, index]
   ```

3. **(If the distro label comes out wrong)** - `scripts/scan_marketplace.py` ->
   `derive_family_and_distro_label()` builds the human label (e.g. `RHEL 9`) from
   the publisher/offer/sku. Add a branch if a new publisher needs custom parsing.

> Publishers are **shared by every package** - one crawl feeds all of Phase 2.
> Adding a publisher does not require any Phase 2/3 change by itself.

## 2. How to change the query URL on prod

The prod URL is built in layers in `src/phase2/pmc_packages.py`:

- **Base server:** `PROD_BASE = os.environ.get("PROD_REPO_BASE") or "https://packages.microsoft.com"`.
  Override with the **`PROD_REPO_BASE`** repo variable to point at a different
  content server (e.g. a staging PMC) - no code change needed.
- **Repo path:** `repo_base_url(distro, version)` -> `{base}/<distro>/<version>/prod/`.
- **Package directory:** `aznfs_dir_url(distro, version, family)`:

  ```python
  def aznfs_dir_url(distro, version, family, base=PROD_BASE):
      root = repo_base_url(distro, version, base)   # .../<distro>/<version>/prod/
      if index_kind(family) == "yum":
          return root + "Packages/a/"               # yum
      return root + "pool/main/a/aznfs/"            # apt  <-- package dir here
  ```

  To query a different package, change the apt subdir (`pool/main/a/<name>/`) and
  the yum path. The `<distro>` segment itself comes from `distro_map.yaml`
  (see #1).

## 3. How to change the version format for "latest"

PMC prod lists many lineages of a package side by side (e.g. aznfs `0.3.9` and
`3.0.18`). "Latest" = numeric-max **within a tracked series**. In
`src/phase2/pmc_packages.py`:

```python
AZNFS_SERIES = "0.3"        # AzNFS tracks 0.3.x ; azfilesauth would be "1.0"

def in_series(version, series=AZNFS_SERIES):
    # True when version's major.minor matches the series (0.3.458 in "0.3")
    ...
```

- Change `AZNFS_SERIES` to switch lineage - e.g. `"1.0"` for azfilesauth (tracks
  1.0.x), or `""`/`None` to mean "no series filter, pure numeric-max".
- The picker itself (`max(..., key=version_tuple)` over files passing
  `in_series`) does not change - only the series constant does.
- `_AZNFS_VERSION_RE` and `file_arch()` parse the filename (`aznfs_<ver>_<arch>.deb`).
  If the package name/prefix differs, update these too.

## 4. How to add more test suites (Phase 3)

A LISA suite is three pieces:

1. **The suite** - `phase3/testsuites/<name>_validation.py` (a LISA `TestSuite`
   subclass). Model it on `aznfs_validation.py`; keep the `[Tier N: step]`
   assertion tags so the driver can attribute failures.
2. **The runbook** - `phase3/runbooks/<name>_validation.yml`, referencing the new
   suite.
3. **Wire it into the driver** - `phase3/run_phase3.py` currently hardcodes:

   ```python
   _BASE_RUNBOOK = _HERE / "runbooks" / "aznfs_validation.yml"
   ...
   cmd = ["lisa", "run", "-r", str(_BASE_RUNBOOK)] + _overrides(job, ...)
   ```

   To support more than one, select the runbook per job/package (e.g. from a
   `job.product` field or a `{product: runbook}` map) instead of always using
   `_BASE_RUNBOOK`.

The verdict recording (`phase3/orchestrator/record_result.py`) and the one
summary e-mail are suite-agnostic and need no change.


### 5. Change which distro versions are "supported"
`src/phase2/orchestrator.py` -> `_SUPPORTED_UBUNTU/_RHEL/_ROCKY/_SLES` and
`_is_aznfs_supported_distro()`. A distro outside these sets is marked
`known_unsupported` at Gate 2.

### 6. Change the support-matrix (packages.csv) source
`src/phase2/orchestrator.py` -> `_AZNFS_PACKAGES_CSV_URL` +
`_packages_csv_mentions_distro()` (a live `requests.get` to the CSV). Point at a
different matrix URL/parser for a different package.

### 7. Exclude offers or distros from tracking
`scripts/config.py`:
- `EXCLUDED_OFFER_SUBSTRINGS` (default `advanced-sla`) - skip marketplace offers
  whose name matches (comma-separated, env-overridable).
- `EXCLUDED_DISTRO_PREFIXES` (default `centos`) - drop rows whose `distro_label`
  starts with these, in both the hand-off and the cached DB.

### 8. Change what Phase 2 emits to Phase 3
`src/phase2/orchestrator.py` -> `_make_lisa_job()` builds each job dict
(`publisher, image, sku, version, region, arch, distro_label, aznfs_package_url,
aznfs_version`). Add/rename fields here; Phase 3 (`run_phase3.py`) reads them.

### 9. Change the schedule / trigger
`.github/workflows/scan-marketplace.yml` -> `on.schedule.cron` (kept off the hour
and off midnight on purpose - GitHub's scheduler is best-effort). Phase 2/3 chain
off Phase 1 via `workflow_run`; each also has a manual `workflow_dispatch`.

### 10. Change e-mail recipients / content
Recipients: the `NOTIFY_RECIPIENTS` repo variable (Phase 2/3); Phase 1 falls back
to the default list in `scripts/config.py`. Formatting/tables live in
`scripts/notifier.py` (`send_phase1_summary`, `send_monthly_reminder`,
`send_phase2_summary`) and `phase3/orchestrator/record_result.py`.

### 11. Change the DB schema or validation states
`db/schema.sql` (authoritative, lazy-migrated at runtime) and
`scripts/db_manager.py`. In practice three states are written: `unknown`,
`known_supported`, `known_unsupported`. `pending_publish` is a summary-e-mail
label that no code currently writes, but the schema permits it and Phase 2
re-queues any row found in that state.

### 12. Run a phase manually
```bash
gh workflow run scan-marketplace.yml      # Phase 1 (accepts emit_backlog input)
gh workflow run phase2-publish.yml        # Phase 2 (uses latest Phase 1 artifact)
gh workflow run phase3-validate.yml       # Phase 3 (uses latest Phase 2 artifact)
```

---

## Where each phase lives (orientation)

```
scripts/                     Phase 1 + shared helpers
  config.py                  publishers, regions, exclusions, paths
  scan_marketplace.py        Phase 1 entry point + distro_label deriver
  db_manager.py              SQLite ops + validation states
  notifier.py                ACS e-mail (all phases)
src/phase2/                  Phase 2 (PMC prod validation)
  pmc_packages.py            prod URL builder + version series + filename parsing
  orchestrator.py            the 3 gates + support policy + _make_lisa_job
  distro_map.yaml            distro_label/publisher -> PMC <distro> segment
  run.py                     Phase 2 entry point
phase3/                      Phase 3 (LISA validation)
  run_phase3.py              driver (_BASE_RUNBOOK, per-distro lisa run)
  testsuites/                the LISA suites
  runbooks/                  the LISA runbooks
  orchestrator/record_result.py   DB verdict + summary e-mail
```

To extend the pipeline to a **new package** (Azure Files Authenticator, AOD)
rather than tweak an existing behaviour, see
[`EXTENDING-TO-AZFILESAUTH-AOD.md`](EXTENDING-TO-AZFILESAUTH-AOD.md).
