#!/usr/bin/env bash
# Store the records of a run on the data branch.
#
#   scripts/store_run.sh [OUT_DIR] [BRANCH] [REMOTE]
#
# Adds OUT_DIR/raw/**/run-*.jsonl.gz to BRANCH under raw/, as one commit, and pushes it. The
# data branch holds records only: one file per run, never rewritten. It shares no history
# with the code. Defaults: out, benchmark-data, origin. REMOTE may be a remote name or a
# URL, which is how this script is tested against a throwaway repository.
#
# The commit is built directly from the files (no checkout, no local branch), so nothing
# in the working tree is touched and the script works the same in CI and on a laptop.
set -euo pipefail

OUT="${1:-out}"
BRANCH="${2:-benchmark-data}"
REMOTE="${3:-origin}"

files="$(find "$OUT/raw" -name 'run-*.jsonl.gz' 2>/dev/null | sort || true)"
if [ -z "$files" ]; then
  echo "store: no records under $OUT/raw, nothing to store"
  exit 0
fi

# Records are committed as the bot wherever this runs. The git identity of the machine is
# deliberately not used: a run stored from a laptop must not sign the data with a person.
NAME="${GIT_AUTHOR_NAME:-github-actions[bot]}"
EMAIL="${GIT_AUTHOR_EMAIL:-41898282+github-actions[bot]@users.noreply.github.com}"
export GIT_AUTHOR_NAME="$NAME" GIT_AUTHOR_EMAIL="$EMAIL"
export GIT_COMMITTER_NAME="$NAME" GIT_COMMITTER_EMAIL="$EMAIL"

INDEX="$(mktemp)"
trap 'rm -f "$INDEX"' EXIT

readme() {
  cat <<'EOF'
# SERP benchmark: records

One file per run, written by the benchmark and never edited afterwards:

    raw/YYYY/MM/DD/run-HHMM.jsonl.gz

Each line is one request to one provider: what was asked, when, how long it took, which
check decided its outcome, and the site and position of every result. Addresses are stored
as a fingerprint, not in full, and no provider response is stored here.

`serp-bench verify <file>` recomputes the report of a run from its file.
The code that writes these files is on the default branch of this repository.
EOF
}

for attempt in 1 2 3; do
  parent=""
  rm -f "$INDEX"
  if git fetch --quiet "$REMOTE" "$BRANCH" 2>/dev/null; then
    parent="$(git rev-parse FETCH_HEAD)"
    GIT_INDEX_FILE="$INDEX" git read-tree "$parent"
  else
    echo "store: branch $BRANCH does not exist yet, starting it"
    GIT_INDEX_FILE="$INDEX" git read-tree --empty
    blob="$(readme | git hash-object -w --stdin)"
    GIT_INDEX_FILE="$INDEX" git update-index --add --cacheinfo "100644,$blob,README.md"
  fi

  added=0
  while IFS= read -r file; do
    path="raw/${file#"$OUT/raw/"}"
    blob="$(git hash-object -w "$file")"
    stored="$(GIT_INDEX_FILE="$INDEX" git ls-files -s -- "$path" | awk '{print $2}')"
    if [ -n "$stored" ]; then
      # A run is written once. The same name with other contents is a clash, not an update.
      if [ "$stored" != "$blob" ]; then
        echo "store: $path is already on $BRANCH with different contents" >&2
        exit 1
      fi
      continue
    fi
    GIT_INDEX_FILE="$INDEX" git update-index --add --cacheinfo "100644,$blob,$path"
    added=$((added + 1))
  done <<< "$files"

  if [ "$added" -eq 0 ] && [ -n "$parent" ]; then
    echo "store: everything is already on $BRANCH"
    exit 0
  fi

  tree="$(GIT_INDEX_FILE="$INDEX" git write-tree)"
  # [skip ci]: a data commit must not start builds of anything.
  message="records: $added run file(s), $(date -u +%Y-%m-%dT%H:%MZ) [skip ci]"
  if [ -n "$parent" ]; then
    commit="$(git commit-tree "$tree" -p "$parent" -m "$message")"
  else
    commit="$(git commit-tree "$tree" -m "$message")"
  fi
  if git push --quiet "$REMOTE" "$commit:refs/heads/$BRANCH"; then
    echo "store: $added run file(s) pushed to $BRANCH"
    # In CI: tell the next step that there is something new to publish.
    if [ -n "${GITHUB_OUTPUT:-}" ]; then echo "stored=true" >> "$GITHUB_OUTPUT"; fi
    exit 0
  fi
  echo "store: push rejected (attempt $attempt): someone else pushed first, trying again" >&2
  sleep $((attempt * 3))
done
echo "store: could not push to $BRANCH after 3 attempts" >&2
exit 1
