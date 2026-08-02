#!/usr/bin/env bash
# Upload a Grafana dashboard JSON to a Grafana instance over the HTTP API.
#
# Idempotent: re-running updates the existing dashboard in place (matched on the
# JSON's own uid) rather than creating duplicates.
#
# Usage:
#   scripts/upload-dashboard.sh [path/to/dashboard.json]
#
# Requires (read from the environment, or from ~/.config/genesis/.env):
#   GRAFANA_URL     e.g. https://yourstack.grafana.net
#   GRAFANA_TOKEN   a Grafana *service account* token (glsa_...)
#
# The Loki push credential (GENESIS_LOKI_TOKEN, glc_...) does NOT work here.
# That is a Grafana Cloud access-policy token: it authenticates to Loki and to
# grafana.com, and the stack's own API rejects it with "Invalid API key". Minting
# a service account from the Cloud API needs a policy scope the logs token
# doesn't carry either (403), so the token has to be created once by hand:
#
#   Grafana -> Administration -> Users and access -> Service accounts
#   -> Add service account -> role Editor -> Add service account token
#
# Then put GRAFANA_URL and GRAFANA_TOKEN in ~/.config/genesis/.env and this
# script never needs the UI again.
set -uo pipefail

DASHBOARD="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/templates/dashboards/genesis-activity.json}"
ENV_FILE="${GENESIS_CONFIG_DIR:-$HOME/.config/genesis}/.env"

if [ -f "$ENV_FILE" ]; then
    while IFS='=' read -r key value; do
        case "$key" in
            GRAFANA_URL | GRAFANA_TOKEN) export "$key=$value" ;;
        esac
    done < "$ENV_FILE"
fi

: "${GRAFANA_URL:=}"
: "${GRAFANA_TOKEN:=}"

fail() { echo "ERROR: $*" >&2; exit 1; }

[ -f "$DASHBOARD" ] || fail "dashboard not found: $DASHBOARD"
[ -n "$GRAFANA_URL" ] || fail "GRAFANA_URL is not set (see the header of this script)"
[ -n "$GRAFANA_TOKEN" ] || fail "GRAFANA_TOKEN is not set (needs a glsa_ service account token)"
python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$DASHBOARD" \
    || fail "$DASHBOARD is not valid JSON"

GRAFANA_URL="${GRAFANA_URL%/}"
UID_VALUE="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('uid',''))" "$DASHBOARD")"

# Free-tier stacks sleep when idle and answer 503 "Loading" while waking up.
# Treating that as a failure would make the first run of the day always fail.
for attempt in 1 2 3 4 5 6; do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 \
        -H "Authorization: Bearer $GRAFANA_TOKEN" "$GRAFANA_URL/api/org")"
    case "$code" in
        200) break ;;
        503) echo "stack is waking up (attempt $attempt) ..."; sleep 20 ;;
        401 | 403) fail "auth rejected (HTTP $code). GRAFANA_TOKEN must be a service account token (glsa_...), not a Loki access-policy token (glc_...)" ;;
        *) fail "unexpected HTTP $code from $GRAFANA_URL/api/org" ;;
    esac
done
[ "$code" = "200" ] || fail "stack did not become ready"

# overwrite:true is what makes this idempotent — same uid updates in place.
PAYLOAD="$(python3 -c "
import json, sys
dash = json.load(open(sys.argv[1]))
dash.pop('id', None)
print(json.dumps({'dashboard': dash, 'overwrite': True,
                  'message': 'uploaded by genesis scripts/upload-dashboard.sh'}))
" "$DASHBOARD")"

RESPONSE="$(curl -s -w '\n%{http_code}' --max-time 60 -X POST \
    -H "Authorization: Bearer $GRAFANA_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" "$GRAFANA_URL/api/dashboards/db")"
BODY="$(printf '%s' "$RESPONSE" | sed '$d')"
STATUS="$(printf '%s' "$RESPONSE" | tail -n1)"

if [ "$STATUS" != "200" ]; then
    fail "upload failed (HTTP $STATUS): $BODY"
fi

URL_PATH="$(printf '%s' "$BODY" | python3 -c "import json,sys; print(json.load(sys.stdin).get('url',''))" 2>/dev/null)"
echo "Uploaded: ${GRAFANA_URL}${URL_PATH}"

# Read it back: a 200 on POST means accepted, not necessarily retrievable.
if [ -n "$UID_VALUE" ]; then
    verify="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 \
        -H "Authorization: Bearer $GRAFANA_TOKEN" "$GRAFANA_URL/api/dashboards/uid/$UID_VALUE")"
    [ "$verify" = "200" ] && echo "Verified: uid=$UID_VALUE is retrievable" \
        || echo "WARNING: uid=$UID_VALUE not retrievable (HTTP $verify)" >&2
fi
