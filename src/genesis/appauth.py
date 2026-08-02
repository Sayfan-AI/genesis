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
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

API = "https://api.github.com"

# Env var naming the operator's escape hatch. Anything other than "app" (the
# default) keeps the agent on whatever credential the shell already has.
IDENTITY_ENV = "GENESIS_AGENT_IDENTITY"


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
    except (urllib.error.URLError, ValueError, TimeoutError):
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
