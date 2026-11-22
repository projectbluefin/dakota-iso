#!/usr/bin/env bash
# Publish one E2E leg's screenshots to a branch that always holds exactly one
# parentless commit, replaced on every publish, so the branch never grows.
#
# Usage: publish_screenshots.sh REMOTE BRANCH RUN_DIR LEG_DIR SRC_DIR
#   REMOTE   git URL to push to, credentials included
#   BRANCH   branch name, e.g. ci-screenshots
#   RUN_DIR  path in the branch shared by every leg of one workflow run, e.g.
#            luks-e2e/<run id>. The only path carried over from the current
#            tip; everything else (older runs) is dropped.
#   LEG_DIR  path in the branch for this leg, under RUN_DIR, e.g.
#            luks-e2e/<run id>/dakota/dev. Replaced by SRC_DIR's files.
#   SRC_DIR  local directory whose regular files are published.
#
# Matrix legs of one run publish concurrently. Each push is leased on the tip
# it read, so a leg that loses the race re-reads the tip (now carrying the
# other leg's files) and tries again instead of overwriting it.
#
# Prints the published commit SHA. Publishes nothing when SRC_DIR is empty.
set -euo pipefail

if [[ $# -ne 5 ]]; then
  echo "usage: $0 REMOTE BRANCH RUN_DIR LEG_DIR SRC_DIR" >&2
  exit 2
fi

remote=$1
branch=$2
run_dir=${3%/}
leg_dir=${4%/}
src_dir=$(realpath -- "$5")
attempts=${PUBLISH_ATTEMPTS:-5}
retry_delay=${PUBLISH_RETRY_DELAY:-5}

case "$leg_dir/" in
  "$run_dir"/?*) ;;
  *)
    echo "LEG_DIR ($leg_dir) must be under RUN_DIR ($run_dir)" >&2
    exit 2
    ;;
esac

files=()
for f in "$src_dir"/*; do
  if [[ -f "$f" ]]; then files+=("$f"); fi
done
if [[ ${#files[@]} -eq 0 ]]; then
  echo "No screenshots in ${src_dir}; nothing to publish" >&2
  exit 0
fi

export GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-github-actions[bot]}"
export GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-41898273+github-actions[bot]@users.noreply.github.com}"
export GIT_COMMITTER_NAME="${GIT_COMMITTER_NAME:-$GIT_AUTHOR_NAME}"
export GIT_COMMITTER_EMAIL="${GIT_COMMITTER_EMAIL:-$GIT_AUTHOR_EMAIL}"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
git init -q "$work"
cd "$work"

for ((attempt = 1; attempt <= attempts; attempt++)); do
  old=""
  rc=0
  git ls-remote --exit-code "$remote" "refs/heads/${branch}" >/dev/null || rc=$?
  if [[ $rc -eq 0 ]]; then
    git fetch -q --depth=1 "$remote" "+refs/heads/${branch}:refs/tip"
    old=$(git rev-parse refs/tip)
  elif [[ $rc -ne 2 ]]; then
    echo "Cannot read ${branch} from remote (git ls-remote exit ${rc})" >&2
    exit 1
  fi

  rm -f .git/index
  git read-tree --empty
  if [[ -n "$old" ]] && git cat-file -e "${old}:${run_dir}" 2>/dev/null; then
    git read-tree --prefix="${run_dir}/" "${old}:${run_dir}"
    git rm -r -q --cached --ignore-unmatch -- "${leg_dir}"
  fi
  for f in "${files[@]}"; do
    blob=$(git hash-object -w -- "$f")
    git update-index --add --cacheinfo "100644,${blob},${leg_dir}/$(basename -- "$f")"
  done

  tree=$(git write-tree)
  commit=$(git commit-tree "$tree" -m "E2E screenshots: ${leg_dir}")

  # An empty lease value means "the branch must not exist yet".
  if git push -q --force-with-lease="refs/heads/${branch}:${old}" \
      "$remote" "${commit}:refs/heads/${branch}"; then
    echo "$commit"
    exit 0
  fi
  echo "Push to ${branch} rejected (attempt ${attempt}/${attempts}); re-reading tip" >&2
  sleep "$((retry_delay * attempt))"
done

echo "Gave up publishing to ${branch} after ${attempts} attempts" >&2
exit 1
