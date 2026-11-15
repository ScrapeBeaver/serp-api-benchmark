#!/usr/bin/env bash
# Publish the summary of all records as one file on its own branch.
#
#   scripts/store_summary.sh [SUMMARY_FILE] [BRANCH] [REMOTE]
#
# Puts SUMMARY_FILE on BRANCH as `summary.json` and pushes it. The branch holds that file
# and a README, in a single commit that is replaced every time: the summary is rebuilt
# from the records after every run, any earlier one can be rebuilt from the records on
# the data branch, and keeping every version would only grow the repository. Whoever
# shows the results reads the file from this branch and needs nothing else.
# Defaults: dist/summary.json, benchmark-summary, origin. REMOTE may be a remote name or
# a URL, which is how this script is tested against a throwaway repository.
#
# The commit is built directly from the file (no checkout, no local branch), so nothing
# in the working tree is touched and the script works the same in CI and on a laptop.
set -euo pipefail

FILE="${1:-dist/summary.json}"
BRANCH="${2:-benchmark-summary}"
REMOTE="${3:-origin}"

if [ ! -s "$FILE" ]; then
  echo "summary: $FILE is missing or empty, nothing to publish" >&2
  exit 1
fi
# A file that is not the summary must never replace the one people read.
if ! python3 - "$FILE" <<'EOF'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
ok = isinstance(data, dict) and isinstance(data.get("providers"), list) and "generated_at" in data
sys.exit(0 if ok else 1)
EOF
then
  echo "summary: $FILE is not a summary (JSON with \`providers\` and \`generated_at\`)" >&2
  exit 1
fi

# Committed as the bot wherever this runs, never with the git identity of the machine.
NAME="${GIT_AUTHOR_NAME:-github-actions[bot]}"
EMAIL="${GIT_AUTHOR_EMAIL:-41898282+github-actions[bot]@users.noreply.github.com}"
export GIT_AUTHOR_NAME="$NAME" GIT_AUTHOR_EMAIL="$EMAIL"
export GIT_COMMITTER_NAME="$NAME" GIT_COMMITTER_EMAIL="$EMAIL"

INDEX="$(mktemp)"
trap 'rm -f "$INDEX"' EXIT
rm -f "$INDEX"

readme() {
  cat <<'EOF'
# SERP benchmark: summary

`summary.json` is every figure of the results page, computed from all the records by the
benchmark's own code after each run. This branch holds only the latest one, in a single
commit that is replaced every time.

The records it is computed from are on the `benchmark-data` branch, and the code that
computes it is on the default branch: `serp-bench site --records <folder>` rebuilds it.
EOF
}

GIT_INDEX_FILE="$INDEX" git read-tree --empty
blob="$(git hash-object -w "$FILE")"
GIT_INDEX_FILE="$INDEX" git update-index --add --cacheinfo "100644,$blob,summary.json"
blob="$(readme | git hash-object -w --stdin)"
GIT_INDEX_FILE="$INDEX" git update-index --add --cacheinfo "100644,$blob,README.md"
tree="$(GIT_INDEX_FILE="$INDEX" git write-tree)"

# [skip ci]: publishing the summary must not start builds of anything.
commit="$(git commit-tree "$tree" -m "summary: $(date -u +%Y-%m-%dT%H:%MZ) [skip ci]")"
# Forced on purpose: the branch is one commit that replaces the one before it.
git push --quiet --force "$REMOTE" "$commit:refs/heads/$BRANCH"
echo "summary: $(wc -c < "$FILE" | tr -d ' ') bytes published on $BRANCH"
