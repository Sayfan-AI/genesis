#!/usr/bin/env bash
# Genesis failure escalation — open, or update, a `needs:human` issue when an
# autonomous run dies.
#
# There is no model in this path and there must never be one. The failure it
# exists for is a run that hit `error_max_turns`, and an agent told to "fix the
# problem, and tell a human if you can't" spends its last turn on the fixing and
# never reaches the telling — so the one thing a stalled loop owes a person is
# precisely the thing that gets dropped. Measured on MaKlaude: a T8 e2e failure
# stalled milestone 1, the orchestrator run sent to triage it died at max-turns,
# and the system then sat looking idle — no fix, no escalation, no ping — until a
# human went looking. A shell script has no budget to run out of.
#
# Called from an `if: failure()` step in each workflow that can fail with work
# left undone. Deliberately NOT a `workflow_run` watcher workflow, which is the
# obvious design and the wrong one twice over: a watcher can only test the
# conclusion inside a job, so GitHub queues and skips a run for every completion
# it watches (~26 skipped runs on MaKlaude from watching four busy workflows),
# and a watcher also fires on a benign concurrency *cancellation*, which
# `if: failure()` doesn't.
#
# Required env:
#   GH_TOKEN   a token with `issues: write` — each caller's App token
#   GH_REPO    owner/repo
#   WF_NAME    the failing workflow's name (`${{ github.workflow }}`)
#   RUN_URL    the failed run's URL
# Optional env:
#   GENESIS_RUN_STARTED            ISO8601 UTC — when the failed run began
#   GENESIS_ARTIFACT_LOOKBACK_MIN  fallback window in minutes (default 120)
set -euo pipefail

: "${GH_TOKEN:?GH_TOKEN required}"
: "${GH_REPO:?GH_REPO required}"
WF_NAME="${WF_NAME:-unknown workflow}"
RUN_URL="${RUN_URL:-(run url unavailable)}"
LOOKBACK_MIN="${GENESIS_ARTIFACT_LOOKBACK_MIN:-120}"

GATE_LABEL="needs:human"
# The second label is what keeps this script from wedging the loop it protects.
# The scheduled orchestrator skips its LLM step whenever a `needs:human` issue is
# open, so an escalation carrying only that label would silence the cron until a
# person acted — and a single transient API error would cost a dev system every
# scheduled tick until someone noticed the silence, which is the failure mode
# issue #27 is about, reintroduced from the other side. `automation:failure` is
# how the gate tells "a person must decide something" apart from "a run died, and
# the next one may well not"; see the gate step in genesis-orchestrator.yml.
CLASS_LABEL="automation:failure"

# `gh issue create --label` resolves every name to a label ID and fails the
# entire call if one doesn't exist ("could not add label"), and a freshly
# scaffolded repo has neither of these. The agent paths get away with assuming
# the labels exist because a model that hits that error creates the label and
# retries; this is the path that can't improvise, and it's the path that only
# ever runs when something else has already failed. `|| true` because "already
# exists" is the normal answer here, not a problem.
gh label create "$GATE_LABEL" --color B60205 \
    --description "Waiting on a person" >/dev/null 2>&1 || true
gh label create "$CLASS_LABEL" --color D93F0B \
    --description "An autonomous run failed" >/dev/null 2>&1 || true

# "The run died" isn't the question a human actually has — "did anything land,
# or is the repo where it was?" is. A run that hits max-turns has usually already
# produced its deliverable and lost only the wrap-up, and an escalation that says
# only "run failed" costs a person a manual hunt for work that's already sitting
# in an open PR.
if [ -z "${GENESIS_RUN_STARTED:-}" ]; then
    # GNU `date` on the runner, BSD `date` on the macOS laptop that runs the unit
    # tests and `genesis serve`. Neither accepts the other's flag, so try both
    # rather than shipping a script that works in exactly one of the two modes
    # this system is expected to run in.
    since="$(date -u -d "-${LOOKBACK_MIN} minutes" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
        || date -u -v"-${LOOKBACK_MIN}M" +%Y-%m-%dT%H:%M:%SZ)"
else
    since="$GENESIS_RUN_STARTED"
fi

# The REST issues endpoint, not `gh issue list --search`: search is index-lagged
# by a minute or two and the artifacts worth reporting were created seconds
# before the run died, so search would routinely miss exactly the interesting
# ones. `since` + `sort=updated` is served from the primary and is exact. The
# endpoint returns pull requests too — a PR is an issue with a `pull_request`
# key — so one call covers both.
artifacts="$(gh api --method GET "repos/${GH_REPO}/issues" \
    -f state=all -f sort=updated -f direction=desc -f per_page=30 -f since="$since" \
    --jq '.[] | "- \(if .pull_request then "PR" else "Issue" end) #\(.number) (\(.state)) — \(.title)\n  \(.html_url)"' \
    2>/dev/null || true)"

if [ -n "$artifacts" ]; then
    landed="$(printf 'Touched since %s — **triage these before assuming the run achieved nothing.** If the deliverable is already here (a green pull request, a posted diagnosis), the run lost only its wrap-up: finish the bookkeeping rather than redoing the work.\n\n%s' \
        "$since" "$artifacts")"
else
    landed="$(printf 'Nothing was touched since %s. Check for a pushed branch with no pull request before concluding the run left no trace.' \
        "$since")"
fi

# Dedup is PER WORKFLOW, not global. An earlier MaKlaude version reused any open
# `automation:failure` issue, so two different workflows failing in the same
# window landed in one issue and a human had to untangle which failure belonged
# to which — and closing it for one workflow closed it while the other's failure
# was still live. Keying on the workflow name gives at most one open issue per
# workflow: bounded issue count, and a per-workflow failure cadence the evolver
# can read to tell a recurring failure from a one-off. Hidden HTML comment so the
# key never renders but stays greppable in the body.
marker="<!-- genesis-failure-wf: ${WF_NAME} -->"

body="$(printf 'A workflow run failed and the loop couldn'"'"'t advance on its own.\n\n- Workflow: **%s**\n- Failed run: %s\n\n### What this run may already have landed\n\n%s\n\n### Why this issue isn'"'"'t a gate\n\nIt carries `%s` so a person can find it, and `%s` so it doesn'"'"'t silence the scheduled orchestrator. A run that died isn'"'"'t the same claim as a decision only a person can make, and the next scheduled run may well succeed. If it fails again this issue gains a comment rather than a twin, so the comment count is the failure cadence.\n\n%s' \
    "$WF_NAME" "$RUN_URL" "$landed" "$GATE_LABEL" "$CLASS_LABEL" "$marker")"

# `|| true` on the lookup, and it isn't laziness: if the query fails we would
# rather file a duplicate escalation than none. This script only ever runs when
# something has already gone wrong, so its own error handling has to fail toward
# telling a human, never toward silence.
existing="$(gh issue list --state open --label "$CLASS_LABEL" --json number,body \
    | jq -r --arg m "$marker" '[.[] | select((.body // "") | contains($m)) | .number] | first // empty' \
    || true)"

if [ -n "$existing" ]; then
    gh issue comment "$existing" --body "$body"
else
    gh issue create \
        --title "Autonomous system needs help: ${WF_NAME} run failed" \
        --label "$GATE_LABEL" --label "$CLASS_LABEL" \
        --body "$body"
fi
