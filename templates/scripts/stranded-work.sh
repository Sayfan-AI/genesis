#!/usr/bin/env bash
# Name local branches holding finished work that nothing else can see.
#
# Under `genesis serve`, sessions commit in checkouts on the operator's machine.
# A session can end on any turn, including the one between its commit and its
# push, and the commit is then on a local branch that no issue, summary, pull
# request list or check knows about. Nothing fails, so nothing reports it.
# Measured on a sibling dev system (genesis issue #156): an evolver commit sat
# unpushed for 16 days, and an interactive session's README fix never got a pull
# request at all. Both were found by hand.
#
# A branch is reported when BOTH hold:
#   - it has commits whose patches aren't on the default branch
#     (`git cherry origin/<default> <branch>` prints a `+` line), and
#   - no pull request, open or closed, ever had it as its head.
# The second condition is what keeps squash-merged branches out: their commits
# never appear on the default branch by patch, but their pull request does.
#
# Read-only and non-fatal, as pre-session.sh requires. It doesn't fetch: it
# compares against the last-fetched remote ref, which can only over-report
# (a stale default branch looks like it's missing commits), and a branch that
# had a pull request is excluded whatever the ref says. If `gh` can't list pull
# requests it says so and reports nothing, rather than naming every
# squash-merged branch as stranded. Works under macOS's bash 3.2.
set -uo pipefail

git rev-parse --git-dir >/dev/null 2>&1 || exit 0

default=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null)
default=${default#origin/}
[ -n "$default" ] || default=main
base="origin/$default"

if ! git rev-parse --verify --quiet "$base" >/dev/null; then
    echo "stranded-work: no $base ref to compare against; skipped"
    exit 0
fi

candidates=""
for branch in $(git for-each-ref --format='%(refname:short)' refs/heads/); do
    [ "$branch" = "$default" ] && continue
    ahead=$(git cherry "$base" "$branch" 2>/dev/null | grep -c '^+')
    [ "${ahead:-0}" -gt 0 ] && candidates="$candidates$branch $ahead
"
done

[ -n "$candidates" ] || exit 0

if ! heads=$(gh pr list --state all --limit 1000 --json headRefName --jq '.[].headRefName' 2>/dev/null); then
    echo "stranded-work: couldn't list pull requests with gh; skipped"
    exit 0
fi

printf '%s' "$candidates" | while read -r branch ahead; do
    [ -n "$branch" ] || continue
    printf '%s\n' "$heads" | grep -qxF -- "$branch" && continue
    echo "STRANDED WORK: local branch '$branch' has $ahead commit(s) not on $base and has never been the head of a pull request. Push it and open a pull request, or say on an issue that the work is abandoned."
done

exit 0
