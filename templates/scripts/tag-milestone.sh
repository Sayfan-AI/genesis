#!/usr/bin/env bash
# Tag every signed-off milestone, so `git checkout milestone-3` is a thing you can
# actually do six weeks later.
#
# Idempotent and self-detecting on purpose: run it with no arguments and it works
# out which milestones are done and untagged, from repository state. The
# alternative — an agent remembering to tag at the right moment — is the shape
# that keeps failing here. A milestone sign-off happens once, in a run that is
# also planning the next milestone, and "also create a tag" is exactly the step
# that gets dropped when the run is busy or dies partway through. Nothing ever
# reports a missing tag, because a missing tag looks like nothing at all.
#
# The signal is the seeded human gate: the orchestrator opens ONE
# "Milestone N complete" issue labelled `needs:human` and stops, and the human
# closing it is the sign-off. So a closed completion issue is the fact, and the
# tag is derived from it - no judgment, no memory.
#
# Tags an existing commit only. It never creates one, so running it on a dirty
# tree is safe: the tag lands on whatever HEAD is, which for a signed-off
# milestone is the merge that finished it.
set -euo pipefail

ONLY="${1:-}"

# `--json` rather than parsing titles out of a table: a title with a comma or a
# tab breaks the table and this has to be boring.
closed=$(gh issue list --state closed --limit 200 --json number,title,closedAt \
    --jq '.[] | select(.title | test("^Milestone [0-9]+ complete";"i")) |
          "\(.title|capture("(?<n>[0-9]+)").n)"' | sort -un)

if [ -z "$closed" ]; then
    echo "no signed-off milestones yet"
    exit 0
fi

git fetch --tags --quiet origin || true

tagged_any=0
for n in $closed; do
    if [ -n "$ONLY" ] && [ "$n" != "$ONLY" ]; then
        continue
    fi
    tag="milestone-$n"
    if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
        continue
    fi
    # Annotated, not lightweight: an annotated tag carries who and when, which is
    # the whole reason for wanting the tag at all.
    git tag -a "$tag" -m "Milestone $n complete"
    if git push --quiet origin "$tag"; then
        echo "tagged $tag at $(git rev-parse --short HEAD)"
        tagged_any=1
    else
        # A tag that exists only locally is worse than none: it makes this script
        # skip the milestone forever while nobody else can see it.
        git tag -d "$tag" >/dev/null
        echo "could not push $tag - removed the local tag so the next run retries" >&2
    fi
done

if [ "$tagged_any" -eq 0 ]; then
    echo "every signed-off milestone is already tagged"
fi
