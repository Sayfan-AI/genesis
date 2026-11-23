"""Mint short-lived GitHub App installation tokens for local agent sessions.

Why this exists: in GitHub Actions the agent authenticates as the Genesis App
(`genesis-dev-bot[bot]`) because `actions/create-github-app-token` mints a token
per run. Locally, `genesis serve` had no equivalent, so sessions inherited the
operator's own `gh` credential and the agent became indistinguishable from the
human — same login on its commits, its comments, its merges, and its approvals.

That collapse is not cosmetic. An approval gate's whole premise is that the
approver is not the actor, and a gate cannot tell them apart when both are one
account. It also makes "only merge PRs created by the bot" vacuous, and leaves an
operator unable to read their own repo history.

There is no stored token to reuse: Actions mints one on every run from the App ID
and private key. Both already live in `~/.config/genesis/.env`, so local mode can
perform the same exchange — sign a JWT with the PEM, resolve the installation,
trade it for an installation token that expires in an hour.

Tokens are minted per session rather than per process precisely because of that
hour: a `serve` that runs all afternoon would otherwise be holding a dead
credential after the first one.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"

# Everything a GitHub API call can die of besides an HTTP status, and all of it
# transport, which heals: a dropped keep-alive (`RemoteDisconnected`), a
# truncated body (`IncompleteRead`, or the JSON error it turns into), a read
# timeout. `urlopen` wraps only send-side failures in URLError; reading the
# response isn't wrapped, so none of these is one, and each used to reach the
# plane's top-level handler and stop it. Measured on genesis-dashboard: one
# "Remote end closed connection without response" at 10:09 ended a plane that
# had been idling correctly behind a human gate. It lives here, the lowest
# module that calls GitHub, so every caller shares one tuple: the fix reached
# the poll loop, then the startup prime (issue #119), then the token mint
# (issue #121), one caller at a time.
TRANSIENT_HTTP_ERRORS = (urllib.error.URLError, http.client.HTTPException, OSError, ValueError)

# Env var naming the operator's escape hatch. Anything other than "app" (the
# default) keeps the agent on whatever credential the shell already has.
IDENTITY_ENV = "GENESIS_AGENT_IDENTITY"


# The two values the token exchange needs, and the only two `serve` loads from
# the adopter-local file. `ANTHROPIC_API_KEY` lives there too and is left alone
# on purpose: local sessions run `claude` on the operator's own login, and
# exporting the key into the plane would silently switch every session's auth
# and billing to it.
APP_CREDENTIAL_KEYS = ("GENESIS_GITHUB_APP_ID", "GENESIS_GITHUB_APP_SECRET")

_ENV_LINE = re.compile(
    r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)="
    r"(?:\"((?:[^\"\\]|\\.)*)\"|'([^']*)'|([^\n]*))",
    re.M,
)


def local_env_path() -> Path:
    """`~/.config/genesis/.env`, or under GENESIS_CONFIG_DIR (mainly for tests)."""
    override = os.environ.get("GENESIS_CONFIG_DIR")
    base = Path(override) if override else Path.home() / ".config" / "genesis"
    return base / ".env"


def load_app_credentials(environ=None, path: Path | None = None) -> list[str]:
    """Fill the App ID and key from the adopter-local `.env` if the shell didn't.

    The module docstring's premise - "both already live in ~/.config/genesis/.env,
    so local mode can perform the same exchange" - was true of the file and false
    of the code: `mint_installation_token` reads the process environment only, so
    a `serve` started from a shell that hadn't sourced the file ran every session
    as the operator. The one sign was a log line. Measured on genesis-dashboard (genesis issue #98):
    its task-4 pull request opened under the operator's login, which the bot-only
    auto-merge then correctly refused to land.

    A value already in the environment wins, so an operator who exports a
    different App still gets it. Multi-line double-quoted values are read whole,
    because the private key is one. Returns the names it loaded.
    """
    environ = os.environ if environ is None else environ
    wanted = [k for k in APP_CREDENTIAL_KEYS if not (environ.get(k) or "").strip()]
    if not wanted:
        return []
    try:
        text = (path or local_env_path()).read_text()
    except OSError:
        return []
    loaded = []
    for m in _ENV_LINE.finditer(text):
        key = m.group(1)
        if key not in wanted or key in loaded:
            continue
        if m.group(2) is not None:
            value = m.group(2)
        elif m.group(3) is not None:
            value = m.group(3)
        else:
            value = m.group(4).split(" #", 1)[0].strip()
        if value:
            environ[key] = value
            loaded.append(key)
    return loaded


def _b64(raw: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def _app_jwt(app_id: str, pem: str) -> str | None:
    """Sign a short-lived RS256 JWT with the App's private key.

    Shelling out to openssl rather than taking a dependency on a JWT library:
    genesis ships as a handful of stdlib-only modules, and openssl is present
    anywhere `gh` and `git` are.
    """
    now = int(time.time())
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    # 9 minutes; GitHub rejects anything over 10, and clock skew eats the rest.
    payload = _b64(json.dumps({"iat": now - 60, "exp": now + 540, "iss": app_id}).encode())
    signing = header + b"." + payload

    key_path = None
    try:
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write(pem.strip() + "\n")
            key_path = handle.name
        os.chmod(key_path, 0o600)
        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", key_path],
            input=signing,
            capture_output=True,
            timeout=15,
            check=False,
        )
        if result.returncode != 0 or not result.stdout:
            return None
        return (signing + b"." + _b64(result.stdout)).decode()
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        if key_path:
            try:
                os.unlink(key_path)
            except OSError:
                pass


def _api(url: str, token: str, method: str = "GET") -> dict | None:
    request = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "genesis-local-control-plane",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except TRANSIENT_HTTP_ERRORS:
        return None


def mint_installation_token(repo: str, env: dict[str, str] | None = None) -> str | None:
    """Return a fresh installation token for `repo`, or None to keep the ambient one.

    None is returned for every failure and for the explicit opt-out. Falling back
    to the operator's credential is worse for attribution but keeps the dev system
    running, and a dev system that refuses to start because a token exchange
    hiccuped is a worse outcome than one whose commits are attributed to a human.
    """
    env = os.environ if env is None else env

    if env.get(IDENTITY_ENV, "app").strip().lower() != "app":
        return None

    app_id = (env.get("GENESIS_GITHUB_APP_ID") or "").strip()
    pem = env.get("GENESIS_GITHUB_APP_SECRET") or ""
    if not app_id or "PRIVATE KEY" not in pem:
        return None

    jwt = _app_jwt(app_id, pem)
    if not jwt:
        return None

    installation = _api(f"{API}/repos/{repo}/installation", jwt)
    if not installation or "id" not in installation:
        return None

    minted = _api(f"{API}/app/installations/{installation['id']}/access_tokens", jwt, "POST")
    if not minted or not minted.get("token"):
        return None
    return str(minted["token"])


# Per-machine runtime directory (`templates/gitignore` matches `.genesis/.*`).
# The file on disk is static shell; the token itself stays in the environment.
GH_SHIM_DIR = Path(".genesis/.gh-shim")
APP_TOKEN_ENV = "GENESIS_APP_TOKEN"
REAL_GH_ENV = "GENESIS_REAL_GH"

_GH_SHIM_SCRIPT = """#!/bin/sh
# Genesis App-identity shim for `gh`.
#
# `_session_env` sets `GH_TOKEN` to a minted installation token, which holds
# until an operator's personal agent instructions tell sessions to run
# `GH_TOKEN=$(gh auth token --user <login>) gh ...`. That reaches into the
# operator's keyring, replaces the App token for that command, and turns PRs,
# issues, and comments back into the operator's own login (genesis issue #192).
#
# Keeping the minted token in `GENESIS_APP_TOKEN` and placing this shim ahead of
# the real `gh` closes both paths without touching the keyring or writing a
# secret to disk: `gh auth token` prints the App token, `gh auth` mutations are
# refused, and every other `gh` call runs with `GH_TOKEN` / `GITHUB_TOKEN`
# restored to `GENESIS_APP_TOKEN` regardless of any per-command override.
set -eu

resolve_real_gh() {
    if [ -n "${GENESIS_REAL_GH:-}" ] && [ -x "${GENESIS_REAL_GH}" ]; then
        printf '%s\\n' "${GENESIS_REAL_GH}"
        return 0
    fi
    self_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
    old_ifs="$IFS"
    IFS=":"
    for entry in ${PATH:-}; do
        [ -n "$entry" ] || continue
        cand_dir="$(CDPATH= cd -- "$entry" 2>/dev/null && pwd -P)" || continue
        [ "$cand_dir" = "$self_dir" ] && continue
        if [ -x "$cand_dir/gh" ]; then
            IFS="$old_ifs"
            printf '%s\\n' "$cand_dir/gh"
            return 0
        fi
    done
    IFS="$old_ifs"
    return 1
}

if [ -n "${GENESIS_APP_TOKEN:-}" ]; then
    if [ "${1:-}" = "auth" ]; then
        case "${2:-}" in
            token)
                printf '%s\\n' "$GENESIS_APP_TOKEN"
                exit 0
                ;;
            login|logout|switch|refresh|setup-git)
                printf '%s\\n' "gh auth $2 is disabled inside an App-authenticated genesis session" >&2
                exit 2
                ;;
        esac
    fi
    export GH_TOKEN="$GENESIS_APP_TOKEN"
    export GITHUB_TOKEN="$GENESIS_APP_TOKEN"
fi

real_gh="$(resolve_real_gh)" || {
    printf '%s\\n' "genesis gh shim: real gh binary not found on PATH" >&2
    exit 127
}
exec "$real_gh" "$@"
"""


def _path_without_dir(raw_path: str, excluded_real: str) -> list[str]:
    kept: list[str] = []
    for entry in raw_path.split(os.pathsep):
        if not entry:
            continue
        try:
            if os.path.realpath(entry) == excluded_real:
                continue
        except OSError:
            pass
        kept.append(entry)
    return kept


def bind_app_token(
    env: dict[str, str], token: str, *, shim_dir: Path = GH_SHIM_DIR
) -> None:
    """Bind `token` into `env` and shield `gh` from per-command keyring overrides."""
    env["GH_TOKEN"] = token
    env["GITHUB_TOKEN"] = token
    env[APP_TOKEN_ENV] = token

    try:
        shim_dir.mkdir(parents=True, exist_ok=True)
        shim_real = os.path.realpath(shim_dir)
    except OSError:
        return

    clean_entries = _path_without_dir(env.get("PATH") or os.defpath, shim_real)
    real_gh = shutil.which("gh", path=os.pathsep.join(clean_entries))
    if real_gh:
        env[REAL_GH_ENV] = real_gh
    else:
        env.pop(REAL_GH_ENV, None)

    shim_path = shim_dir / "gh"
    try:
        needs_write = not shim_path.is_file() or shim_path.read_text() != _GH_SHIM_SCRIPT
        if needs_write:
            tmp_path = shim_dir / f".gh.tmp.{os.getpid()}"
            tmp_path.write_text(_GH_SHIM_SCRIPT)
            os.chmod(tmp_path, 0o755)
            os.replace(tmp_path, shim_path)
        elif not os.access(shim_path, os.X_OK):
            os.chmod(shim_path, 0o755)
    except OSError:
        return

    env["PATH"] = os.pathsep.join([shim_real, *clean_entries])


def clear_app_token_shim(
    env: dict[str, str], *, shim_dir: Path = GH_SHIM_DIR
) -> None:
    """Drop App-shim env vars when falling back to the operator's credential."""
    env.pop(APP_TOKEN_ENV, None)
    env.pop(REAL_GH_ENV, None)
    try:
        shim_real = os.path.realpath(shim_dir)
    except OSError:
        return
    if "PATH" in env:
        env["PATH"] = os.pathsep.join(_path_without_dir(env["PATH"], shim_real))

