#!/usr/bin/env bash
# Genesis's own pre-session net. The seam is the one genesis seeds
# (templates/scripts/pre-session.sh, and the contract in its header holds here
# too: idempotent, fast, non-fatal, deterministic). The content is genesis's.
#
# Start every session on an up-to-date main. The served evolver works in a clone
# that stays on whatever branch the last session left it on, and it reads its
# charter from the working tree. Measured on the first served run: the clone sat
# on the branch of a pull request that had already merged and been deleted, so
# the next session would have read the charter as it stood before the fix that
# merged after it. A dirty tree is left alone, since that's work in progress and
# not ours to move.
set -uo pipefail

branch="$(git branch --show-current 2>/dev/null || true)"
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  echo "pre-session: uncommitted changes on ${branch:-detached HEAD}, leaving the tree where it is"
  exit 0
fi
git fetch -q origin 2>/dev/null || { echo "pre-session: fetch failed, staying on ${branch:-detached HEAD}"; exit 0; }
if [ "$branch" != "main" ]; then
  git switch -q main 2>/dev/null || { echo "pre-session: could not switch to main from $branch"; exit 0; }
fi
if git merge -q --ff-only origin/main 2>/dev/null; then
  echo "pre-session: on main at $(git log --oneline -1)"
else
  echo "pre-session: main has diverged from origin/main, not moving it"
fi
exit 0
