#!/usr/bin/env bash
# Activate this genesis dev system — the single command that wakes it up.
#
# Genesis ships a dev repo's workflows DISABLED and with no secrets. Run this once
# from a clone of the dev repo, after you've populated ~/.config/genesis/.env, and
# it does every remaining step in one go:
#   1. reads ANTHROPIC_API_KEY / GENESIS_GITHUB_APP_ID / GENESIS_GITHUB_APP_SECRET
#      from ~/.config/genesis/.env  (shared across all your genesis projects),
#   2. verifies the genesis GitHub App is actually installed on this repo,
#   3. sets the values as THIS repo's GitHub Actions secrets
#      (ANTHROPIC_API_KEY, GENESIS_APP_ID, GENESIS_APP_PRIVATE_KEY),
#   4. seeds the OPTIONAL Loki secrets when the .env has them
#      (GENESIS_LOKI_URL, GENESIS_LOKI_USER, GENESIS_LOKI_TOKEN) so the activity
#      logging hooks reach Grafana from Actions runs, not just local ones,
#   5. enables the workflows genesis disabled at publish.
#
# It refuses to run if any value is missing/placeholder or if the App isn't
# installed. You can also export the three vars yourself instead of using the .env.
#
# Usage: .genesis/scripts/activate.sh
set -euo pipefail

ENV_FILE="${GENESIS_CONFIG_DIR:-$HOME/.config/genesis}/.env"

# --- 1. validate the three values are present and not placeholders --------------
# Validate BEFORE sourcing into this shell, so placeholder values never leak into
# the environment (and on to gh / child processes): the candidate values are
# loaded in a throwaway subshell and checked there; the file is only sourced for
# real once all three are clean.
is_placeholder() {
    case "$1" in
        "" | *PLACEHOLDER* | *REPLACE_WITH* | *"paste the full PEM"*) return 0 ;;
        *) return 1 ;;
    esac
}
validate_values() {
    local v missing=()
    for v in ANTHROPIC_API_KEY GENESIS_GITHUB_APP_ID GENESIS_GITHUB_APP_SECRET; do
        is_placeholder "${!v:-}" && missing+=("$v")
    done
    if [ "${#missing[@]}" -gt 0 ]; then
        echo "ERROR: missing or placeholder value(s): ${missing[*]}" >&2
        return 1
    fi
}

if [ -f "$ENV_FILE" ]; then
    # Check in a subshell first - never source placeholders into the real env.
    if ! ( set -a; . "$ENV_FILE"; set +a; validate_values ); then
        echo "Populate $ENV_FILE (shared across all your projects), then re-run." >&2
        exit 1
    fi
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
else
    # No env file - fall back to vars the caller exported.
    if ! validate_values; then
        echo "No $ENV_FILE found and the vars aren't exported - create/populate it, then re-run." >&2
        exit 1
    fi
fi

REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)" \
    || { echo "ERROR: run this from inside a clone of the dev repo." >&2; exit 1; }

# --- 2. verify the genesis GitHub App is installed on this repo -----------------
# The /repos/{repo}/installation endpoint needs a JWT signed by the App key — a
# plain user token gets 401 — so we mint a short-lived App JWT from the App ID + PEM.
b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }
app_jwt() {
    command -v openssl >/dev/null 2>&1 || return 2
    local now iat exp header payload signing sig
    now="$(date +%s)"; iat=$((now - 60)); exp=$((now + 540))
    header='{"alg":"RS256","typ":"JWT"}'
    payload="{\"iat\":$iat,\"exp\":$exp,\"iss\":\"$GENESIS_GITHUB_APP_ID\"}"
    signing="$(printf '%s' "$header" | b64url).$(printf '%s' "$payload" | b64url)"
    sig="$(printf '%s' "$signing" \
        | openssl dgst -sha256 -sign <(printf '%s' "$GENESIS_GITHUB_APP_SECRET") \
        | b64url)" || return 2
    printf '%s.%s' "$signing" "$sig"
}
# What the seeded workflows actually need, and why each one, so a failure here
# reads as a checklist rather than a riddle. A workflow's `permission-*` input can
# only NARROW what the installation already grants - it cannot add anything - so
# every one of these has to be on the App itself.
#
# This check exists because the failure it replaces is the expensive kind: the
# agent authors the change, commits it, and the push is rejected *mid-run*, so a
# whole session's work and budget is spent discovering a setup problem. Measured
# on the-gigi/butterfly for `workflows` (genesis issue #20) and again for
# `actions` (genesis issue #14), where every `gh run list` from inside an agent
# returned 403 and the evolver silently lost one of its main signals.
#
# Fails rather than warns. An adopter who has just run activate.sh believes the
# repo is ready, and a warning scrolled past three lines ago does not survive
# that belief.
# Exported because the python below reads it from the environment rather than
# taking it as an argument - a multi-line argv entry is a quoting minefield.
export REQUIRED_APP_PERMISSIONS="
contents:write:the agent commits and pushes its own work
issues:write:the loop coordinates through issues
pull_requests:write:workers open pull requests and auto-merge lands them
workflows:write:the evolver edits .github/workflows/ files
actions:write:auto-merge re-dispatches the orchestrator with gh workflow run
"

check_app_permissions() {
    local body="$1" missing
    missing="$(printf '%s' "$body" | python3 -c '
import json, os, sys

try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(0)  # unreadable body: this check is a convenience, not a gate on itself

granted = payload.get("permissions") if isinstance(payload, dict) else None
# An ABSENT permissions object teaches us nothing, and reporting "all five
# missing" off it would block activation on a check that learned nothing. Only a
# populated object is evidence. An installation always carries at least
# `metadata`, so an empty one means the shape changed, not that the App has no
# grants.
if not isinstance(granted, dict) or not granted:
    sys.exit(0)

for line in os.environ["REQUIRED_APP_PERMISSIONS"].strip().splitlines():
    name, level, why = line.split(":", 2)
    have = granted.get(name)
    # write satisfies a read requirement; nothing satisfies a missing one.
    if have == "write" or (level == "read" and have == "read"):
        continue
    print("  %-15s need %-5s have %-7s - %s" % (name, level, have or "nothing", why))
' 2>/dev/null)" || return 0

    if [ -n "$missing" ]; then
        echo "ERROR: the genesis App is installed on $REPO but is missing permissions:" >&2
        echo "$missing" >&2
        echo "" >&2
        echo "Add them to the App (Settings -> Developer settings -> GitHub Apps ->" >&2
        echo "your genesis App -> Permissions), then ACCEPT the permission update on" >&2
        echo "the installation - a granted permission does nothing until the install" >&2
        echo "is updated. Then re-run this script." >&2
        echo "" >&2
        echo "A workflow's permission-* input can only narrow what the App grants, so" >&2
        echo "this cannot be fixed in YAML. Without it the failure surfaces mid-run:" >&2
        echo "the agent does the work, then the push or the API call is refused." >&2
        exit 1
    fi
    echo "App permissions look right."
}

verify_app_installed() {
    if ! command -v openssl >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
        echo "WARNING: openssl/curl not found; skipping App-install check." >&2
        return 0
    fi
    local jwt code body tmp
    jwt="$(app_jwt)" || { echo "WARNING: couldn't mint App JWT; skipping App-install check." >&2; return 0; }
    # Keep the body. It carries a `permissions` object, and throwing it away is
    # what made a missing grant a mid-run failure instead of a setup one - see
    # check_app_permissions below.
    tmp="$(mktemp)"
    code="$(curl -s -o "$tmp" -w '%{http_code}' \
        -H "Authorization: Bearer $jwt" \
        -H "Accept: application/vnd.github+json" \
        "https://api.github.com/repos/$REPO/installation")"
    body="$(cat "$tmp")"; rm -f "$tmp"
    case "$code" in
        200) echo "Genesis App is installed on $REPO."
             check_app_permissions "$body" ;;
        404) echo "ERROR: the genesis GitHub App is not installed on $REPO." >&2
             echo "Install it on the repo's org/account, then re-run." >&2
             exit 1 ;;
        401) echo "ERROR: App JWT rejected (HTTP 401) — check GENESIS_GITHUB_APP_ID and the PEM." >&2
             exit 1 ;;
        *)   echo "WARNING: couldn't confirm App install (HTTP $code); continuing." >&2 ;;
    esac
}
verify_app_installed

# --- 3. seed the repo's Actions secrets -----------------------------------------
echo "Seeding secrets onto $REPO ..."
# Pipe via stdin (printf is a builtin) so values never reach the process arg list.
printf '%s' "$ANTHROPIC_API_KEY"         | gh secret set ANTHROPIC_API_KEY
printf '%s' "$GENESIS_GITHUB_APP_ID"     | gh secret set GENESIS_APP_ID
printf '%s' "$GENESIS_GITHUB_APP_SECRET" | gh secret set GENESIS_APP_PRIVATE_KEY

# --- 4. seed the OPTIONAL Loki secrets ------------------------------------------
# Activity logging is opt-in: without these, log.sh skips the push and everything
# else works. There's no stderr consolation prize in Actions — Claude Code
# captures hook stderr into its own transcript, so unconfigured Loki means no
# activity trail in Actions runs. All three or none — a URL without credentials
# would just collect 401s, and log.sh's failure warning lands in that same
# invisible transcript.
loki_missing=()
for v in GENESIS_LOKI_URL GENESIS_LOKI_USER GENESIS_LOKI_TOKEN; do
    is_placeholder "${!v:-}" && loki_missing+=("$v")
done
if [ "${#loki_missing[@]}" -eq 0 ]; then
    printf '%s' "$GENESIS_LOKI_URL"   | gh secret set GENESIS_LOKI_URL
    printf '%s' "$GENESIS_LOKI_USER"  | gh secret set GENESIS_LOKI_USER
    printf '%s' "$GENESIS_LOKI_TOKEN" | gh secret set GENESIS_LOKI_TOKEN
    echo "Loki activity logging enabled (3 secrets seeded)."
elif [ "${#loki_missing[@]}" -eq 3 ]; then
    echo "Loki not configured — Actions runs will have no activity trail (hook stderr never reaches the run logs)."
else
    echo "WARNING: partial Loki config, skipping. Missing: ${loki_missing[*]}" >&2
    echo "         Set all three in $ENV_FILE and re-run to enable logging." >&2
fi

# --- 5. enable the workflows genesis disabled at publish ------------------------
echo "Enabling workflows ..."
gh workflow list --all --json id,name,state \
    | python3 -c "
import json, sys
for wf in json.load(sys.stdin):
    if wf['state'] == 'disabled_manually':
        print(wf['id'], wf['name'])
" \
    | while read -r id name; do
        gh workflow enable "$id" && echo "enabled: $name"
    done

echo "Dev system activated on $REPO. The next trigger will wake the orchestrator."
