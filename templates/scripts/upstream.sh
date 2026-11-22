#!/usr/bin/env bash
# Escalate a framework finding to genesis, and check that it arrived as one.
#
#   bash .genesis/scripts/upstream.sh --title "..." --body-file FILE [--local N]
#
# Files the issue on the genesis repo, asks for `needs:evolver`, then reads the
# issue's labels back. It doesn't trust the create call, because the create call
# is the half that works. A project's GitHub App is normally installed on that
# project only. Genesis is public, so the App can open an issue there, but it
# can't label one: the label step answers `Resource not accessible by
# integration`, and an agent that files with `gh issue create --label` and moves
# on never sees it. sagrin/sweatless filed seven escalations that way (genesis
# issue #155). All seven sat outside the `needs:evolver` queue until a person
# happened to label them, and the project had closed each local finding as
# "escalated".
#
# So the label is best-effort, and two things don't depend on it:
#
# 1. Every escalation ends with the FOOTER line below. Genesis triages open
#    unlabelled issues that carry it (see "Unlabelled Escalations" in genesis's
#    CLAUDE.md), so a finding whose label didn't stick still reaches its evolver.
# 2. When the label is missing, the script says so on stderr and, with --local,
#    on the local issue. A person reading the board then sees "filed, unlabelled"
#    rather than "escalated".
#
# A missing label is reported but still exits 0, and that's deliberate: the
# issue exists upstream, and an agent that reads a non-zero exit as "filing
# failed" retries and files a twin. Exit 1 means nothing was filed.
#
# Env:
#   GENESIS_UPSTREAM_REPO  owner/repo to file on (default Sayfan-AI/genesis)
#   GH_REPO                this project's owner/repo (default: gh repo view)
set -euo pipefail

UPSTREAM="${GENESIS_UPSTREAM_REPO:-Sayfan-AI/genesis}"
LABEL="needs:evolver"
# Genesis searches for this exact phrase, and genesis's own tests pin it to the
# search it documents, so the two can't drift apart.
FOOTER="Escalated with .genesis/scripts/upstream.sh"

title=""
body_file=""
local_issue=""
while [ $# -gt 0 ]; do
    case "$1" in
        --title) title="${2-}"; shift 2 ;;
        --body-file) body_file="${2-}"; shift 2 ;;
        --local) local_issue="${2-}"; shift 2 ;;
        *) echo "upstream.sh: unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [ -z "$title" ] || [ -z "$body_file" ] || [ ! -r "$body_file" ]; then
    echo "usage: upstream.sh --title TITLE --body-file FILE [--local N]" >&2
    exit 2
fi

project="${GH_REPO:-}"
if [ -z "$project" ]; then
    project="$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || true)"
fi
[ -n "$project" ] || project="(unknown project)"

source_ref="$project"
[ -n "$local_issue" ] && source_ref="$project issue #$local_issue"

body="$(cat "$body_file")

---
$FOOTER from $source_ref."

url="$(printf '%s' "$body" | gh issue create -R "$UPSTREAM" --title "$title" --body-file -)" || {
    echo "upstream.sh: could not open an issue on $UPSTREAM; nothing was filed" >&2
    exit 1
}
url="$(printf '%s' "$url" | tail -n 1)"

label_err="$(gh issue edit "$url" -R "$UPSTREAM" --add-label "$LABEL" 2>&1 >/dev/null)" || true

# Read the labels back rather than trusting the edit's exit code. If the read
# itself fails, the label is unverified, which is reported the same way as
# missing.
labels="$(gh issue view "$url" -R "$UPSTREAM" --json labels --jq '.labels[].name' 2>/dev/null || true)"

if printf '%s\n' "$labels" | grep -qx "$LABEL"; then
    if [ -n "$local_issue" ]; then
        gh issue comment "$local_issue" \
            --body "Escalated to genesis: $url (labelled \`$LABEL\`)." >/dev/null || true
    fi
    echo "$url"
    exit 0
fi

# Not `${label_err:-the label isn't ...}`: bash 3.2 reads that apostrophe as an
# opening quote and the script stops parsing.
reason="$label_err"
[ -n "$reason" ] || reason="the label is not on the issue"
reason="$(printf '%s' "$reason" | tr '\n' ' ' | sed 's/ *$//')"
echo "upstream.sh: filed $url, but \`$LABEL\` didn't stick: $reason" >&2
echo "upstream.sh: genesis triages unlabelled escalations that carry the footer, so don't file it again" >&2

if [ -n "$local_issue" ]; then
    gh issue comment "$local_issue" --body "Escalated to genesis as $url, but it's **unlabelled**: adding \`$LABEL\` failed ($reason).

This project's App can open issues on $UPSTREAM but can't label them there. Genesis also triages unlabelled escalations that carry the \`$FOOTER\` footer, so the finding is still in reach. To put it in the queue now, someone with write access on $UPSTREAM can add the label." >/dev/null || true
fi

echo "$url"
exit 0
