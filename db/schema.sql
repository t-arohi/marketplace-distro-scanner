-- Schema for marketplace image tracking database.
-- This file is the source of truth; the Python script creates the DB from this on first run.
--
-- Uniqueness: one row per (publisher, image, sku, region, architecture).
-- The `version` column tracks the LATEST version seen for that SKU; older versions
-- never get their own rows (see db_manager.check_and_upsert for dedup logic).

CREATE TABLE IF NOT EXISTS images (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    publisher     TEXT    NOT NULL,
    image         TEXT    NOT NULL,   -- Azure SDK "offer" field (e.g. 0001-com-ubuntu-server-focal)
    sku           TEXT    NOT NULL,   -- Azure SDK "sku"   field (e.g. 20_04-lts-gen2)
    version       TEXT    NOT NULL,   -- Latest known version  (e.g. 20.04.202405010)
    region        TEXT    NOT NULL,   -- Azure region          (e.g. eastus)
    architecture  TEXT    NOT NULL DEFAULT 'x86_64',
                                      -- x86_64 | arm64
    family        TEXT    NOT NULL DEFAULT 'unknown',
                                      -- apt | yum   (package manager kind, drives Phase 2 install commands)
    distro_label  TEXT    NOT NULL DEFAULT '',
                                      -- Human-readable label (e.g. "Ubuntu 24.04", "RHEL 9")
    date_added    TEXT    NOT NULL,   -- ISO8601 UTC; set on insert AND on version bump
    last_modified TEXT    NOT NULL,   -- ISO8601 UTC, updated when version changes
    last_checked  TEXT    NOT NULL,   -- ISO8601 UTC, updated on every scan run
    validated     TEXT    NOT NULL DEFAULT 'unknown',
                                      -- unknown           : not yet handed to Phase 2/3
                                      -- pending_publish   : no code writes this today, but Phase 2
                                      --                     re-queues any row found in it
                                      -- known_supported   : passed Phase 3 LISA test cases
                                      -- known_unsupported : failed at some phase (reason e-mailed)
    last_validated_version TEXT NOT NULL DEFAULT '',
                                      -- AzNFS version Phase 3 last validated on prod (e.g. '0.3.46');
                                      -- empty = never validated. Phase 2 compares the numeric-latest
                                      -- published prod version against this to decide if (re)validation
                                      -- is needed. Compare NUMERICALLY, never as strings.
    last_validated_image_version TEXT NOT NULL DEFAULT '',
                                      -- Marketplace IMAGE version validated at the last pass (e.g.
                                      -- '22.04.202608180'). Phase 2 re-validates when the current image
                                      -- differs AND >= PHASE3_IMAGE_REVALIDATE_DAYS have passed, so OS
                                      -- rebuilds are re-checked without re-running on every daily bump.
    last_regressed_version TEXT NOT NULL DEFAULT '',
                                      -- AzNFS version that FAILED on a distro that was already
                                      -- known_supported. Covers BOTH a package regression and an
                                      -- image regression (the same version failing on a newer
                                      -- marketplace image). The distro stays known_supported at
                                      -- last_validated_version; Phase 2 will NOT re-test this exact
                                      -- version, but a strictly newer package supersedes it
                                      -- (auto-recovery). Cleared on the next pass.
    reason        TEXT    NOT NULL DEFAULT '',
                                      -- Human-readable verdict reason, set by Phase 2/3 when a row is
                                      -- marked known_unsupported (e.g. "prod repo is missing"). Cleared
                                      -- (empty) on known_supported. Surfaced in the monthly digest's
                                      -- known_unsupported table.
    verdict_source TEXT   NOT NULL DEFAULT '',
                                      -- Which phase produced the verdict: 'gate' (Phase 2 repo/package
                                      -- check) or 'lisa' (Phase 3 ran the suite on a VM). Phase 2
                                      -- re-checks its own cheap 'gate' verdicts every run so a stale or
                                      -- transient known_unsupported self-heals, but leaves 'lisa'
                                      -- verdicts alone so a failing distro is not re-provisioned daily.
                                      -- Also holds 'probe_error', which is NOT a verdict: it means the
                                      -- last check could not reach PMC, so `validated` was left as it
                                      -- was and the row is retried next run. Any real verdict clears it.
    UNIQUE(publisher, image, sku, region, architecture)
);

CREATE INDEX IF NOT EXISTS idx_validated    ON images(validated);
CREATE INDEX IF NOT EXISTS idx_region       ON images(region);
CREATE INDEX IF NOT EXISTS idx_publisher    ON images(publisher);
CREATE INDEX IF NOT EXISTS idx_architecture ON images(architecture);
CREATE INDEX IF NOT EXISTS idx_family       ON images(family);

-- Scanner metadata as simple key/value rows (e.g. the calendar month the
-- monthly reminder email was last sent, so daily "nothing new" runs stay
-- silent but a once-a-month snapshot still goes out).
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
