#!/usr/bin/env bash
# Refresh the STATUS.md page committed in the repo so the current validation
# buckets are visible on GitHub without running anything. Never fails the run.
set -uo pipefail

DB="${DB_PATH:-marketplace.db}"
if [ ! -s "$DB" ]; then
  echo "No database at $DB; skipping status page refresh."
  exit 0
fi

PY="$(command -v python3 || command -v python)"
if ! "$PY" scripts/query_status.py --db "$DB" --format markdown --out STATUS.md; then
  echo "::warning::Could not generate STATUS.md"
  exit 0
fi

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  cat STATUS.md >> "$GITHUB_STEP_SUMMARY"
fi

if [ -z "$(git status --porcelain -- STATUS.md)" ]; then
  echo "STATUS.md is already up to date."
  exit 0
fi

# Generated content goes to its own branch: master is protected (reviews are
# required), so a direct push from CI is rejected with GH006 no matter how many
# times it is retried. Read the page at
# https://github.com/<owner>/<repo>/blob/<STATUS_BRANCH>/STATUS.md
BRANCH="${STATUS_BRANCH:-status-page}"
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add STATUS.md
git commit -m "chore: refresh AzNFS validation status page [skip ci]"

# The branch holds one throwaway commit per refresh, so overwrite it rather than
# rebasing: there is no history worth preserving and nothing else writes here.
if git push --force origin "HEAD:refs/heads/$BRANCH"; then
  echo "Published STATUS.md to $BRANCH."
  exit 0
fi

echo "::warning::Could not push the STATUS.md refresh to $BRANCH"
exit 0
