#!/usr/bin/env bash
# Genesis issue manager — abstraction over gh CLI
# Supports: create, list, unanswered-comments, unselectable-work, red-prs, close,
#           assign, comment, label, claim, next, release, sweep-claims, view
set -euo pipefail

CMD="${1:-help}"
shift || true

# JSON fields to fetch for list/view queries
FIELDS="number,title,state,url,labels,assignees,createdAt,updatedAt"

# How far back a trailing human comment still counts as unanswered. A week-old
# comment nothing ever replied to has either been handled out of band or stopped
# mattering, and a section that reprints it forever is the noise that teaches the
# loop to skip the whole report.
DEFAULT_COMMENT_WINDOW_DAYS="${GENESIS_COMMENT_WINDOW_DAYS:-7}"

format_issues() {
    python3 -c "
import sys, json
issues = json.load(sys.stdin)
for i in issues:
    labels = ','.join(l['name'] for l in i.get('labels', []))
    assignees = ','.join(a['login'] for a in i.get('assignees', []))
    parts = [f'#{i[\"number\"]}', f'[{i[\"state\"]}]', i['title']]
    if labels:
        parts.append(f'({labels})')
    if assignees:
        parts.append(f'-> {assignees}')
    print(' '.join(parts))
" 2>/dev/null || cat
}

# A human said something and nothing has answered it.
#
# Every other safety net a dev system gets keys on CI state, issue/PR state, or
# run outcome. None of them keys on *a person having said something*. Measured on
# Sayfan-AI/MaKlaude (2026-08-02, UTC, MaKlaude issue #141):
#
#   06:18:37  bot: "the last done criterion is implemented — PR #154", CI running
#   06:29:18  HUMAN approves the approach and attaches two conditions
#   06:31:43  MaKlaude PR #154 merges on its own green checks
#   06:31:44  MaKlaude issue #141 closed
#
# The comment sat unread for 2m25s, and then the work it constrained was merged
# and its issue closed. Two of the three cases it asked for never shipped.
# Nothing was broken: the merge runner gates on exactly two facts — bot author,
# green checks — and reads no comment at any point, while every label-driven
# report can only see `needs:human`, which nobody had applied to 400 words of
# conditions. So this is derived from repo state and printed unconditionally,
# like the other backstops. Deliberately NOT a new label convention: "humans must
# label a comment that carries conditions" is an opt-in invariant, and the member
# who forgets is a person.
#
# The rule needs no judgment: a thread's NEWEST comment is human-authored. The
# care goes into the exclusions, because this prints every tick and a backstop
# that cries wolf is one the loop learns to skip:
#
#   - bot-authored newest comment  — the loop has replied; nothing is waiting.
#   - older than the window        — see DEFAULT_COMMENT_WINDOW_DAYS.
#   - closed, comment AFTER close  — that is a closing note ("LGTM", "signed
#                                    off", "Closing."), which is what a trailing
#                                    human comment almost always is.
#   - closed by a human            — a person who comments and then closes their
#                                    own thread has answered themselves.
#
# What survives is exactly one closed shape: the human spoke, and then the LOOP
# closed the thread over them. That is MaKlaude issue #141 precisely, and it
# stays actionable (reopen, or answer and reopen) after the close that hides the
# thread from every other section. Replayed against MaKlaude's full comment
# history, these four exclusions turned 18 candidates into 0 reports.
#
# Known boundary, stated rather than silently missed: only conversation comments
# are read (`issues/N/comments`, which covers PRs too). Inline review comments and
# review bodies live on a different endpoint.
#
# Known limitation: "a bot replied last" is a proxy for "answered". A bot reply
# that doesn't address the human's point clears the flag. Clearing it on purpose
# costs one comment; clearing it by accident at least requires the loop to have
# said something.
#
# Local-mode caveat: when `genesis serve` can't mint an App token it runs the
# session on the operator's own gh credential, so that session's comments are
# authored by the human and read as unanswered until something replies. The
# closed branch is unaffected — it requires a bot closer.
#
# Prints nothing when nothing is waiting. If the API can't be read it says so
# instead of printing nothing, because silence here would read as all-clear.
format_unanswered_comments() {
    python3 - "$1" <<'PY'
import json
import subprocess
import sys
from datetime import datetime, timezone

window_days = int(sys.argv[1])
now = datetime.now(timezone.utc)


def ts(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def gh_json(path):
    proc = subprocess.run(['gh', 'api', path], capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


# GitHub App comments carry type "Bot"; the [bot] login suffix is the belt to
# that suspenders, since a PAT-backed bot account has neither.
def is_bot(actor):
    if not isinstance(actor, dict):
        return False
    return actor.get('type') == 'Bot' or str(actor.get('login', '')).endswith('[bot]')


def ago(delta):
    secs = int(delta.total_seconds())
    if secs >= 86400:
        return '%dd' % (secs // 86400)
    if secs >= 3600:
        return '%dh' % (secs // 3600)
    return '%dm' % max(secs // 60, 0)


# One repo-wide call for the most recent comments, which is also the bound on the
# work: a thread whose newest comment falls off this page has had no recent
# conversation, and that is the same thing the window means.
comments = gh_json('repos/{owner}/{repo}/issues/comments?sort=created&direction=desc&per_page=100')
if comments is None:
    print('(the comments API could not be read, so this check did not run — '
          'do NOT read the empty section above it as all-clear)')
    sys.exit(0)

# Newest comment per thread. The feed is served newest-first, but the ordering is
# re-established here rather than assumed: taking the wrong comment as "newest"
# would silently invert every verdict below.
newest = {}
for c in comments:
    if not isinstance(c, dict) or not c.get('created_at'):
        continue
    tail = str(c.get('issue_url', '')).rsplit('/', 1)[-1]
    if not tail.isdigit():
        continue
    num = int(tail)
    prior = newest.get(num)
    if prior is None or ts(c['created_at']) > ts(prior['created_at']):
        newest[num] = c

rows = []
for num, c in newest.items():
    if is_bot(c.get('user')):
        continue
    created = ts(c['created_at'])
    age = now - created
    if age.total_seconds() >= window_days * 86400:
        continue

    thread = gh_json('repos/{owner}/{repo}/issues/%d' % num)
    if thread is None:
        continue

    note = ''
    if thread.get('state') != 'open':
        closed_at = thread.get('closed_at')
        if not closed_at or ts(closed_at) <= created:
            continue
        if not is_bot(thread.get('closed_by')):
            continue
        note = '  [the loop closed this over them — reopen or answer]'

    rows.append((age, num, c, thread, note))

# Stalest first: the comment that has gone unanswered longest is the one being
# forgotten.
rows.sort(key=lambda r: -r[0].total_seconds())

for age, num, c, thread, note in rows:
    kind = 'PR' if thread.get('pull_request') else 'issue'
    print('#%d  unanswered %s — @%s on %s "%s"%s\n      %s' % (
        num, ago(age), (c.get('user') or {}).get('login', '?'),
        kind, thread.get('title', ''), note, c.get('html_url', '')))
PY
}

# ----- claims -----
#
# `in-progress` is the whole concurrency protocol: it is what `next` reads to
# skip an issue somebody is already on. The label alone carries no identity and
# no timestamp, so it looks identical whether a live session applied it four
# seconds ago or a session that has since been killed applied it an hour ago.
# That ambiguity is why nothing could ever safely take one back, and why
# MaKlaude issue #195 stayed unselectable with no session alive to work it.
#
# The marker below is the missing half: a comment naming the session that
# claimed the issue, which turns "somebody has this" into "session X has this,
# since 02:18". `release` uses the identity, `sweep-claims` uses the timestamp.
# It goes at the END of the comment body on purpose — a line that *starts* with
# `<!--` opens a markdown HTML block, and everything after the `-->` on that
# line then renders as raw text rather than prose.
CLAIM_MARKER="genesis-claim"

# How long a claim may outlive the session holding it before the backstop sweep
# takes it back. This is the only place age decides anything, and the window has
# to clear the longest a session can legitimately hold a claim — the control
# plane's session cap, `GENESIS_SESSION_TIMEOUT`, one hour by default. A shorter
# window races a slow but healthy run and hands its issue to a second worker:
# two branches, a merge conflict, and neither run aware of the other, which is
# strictly worse than the invisibility the sweep exists to fix.
DEFAULT_CLAIM_STALE_HOURS=2

claim_session() {
    # Who the caller is, or nothing when the caller has no identity to offer.
    #
    # The control plane exports GENESIS_SESSION once per continuation chain, so a
    # chain the ladder declines to resume can find exactly the claims it made and
    # nothing else. GitHub Actions has no ladder but does have a run id, which is
    # the same kind of handle.
    #
    # Empty is a real answer and not a failure: `claim` records it as
    # `unattributed`, and `sweep-claims` uses emptiness to mean "I hold no claims,
    # so exempt none". Substituting a placeholder here instead would make an
    # anonymous sweeper exempt every anonymous claim - the exact set that most
    # needs sweeping.
    if [ -n "${GENESIS_SESSION:-}" ]; then
        # Restricted charset because the identity round-trips through a marker
        # parsed by a whitespace-delimited regex.
        printf '%s' "$GENESIS_SESSION" | tr -c 'A-Za-z0-9._-' '-'
    elif [ -n "${GITHUB_RUN_ID:-}" ]; then
        printf 'gha-%s-%s' "$GITHUB_RUN_ID" "${GITHUB_RUN_ATTEMPT:-1}"
    fi
}

claim_rows() {
    # Every live claim, one tab-separated line each: issue number, claiming
    # session, age in whole seconds. An `in-progress` issue carrying no marker
    # reads `-` and `-1`; the placeholder is not cosmetic, because tab is IFS
    # whitespace and `read` collapses two adjacent tabs into one delimiter, which
    # would silently shift an empty session's age into the session field.
    #
    # The LAST marker wins. An issue can be claimed, released and re-claimed, and
    # only the most recent claim is the live one — scoring by the first would
    # release a fresh claim on the strength of an ancient abandoned one.
    gh issue list --state open --label in-progress --json number,comments --limit 100 \
        --jq '
.[] | . as $i
| ([$i.comments[]? | select(.body | test("<!-- '"$CLAIM_MARKER"' "))] | last) as $c
| [ $i.number,
    (if $c == null then "-" else ($c.body | capture("<!-- '"$CLAIM_MARKER"' session=(?<s>[^ ]+)").s) end),
    (if $c == null then -1 else ((now - ($c.createdAt | fromdateiso8601)) | floor) end)
  ] | @tsv'
}

format_red_prs() {
    # Open pull requests the merge sweep will never take, because a check on them
    # has concluded as something other than a pass.
    #
    # This exists because the event that would otherwise report a red check does
    # not reach every execution mode. `genesis-ci-failure.yml` wakes triage within
    # seconds of CI going red, and under `genesis serve` every `genesis-*`
    # workflow is disabled, so in the mode an operator may actually be running
    # there is no wake-on-failure at all. A red pull request is a fact about
    # repository state, so deriving it from state works in both modes — the same
    # reason the merge sweep has a cron next to its `workflow_run` trigger.
    #
    # "Red" is defined as the exact complement of the merge predicate in
    # genesis-merge.yml: concluded, and neither SUCCESS nor SKIPPED. Anything
    # narrower (matching only FAILURE) would leave a timed-out or errored check
    # invisible here while still blocking the merge forever, which is the stall
    # with no reporter. PENDING is the one legacy status state that is non-empty
    # and still means "running", so it is spelled out rather than inferred.
    #
    # Two more ways a pull request is unmergeable forever, neither of which
    # involves a red check, and both of which were silent until they were added
    # here (#33). The merge predicate refuses BOTH on purpose:
    #
    #   - **No checks at all.** genesis-merge.yml never merges an empty rollup,
    #     because on a repo that has CI an empty one almost always means CI hasn't
    #     started. That's right for a fresh pull request and wrong forever after:
    #     a workflow that was never wired, or whose trigger doesn't match the
    #     branch, produces a pull request nothing will ever look at again. Hence
    #     the grace window - before it, silence is CI starting; after it, silence
    #     is all there's ever going to be.
    #   - **A conflicting branch.** Nothing in this system rebases, so it stays
    #     conflicting until a person or an agent acts.
    #
    # Each row says which of the three it is, because the fix differs: a red check
    # wants a look at the failure, an empty rollup wants a look at the workflow
    # triggers, and a conflict wants a rebase.
    gh pr list --state open --limit 100 \
        --json number,title,url,isDraft,mergeable,createdAt,statusCheckRollup \
        --jq '
def check_state: if (.conclusion // "") != "" then .conclusion else (.state // "") end;
def blocking: check_state as $s
  | $s != "" and $s != "PENDING" and $s != "SUCCESS" and $s != "SKIPPED";
($ENV.GENESIS_PR_GRACE_HOURS // "1" | tonumber) as $grace
| .[]
| . as $pr
# `try`, because one unparseable timestamp must not take the whole listing down
# with it. Age only ever gates the no-checks branch, so falling back to 0 costs
# at most a quiet row - a report that dies on a single odd value costs all of them.
| (try ((now - ($pr.createdAt | fromdateiso8601)) / 3600) catch 0) as $age
| [$pr.statusCheckRollup[]? | select(blocking)] as $red
| (if ($red | length) > 0 then
       "red: " + ([$red[] | .name // .context // "check"] | join(", "))
   elif ($pr.statusCheckRollup | length) == 0 and $age >= $grace then
       "no checks have reported in \($age | floor)h - nothing will ever merge this"
   elif $pr.mergeable == "CONFLICTING" then
       "conflicts with the base branch"
   else null end) as $why
| select($why != null)
| "#\($pr.number) \(if $pr.isDraft then "[draft] " else "" end)\($pr.title)\n  \($pr.url)\n  \($why)"'
}

release_one() {
    # Remove the label, then say why on the issue. The comment is not decoration:
    # a human looking at an issue whose `in-progress` label vanished has no other
    # way to tell a release from an agent quietly un-labelling something.
    if ! gh issue edit "$1" --remove-label in-progress >/dev/null; then
        echo "issues.sh: could not remove in-progress from #$1" >&2
        return 1
    fi
    gh issue comment "$1" --body \
        "Claim released: $2. \`in-progress\` is off, so this issue is selectable again." \
        >/dev/null || true
    echo "released #$1"
}

# Open work the loop's own selection rules can never choose.
#
# Every other section of `summary` keys on a state that needs someone to ACT —
# it's blocked, nobody answered — or lists what changed. None of them keys on
# SELECTABILITY: whether a run can ever pick the item up at all. An issue filed
# with no `milestone:N` label is in no such state. It sits in the Open Issues
# listing looking completely healthy, and by every question the other nets ask,
# it is.
#
# Why that's fatal here specifically: the orchestrator's hard rules say milestone
# work outranks discretionary work, and that a discretionary finding is "filed and
# moved on" from. So an unmilestoned issue is filed and then nothing ever selects
# it. Measured in Sayfan-AI/MaKlaude on 2026-08-15 — three issues died that way in
# a single day. MaKlaude issue #167 dropped out of milestone 5 when the label came
# off and sat untouched until a human re-added it; MaKlaude issue #186 was filed
# unmilestoned and never picked up; MaKlaude issue #202 became selectable only
# because the human labelled it by hand. Each one looked filed and was abandoned.
#
# The adjacent case is the same defect one step later: an open issue whose
# milestone has already been signed off. The completion gate is closed, the loop
# has moved on, and nothing comes back for it.
#
# This is a detector rather than a sharper prompt because the evidence is an
# ABSENCE spread over days — no run failed, no check went red, nothing looped — so
# no single agent cycle holds the history to notice "this has been unselectable
# since Tuesday". The instruction half already exists (the orchestrator's "every
# issue you file gets a milestone:N label"); this is the half that works when the
# instruction is forgotten.
#
# Because it prints every tick, the exclusions carry more weight than the
# detections — a backstop that cries wolf is one the loop learns to skip:
#
#   - needs:human            a person is holding it, deliberately outside the
#                            milestone task flow: plan gates, completion gates,
#                            and escalations, which carry this label too, so one
#                            exemption covers them all.
#   - genesis:onboarding     issue #1 PRODUCES the roadmap, so it predates every
#                            milestone by construction and can never carry one.
#   - wontfix/duplicate/invalid
#                            "we're deliberately not doing this" is a legitimate
#                            third answer to why an issue carries no milestone.
#                            Without it, the only way to silence a true-but-
#                            unhelpful report is to close the issue and lose the
#                            record of the decision.
#
# Deliberately NOT exempt: `needs:evolver`. Routing a finding to the framework
# doesn't make its local half selectable — MaKlaude issue #202 sat unmilestoned
# carrying exactly that kind of framework-facing finding.
#
# Every unknown resolves toward silence, for the same reason: an OPEN completion
# gate means the milestone is still live, and one active milestone on a
# multi-labelled issue is enough. If the issue list can't be read it says so
# rather than printing nothing, because silence here reads as all-clear.
format_unselectable_work() {
    python3 - <<'PY'
import json
import re
import subprocess
import sys
from datetime import datetime, timezone

MILESTONE = re.compile(r'^milestone:(\d+)$')
# The completion gate as the orchestrator's hard rules mandate it: one
# "Milestone N complete" issue, labelled needs:human. Only a CLOSED one counts —
# an open gate means the milestone hasn't been signed off yet.
SIGNED_OFF = re.compile(r'^milestone\s+(\d+)\s+complete\b', re.IGNORECASE)

EXEMPT = {'needs:human', 'genesis:onboarding', 'wontfix', 'duplicate', 'invalid'}


def gh_issues(state, limit):
    proc = subprocess.run(
        ['gh', 'issue', 'list', '--state', state, '--limit', str(limit),
         '--json', 'number,title,state,labels,createdAt'],
        capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


open_issues = gh_issues('open', 100)
# A deeper page for the closed set: the completion gates looked up here are as
# old as the project, while the open set is bounded by what's in flight.
closed_issues = gh_issues('closed', 200)
if open_issues is None or closed_issues is None:
    print('(the issue list could not be read, so this check did not run — '
          'do NOT read the empty section above it as all-clear)')
    sys.exit(0)

signed_off = {}
for i in closed_issues:
    m = SIGNED_OFF.match(str(i.get('title') or '').strip())
    if m and m.group(1) not in signed_off:
        signed_off[m.group(1)] = i['number']

now = datetime.now(timezone.utc)

rows = []
for i in open_issues:
    labels = [l['name'] for l in i.get('labels') or []]
    if any(l in EXEMPT for l in labels):
        continue

    milestones = sorted({m.group(1) for m in (MILESTONE.match(l) for l in labels) if m})
    if not milestones:
        reason = 'no milestone:N label, so no run can select it'
    else:
        gates = [(n, signed_off.get(n)) for n in milestones]
        # One active milestone is enough to keep it reachable.
        if any(gate is None for _, gate in gates):
            continue
        reason = '%s already signed off (%s), so nothing will come back for it' % (
            ', '.join('milestone:%s' % n for n, _ in gates),
            ', '.join('#%d' % gate for _, gate in gates))

    created = datetime.fromisoformat(i['createdAt'].replace('Z', '+00:00'))
    rows.append(((now - created).days, i, reason, labels))

# Stalest first, like every other report here: the item that has been
# unreachable longest is the one being forgotten.
rows.sort(key=lambda r: -r[0])

for age, i, reason, labels in rows:
    suffix = ' (%s)' % ','.join(labels) if labels else ''
    print('#%d  open %dd — %s%s\n      %s' % (
        i['number'], age, i.get('title', ''), suffix, reason))
PY
}

case "$CMD" in
    create)
        TITLE="" LABELS="" BODY="" MILESTONE="" ASSIGNEE=""
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --title) TITLE="$2"; shift 2 ;;
                --labels) LABELS="$2"; shift 2 ;;
                --body) BODY="$2"; shift 2 ;;
                --milestone) MILESTONE="$2"; shift 2 ;;
                --assignee) ASSIGNEE="$2"; shift 2 ;;
                *) shift ;;
            esac
        done
        if [ -z "$TITLE" ]; then
            echo "Usage: issues.sh create --title TITLE [--labels LABELS] [--body BODY] [--milestone N] [--assignee USER]" >&2
            exit 1
        fi
        ARGS=(issue create --title "$TITLE")
        [ -n "$LABELS" ] && ARGS+=(--label "$LABELS")
        [ -n "$BODY" ] && ARGS+=(--body "$BODY")
        # Add milestone label
        [ -n "$MILESTONE" ] && ARGS+=(--label "milestone:$MILESTONE")
        [ -n "$ASSIGNEE" ] && ARGS+=(--assignee "$ASSIGNEE")
        gh "${ARGS[@]}"
        ;;

    list)
        STATE="open" LABEL="" ASSIGNEE="" SINCE="" SEARCH=""
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --status) STATE="$2"; shift 2 ;;
                --milestone) LABEL="milestone:$2"; shift 2 ;;
                --label) LABEL="$2"; shift 2 ;;
                --assignee) ASSIGNEE="$2"; shift 2 ;;
                --since) SINCE="$2"; shift 2 ;;
                --search) SEARCH="$2"; shift 2 ;;
                --all) STATE="all"; shift ;;
                *) shift ;;
            esac
        done
        ARGS=(issue list --state "$STATE" --json "$FIELDS" --limit 100)
        [ -n "$LABEL" ] && ARGS+=(--label "$LABEL")
        [ -n "$ASSIGNEE" ] && ARGS+=(--assignee "$ASSIGNEE")
        [ -n "$SEARCH" ] && ARGS+=(--search "$SEARCH")

        if [ -n "$SINCE" ]; then
            # Filter by updated date using jq-style python filtering
            gh "${ARGS[@]}" | python3 -c "
import sys, json
from datetime import datetime, timedelta, timezone

since_str = '$SINCE'
# Parse relative time like '24 hours ago', '7 days ago'
parts = since_str.split()
if len(parts) == 3 and parts[2] == 'ago':
    n = int(parts[0])
    unit = parts[1].rstrip('s')
    if unit == 'hour':
        cutoff = datetime.now(timezone.utc) - timedelta(hours=n)
    elif unit == 'day':
        cutoff = datetime.now(timezone.utc) - timedelta(days=n)
    elif unit == 'week':
        cutoff = datetime.now(timezone.utc) - timedelta(weeks=n)
    else:
        cutoff = datetime.min.replace(tzinfo=timezone.utc)
else:
    cutoff = datetime.fromisoformat(since_str.replace('Z', '+00:00'))

issues = json.load(sys.stdin)
filtered = [i for i in issues if datetime.fromisoformat(i['updatedAt'].replace('Z', '+00:00')) >= cutoff]
json.dump(filtered, sys.stdout)
" | format_issues
        else
            gh "${ARGS[@]}" | format_issues
        fi
        ;;

    unanswered-comments)
        # Threads whose newest comment is a person's and that the loop hasn't
        # answered (empty = nobody is waiting on a reply). State-derived, so it
        # doesn't depend on a run having been handed the comment as its trigger —
        # which is the only way MaKlaude issue #141 was ever recovered, and it was
        # luck.
        WINDOW_DAYS="$DEFAULT_COMMENT_WINDOW_DAYS"
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --window-days) WINDOW_DAYS="$2"; shift 2 ;;
                *) shift ;;
            esac
        done
        format_unanswered_comments "$WINDOW_DAYS"
        ;;

    unselectable-work)
        # Open issues the work-selection rules can never choose — no milestone:N
        # label, or every milestone on them already signed off (empty = every
        # open issue is reachable). Not a distress state: these look healthy
        # under Open Issues, which is exactly why they get abandoned.
        format_unselectable_work
        ;;

    red-prs)
        # Pull requests stalled on a red check (empty = nothing is stuck).
        format_red_prs
        ;;

    blocked)
        # Shortcut: list all blocked issues
        gh issue list --state open --label "blocked" --json "$FIELDS" --limit 100 | format_issues
        ;;

    recent)
        # Shortcut: recently updated issues (last 24h by default)
        HOURS="${1:-24}"
        gh issue list --state all --json "$FIELDS" --limit 100 | python3 -c "
import sys, json
from datetime import datetime, timedelta, timezone
cutoff = datetime.now(timezone.utc) - timedelta(hours=$HOURS)
issues = json.load(sys.stdin)
filtered = [i for i in issues if datetime.fromisoformat(i['updatedAt'].replace('Z', '+00:00')) >= cutoff]
json.dump(filtered, sys.stdout)
" | format_issues
        ;;

    summary)
        # Overview of project state: open, blocked, recently closed
        echo "=== Open Issues ==="
        gh issue list --state open --json "$FIELDS" --limit 100 | format_issues
        echo ""
        # Unconditional, for the one input none of the other sections can see: a
        # person having spoken. The rest are derived from issue state, so a
        # comment carrying conditions on work in flight reaches no run unless it
        # happens to be that run's trigger. Empty here means nobody is waiting on
        # a reply — which is why it prints even when empty.
        echo "=== Unanswered Human Comments (a person spoke; answer before acting) ==="
        format_unanswered_comments "$DEFAULT_COMMENT_WINDOW_DAYS"
        echo ""
        # Unconditional, and the one section that asks a different question of
        # the board. The others ask "what needs someone to act?" or "what
        # changed?"; this asks "what can the loop never even choose?" — an issue
        # with no milestone:N label is listed under Open Issues above like any
        # other while being unreachable by the orchestrator's own priority
        # rules, which is how three issues were filed and then abandoned in a
        # single day in a sibling dev system. Empty here means every open issue
        # is reachable.
        echo "=== Unselectable Work (open but no run can pick it up) ==="
        format_unselectable_work
        echo ""
        # Also unconditional, and the same shape one step further along the
        # pipeline: unselectable work is work no run can start, this is work no
        # run can finish. Auto-merge fires on a check going green and nothing
        # fires on one going red, so a pull request with a failing check is
        # finished work that will never land and never asks anyone for anything.
        # Empty here means nothing is stuck.
        echo "=== Stalled Pull Requests (red, unchecked, or conflicting - nothing will merge these) ==="
        format_red_prs
        echo ""
        echo "=== Blocked ==="
        gh issue list --state open --label "blocked" --json "$FIELDS" --limit 100 | format_issues
        echo ""
        echo "=== Recently Closed (7 days) ==="
        gh issue list --state closed --json "$FIELDS" --limit 100 | python3 -c "
import sys, json
from datetime import datetime, timedelta, timezone
cutoff = datetime.now(timezone.utc) - timedelta(days=7)
issues = json.load(sys.stdin)
filtered = [i for i in issues if datetime.fromisoformat(i['updatedAt'].replace('Z', '+00:00')) >= cutoff]
json.dump(filtered, sys.stdout)
" | format_issues
        ;;

    close)
        ID="" REASON=""
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --id) ID="$2"; shift 2 ;;
                --reason) REASON="$2"; shift 2 ;;
                *) shift ;;
            esac
        done
        if [ -z "$ID" ]; then
            echo "Usage: issues.sh close --id ID [--reason REASON]" >&2
            exit 1
        fi
        ARGS=(issue close "$ID")
        [ -n "$REASON" ] && ARGS+=(--reason "$REASON")
        gh "${ARGS[@]}"
        ;;

    assign)
        ID="" TO=""
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --id) ID="$2"; shift 2 ;;
                --to) TO="$2"; shift 2 ;;
                *) shift ;;
            esac
        done
        if [ -z "$ID" ] || [ -z "$TO" ]; then
            echo "Usage: issues.sh assign --id ID --to ASSIGNEE" >&2
            exit 1
        fi
        gh issue edit "$ID" --add-assignee "$TO"
        ;;

    label)
        # Repeated flags ACCUMULATE. They used to overwrite two scalars, so
        # `label --id 199 --remove in-progress --remove needs:human` printed the
        # issue URL, exited 0, and removed only the last one. The caller had no
        # way to tell a full removal from a partial one, and the label the loop
        # relies on to say "someone is working this" was the one silently left
        # behind.
        # Counters rather than ${#ARRAY[@]}: this runs under `set -u` on bash 3.2
        # (macOS ships it), where expanding an EMPTY array is an unbound-variable
        # error. The first version of this fix used ${#ADD[@]} and broke exactly
        # the remove-only call the bug was reported for. Same for the loops below,
        # which use the ${ARRAY[@]+...} guard for the same reason.
        ID="" ADD=() REMOVE=() N_ADD=0 N_REMOVE=0
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --id) ID="$2"; shift 2 ;;
                --add) ADD+=("$2"); N_ADD=$((N_ADD + 1)); shift 2 ;;
                --remove) REMOVE+=("$2"); N_REMOVE=$((N_REMOVE + 1)); shift 2 ;;
                *) shift ;;
            esac
        done
        if [ -z "$ID" ]; then
            echo "Usage: issues.sh label --id ID [--add LABEL]... [--remove LABEL]..." >&2
            exit 1
        fi
        if [ "$N_ADD" -eq 0 ] && [ "$N_REMOVE" -eq 0 ]; then
            echo "issues.sh label: nothing to do, pass --add and/or --remove" >&2
            exit 1
        fi
        # One gh call per label rather than a comma-joined list: a single bad
        # label in a joined list fails the whole edit, and a partial failure that
        # reports success is the bug this rewrite exists to remove.
        rc=0
        for l in ${ADD[@]+"${ADD[@]}"};       do gh issue edit "$ID" --add-label    "$l" || rc=1; done
        for l in ${REMOVE[@]+"${REMOVE[@]}"}; do gh issue edit "$ID" --remove-label "$l" || rc=1; done
        exit $rc
        ;;

    claim)
        # Mark an issue as being worked, and PROVE it stuck.
        #
        # "The agent should label it in-progress" is a rule a model follows most of
        # the time, and most of the time is not a state machine. The label is what
        # the loop, the human, and `summary` all read to answer "is someone on
        # this", so a silently missing label makes the whole board lie. This does
        # the write and then reads it back, so a failure is loud and the caller can
        # tell success from a no-op.
        ID=""
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --id) ID="$2"; shift 2 ;;
                *) shift ;;
            esac
        done
        if [ -z "$ID" ]; then
            echo "Usage: issues.sh claim --id ID" >&2
            exit 1
        fi
        gh issue edit "$ID" --add-label in-progress >/dev/null || {
            echo "issues.sh claim: could not add in-progress to #$ID" >&2
            exit 1
        }
        if ! gh issue view "$ID" --json labels --jq '[.labels[].name]|index("in-progress")' \
             | grep -qv '^null$'; then
            echo "issues.sh claim: in-progress did not stick on #$ID; refusing to report success" >&2
            exit 1
        fi
        # Record WHO claimed it, not just that somebody did. Without this the
        # label is anonymous, and an anonymous claim is one nothing can hand back:
        # `release --session` can't match it and `sweep-claims` can't date it, so
        # it sits on the board until a human notices, which is the failure this
        # whole mechanism exists to remove.
        SESSION="$(claim_session)"
        [ -z "$SESSION" ] && SESSION="unattributed"
        if ! gh issue comment "$ID" --body \
            "Claimed by session \`$SESSION\`. It's released when that session ends without \
finishing, so \`in-progress\` means work is under way right now. <!-- $CLAIM_MARKER session=$SESSION -->" \
            >/dev/null; then
            # Give the label back rather than hold a claim nobody can release.
            # Refusing an unattributable claim is the same discipline as refusing
            # a label that didn't stick — a claim the machinery can't undo is not
            # a claim, it's a leak.
            gh issue edit "$ID" --remove-label in-progress >/dev/null || true
            echo "issues.sh claim: could not record who claimed #$ID; released it again" >&2
            exit 1
        fi
        echo "claimed #$ID"
        ;;

    release)
        # Hand a claim back so the issue becomes selectable again.
        #
        # The label is written by machinery at pickup, so machinery is what has to
        # take it back. Nothing ever did: MaKlaude issue #195 was claimed at
        # 02:18, its session went quiet at 03:17 and was killed at 03:33 with a
        # clean tree, no branch and no commit, and the label sat there until a
        # human removed it by hand. `next --milestone 6` skipped #195 the whole
        # time; with the label gone it returned 195 immediately.
        #
        # Two forms, for the two callers. `--session T` releases everything T
        # claimed, which is what the control plane invokes when its continuation
        # ladder declines to resume a chain — releasing by session rather than by
        # age is the entire point, because age can't tell a dead session from a
        # slow one. `--id N` releases a single issue, for an agent or a human
        # handing work back deliberately.
        ID="" SESSION="" REASON="the claiming session ended without finishing"
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --id) ID="$2"; shift 2 ;;
                --session) SESSION="$2"; shift 2 ;;
                --reason) REASON="$2"; shift 2 ;;
                *) shift ;;
            esac
        done
        if [ -z "$ID" ] && [ -z "$SESSION" ]; then
            echo "Usage: issues.sh release (--id ID | --session SESSION) [--reason REASON]" >&2
            exit 1
        fi
        if [ -n "$ID" ] && [ -n "$SESSION" ]; then
            echo "issues.sh release: pass --id or --session, not both" >&2
            exit 1
        fi
        if [ -n "$ID" ]; then
            TARGETS="$ID"
        else
            TARGETS="$(claim_rows | awk -F'\t' -v s="$SESSION" '$2 == s { print $1 }')"
        fi
        rc=0
        for n in $TARGETS; do
            release_one "$n" "$REASON" || rc=1
        done
        exit $rc
        ;;

    sweep-claims)
        # The backstop, and only the backstop.
        #
        # `release --session` needs a control plane that reached a decision. One
        # that is SIGKILLed, or a GitHub Actions run the runner cancels, reaches
        # no decision at all and releases nothing. This covers exactly that case:
        # a claim whose session cannot be accounted for by anyone.
        #
        # Age is the last resort rather than the mechanism, and the window is why
        # (see DEFAULT_CLAIM_STALE_HOURS). Three claims are deliberately left
        # alone:
        #
        #   - Anything younger than the window. This is the expensive direction to
        #     get wrong, so the sweep is biased hard towards leaving claims alone.
        #   - The caller's own claim, however old, when the caller has an identity.
        #     A sweeper cannot ask another host whether a process is alive, but it
        #     always knows about itself, and a long-running session sweeping the
        #     board must not release the issue it is at that moment working on.
        #   - A claim with no marker, because there is nothing to date it by.
        #     Guessing at its age is the race this design refuses, and reporting
        #     it every sweep is the per-tick noise that gets a report skipped.
        #     `release --id N` is the hatch for those.
        HOURS="$DEFAULT_CLAIM_STALE_HOURS"
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --older-than) HOURS="$2"; shift 2 ;;
                *) shift ;;
            esac
        done
        CUTOFF="$(awk -v h="$HOURS" 'BEGIN { printf "%d", h * 3600 }')"
        if [ "$CUTOFF" -lt 3600 ]; then
            echo "issues.sh sweep-claims: --older-than $HOURS is under an hour, which is \
shorter than a session's own life; that races a live session and hands its issue to a \
second worker" >&2
            exit 1
        fi
        ME="$(claim_session)"
        ROWS="$(claim_rows)"
        rc=0
        while IFS=$'\t' read -r NUM SESS AGE; do
            [ -z "$NUM" ] && continue
            [ "$SESS" = "-" ] && continue
            [ -n "$ME" ] && [ "$SESS" = "$ME" ] && continue
            [ "$AGE" -lt "$CUTOFF" ] && continue
            HRS="$(awk -v a="$AGE" 'BEGIN { printf "%.1f", a / 3600 }')"
            release_one "$NUM" \
                "session \`$SESS\` claimed this ${HRS}h ago and can no longer be accounted for" \
                || rc=1
        done <<EOF
$ROWS
EOF
        exit $rc
        ;;

    next)
        # Deterministically choose this run's unit of work and claim it.
        #
        # Selection is a query, not a judgment: the newest unblocked, unclaimed,
        # open issue on the active milestone, oldest first so nothing starves. The
        # orchestrator calling this cannot forget to mark what it picked, because
        # picking and marking are the same call.
        #
        # Prints the issue number on stdout and nothing else, so callers can do
        # ISSUE=$(issues.sh next --milestone 6). Exits 3 with no output when there
        # is nothing to work, which is a distinct outcome from an error.
        MILESTONE=""
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --milestone) MILESTONE="$2"; shift 2 ;;
                *) shift ;;
            esac
        done
        if [ -z "$MILESTONE" ]; then
            echo "Usage: issues.sh next --milestone N" >&2
            exit 1
        fi
        CANDIDATE="$(gh issue list --state open --label "milestone:$MILESTONE" \
            --json number,createdAt,labels --limit 100 \
            --jq '[.[] | select(([.labels[].name] | index("blocked")) == null)
                       | select(([.labels[].name] | index("in-progress")) == null)
                       | select(([.labels[].name] | index("needs:human")) == null)]
                  | sort_by(.createdAt) | .[0].number // empty')"
        if [ -z "$CANDIDATE" ]; then
            exit 3
        fi
        # Claim it through the same verified path rather than a bare label call.
        # `bash "$0"` rather than `"$0"`: callers invoke this as
        # `bash .genesis/scripts/issues.sh`, so relying on the exec bit would make
        # `next` fail in exactly the environments where `claim` still works.
        bash "$0" claim --id "$CANDIDATE" >/dev/null || exit 1
        echo "$CANDIDATE"
        ;;

    comment)
        ID="" BODY=""
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --id) ID="$2"; shift 2 ;;
                --body) BODY="$2"; shift 2 ;;
                *) shift ;;
            esac
        done
        if [ -z "$ID" ] || [ -z "$BODY" ]; then
            echo "Usage: issues.sh comment --id ID --body BODY" >&2
            exit 1
        fi
        gh issue comment "$ID" --body "$BODY"
        ;;

    view)
        ID="${1:-}"
        if [ -z "$ID" ]; then
            echo "Usage: issues.sh view ID" >&2
            exit 1
        fi
        gh issue view "$ID"
        ;;

    *)
        cat >&2 <<'EOF'
Usage: issues.sh COMMAND [OPTIONS]

Commands:
  create    Create a new issue
  list      List issues with filtering
  unanswered-comments
            List issues/PRs whose newest comment is a person's and that the
            loop has not answered, stalest first. Closed threads are reported
            only when the loop closed them AFTER the comment (empty = nothing
            waiting on a reply)
  unselectable-work
            List open issues no run can select — no milestone:N label, or
            every milestone on them already signed off — stalest first
            (empty = every open issue is reachable)
  red-prs   List open pull requests the merge sweep will never take — a check
            concluded as anything other than a pass, no check has reported at
            all, or the branch conflicts (empty = nothing is stuck)
  blocked   List all blocked issues
  recent    List recently updated issues (default: last 24h)
  summary   Overview of project state
  close     Close an issue
  assign    Assign an issue
  label     Add/remove labels
  claim     Mark an issue in-progress, recording which session claimed it
  next      Pick and claim this run's unit of work (--milestone N)
  release   Hand a claim back (--id N | --session SESSION)
  sweep-claims  Release claims older than --older-than HOURS (default 2)
  comment   Comment on an issue
  view      View issue details

List filters:
  --status STATE       open|closed|all (default: open)
  --milestone N        Filter by milestone label
  --label LABEL        Filter by label
  --assignee USER      Filter by assignee
  --since "N hours ago"  Filter by update time
  --search QUERY       Full-text search
  --all                Show all states

Comment filters (unanswered-comments):
  --window-days N      How far back a trailing human comment still counts as
                       unanswered (default 7, or GENESIS_COMMENT_WINDOW_DAYS)
EOF
        exit 1
        ;;
esac
