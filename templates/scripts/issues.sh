#!/usr/bin/env bash
# Genesis issue manager — abstraction over gh CLI
# Supports: create, list, close, assign, comment, label, view
set -euo pipefail

CMD="${1:-help}"
shift || true

# JSON fields to fetch for list/view queries
FIELDS="number,title,state,url,labels,assignees,createdAt,updatedAt"

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
EOF
        exit 1
        ;;
esac
