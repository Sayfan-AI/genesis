#!/usr/bin/env bash
# Genesis issue manager — abstraction over gh CLI
# Supports: create, list, unanswered-comments, close, assign, comment, label, view
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
        echo "claimed #$ID"
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
  blocked   List all blocked issues
  recent    List recently updated issues (default: last 24h)
  summary   Overview of project state
  close     Close an issue
  assign    Assign an issue
  label     Add/remove labels
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
