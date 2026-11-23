"""Tests for minting GitHub App installation tokens in local mode.

The behaviour under test is an identity boundary: with an App token the agent
acts as `<app>[bot]`; without one it is indistinguishable from the operator, and
an approval gate whose premise is "approver is not actor" has nothing to check.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import urllib.request
from unittest.mock import patch

import pytest

from genesis import appauth


PEM = "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----"
GOOD_ENV = {"GENESIS_GITHUB_APP_ID": "12345", "GENESIS_GITHUB_APP_SECRET": PEM}


@pytest.fixture
def signed(monkeypatch):
    """Make the openssl signature deterministic without a real key."""
    monkeypatch.setattr(
        appauth.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, b"signature-bytes", b""),
    )


def fake_api(responses: dict[str, dict | None]):
    """Route by URL suffix so tests state intent rather than exact URLs."""

    def _api(url, token, method="GET"):
        for suffix, payload in responses.items():
            if url.endswith(suffix):
                return payload
        return None

    return _api


def test_mints_a_token_for_the_repos_installation(signed) -> None:
    api = fake_api({"/installation": {"id": 42}, "/access_tokens": {"token": "ghs_minted"}})
    with patch.object(appauth, "_api", api):
        assert appauth.mint_installation_token("Sayfan-AI/MaKlaude", GOOD_ENV) == "ghs_minted"


def test_opt_out_keeps_the_operators_credential(signed) -> None:
    env = {**GOOD_ENV, appauth.IDENTITY_ENV: "personal"}
    with patch.object(appauth, "_api", fake_api({"/access_tokens": {"token": "ghs_minted"}})):
        assert appauth.mint_installation_token("o/r", env) is None


def test_app_identity_is_the_default() -> None:
    """Isolation should be what you get without asking, since the failure mode of
    forgetting is a silent loss of attribution."""
    assert appauth.IDENTITY_ENV not in GOOD_ENV
    with patch.object(appauth, "_app_jwt", lambda *a: None):
        appauth.mint_installation_token("o/r", GOOD_ENV)  # reached the JWT step


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"GENESIS_GITHUB_APP_ID": "12345"},
        {"GENESIS_GITHUB_APP_SECRET": PEM},
        {"GENESIS_GITHUB_APP_ID": "12345", "GENESIS_GITHUB_APP_SECRET": "not-a-key"},
    ],
)
def test_missing_or_malformed_credentials_fall_back(env) -> None:
    assert appauth.mint_installation_token("o/r", env) is None


def test_signing_failure_falls_back(monkeypatch) -> None:
    monkeypatch.setattr(
        appauth.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, b"", b"openssl exploded"),
    )
    assert appauth.mint_installation_token("o/r", GOOD_ENV) is None


def test_api_failure_falls_back(signed) -> None:
    """A dev system that refuses to start because a token exchange hiccuped is
    worse than one whose commits are attributed to a human."""
    with patch.object(appauth, "_api", fake_api({"/installation": None})):
        assert appauth.mint_installation_token("o/r", GOOD_ENV) is None


# The real `_api` under each transport failure the poll loop already survives.
# `urlopen` raises these raw from reading the response, so a mint that let one
# escape stopped the plane from inside `_session_env()` (issue #121).
TRANSPORT_FAILURES = [
    http.client.RemoteDisconnected("Remote end closed connection without response"),
    http.client.IncompleteRead(b"{\"tok"),
    socket.timeout("The read operation timed out"),
    json.JSONDecodeError("Expecting value", "", 0),
]


@pytest.mark.parametrize("failure", TRANSPORT_FAILURES, ids=lambda e: type(e).__name__)
def test_transport_failure_during_the_exchange_falls_back(signed, failure) -> None:
    with patch.object(urllib.request, "urlopen", side_effect=failure):
        assert appauth.mint_installation_token("o/r", GOOD_ENV) is None


def test_no_token_in_the_exchange_response_falls_back(signed) -> None:
    api = fake_api({"/installation": {"id": 42}, "/access_tokens": {"message": "Bad credentials"}})
    with patch.object(appauth, "_api", api):
        assert appauth.mint_installation_token("o/r", GOOD_ENV) is None


def test_private_key_is_not_left_on_disk(monkeypatch) -> None:
    """The PEM is written to a temp file for openssl; it must not survive the call."""
    seen: list[str] = []
    def capture(cmd, **kwargs):
        seen.append(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, b"sig", b"")

    monkeypatch.setattr(appauth.subprocess, "run", capture)
    with patch.object(appauth, "_api", fake_api({"/installation": {"id": 1}, "/access_tokens": {"token": "t"}})):
        appauth.mint_installation_token("o/r", GOOD_ENV)

    assert seen, "openssl was never invoked"
    assert not os.path.exists(seen[0]), "temp key file outlived the call"


# ---------- loading the credentials serve needs (the file the docstring names) ----------

LOCAL_ENV = f"""# comment
ANTHROPIC_API_KEY=sk-should-never-load
GENESIS_GITHUB_APP_ID=12345
GENESIS_GITHUB_APP_SECRET="{PEM}"
GENESIS_LOKI_URL=https://logs.example
"""


def test_serve_loads_the_app_credentials_from_the_local_env(tmp_path) -> None:
    """A shell that never sourced the file used to run every session as the
    operator, with one log line to say so."""
    path = tmp_path / ".env"
    path.write_text(LOCAL_ENV)
    environ: dict[str, str] = {}
    loaded = appauth.load_app_credentials(environ, path)
    assert loaded == ["GENESIS_GITHUB_APP_ID", "GENESIS_GITHUB_APP_SECRET"]
    assert environ["GENESIS_GITHUB_APP_ID"] == "12345"
    assert environ["GENESIS_GITHUB_APP_SECRET"] == PEM, "the multi-line PEM must be read whole"


def test_only_the_app_credentials_are_loaded(tmp_path) -> None:
    """Exporting ANTHROPIC_API_KEY into the plane would switch every local
    session's Claude auth and billing to it."""
    path = tmp_path / ".env"
    path.write_text(LOCAL_ENV)
    environ: dict[str, str] = {}
    appauth.load_app_credentials(environ, path)
    assert set(environ) == set(appauth.APP_CREDENTIAL_KEYS)


def test_an_exported_value_wins_over_the_file(tmp_path) -> None:
    path = tmp_path / ".env"
    path.write_text(LOCAL_ENV)
    environ = {"GENESIS_GITHUB_APP_ID": "999"}
    assert appauth.load_app_credentials(environ, path) == ["GENESIS_GITHUB_APP_SECRET"]
    assert environ["GENESIS_GITHUB_APP_ID"] == "999"


def test_a_missing_file_or_placeholder_loads_nothing(tmp_path) -> None:
    environ: dict[str, str] = {}
    assert appauth.load_app_credentials(environ, tmp_path / "absent") == []
    path = tmp_path / ".env"
    path.write_text("GENESIS_GITHUB_APP_ID=\nGENESIS_GITHUB_APP_SECRET=\n")
    assert appauth.load_app_credentials(environ, path) == []
    assert environ == {}


def test_the_seeded_placeholder_file_parses(tmp_path) -> None:
    """The file `ensure_local_env` writes must be something this reads, quotes
    and all, once the adopter pastes real values in."""
    from genesis.scaffold import LOCAL_ENV_TEMPLATE

    filled = LOCAL_ENV_TEMPLATE.replace("GENESIS_GITHUB_APP_ID=", "GENESIS_GITHUB_APP_ID=42")
    path = tmp_path / ".env"
    path.write_text(filled)
    environ: dict[str, str] = {}
    appauth.load_app_credentials(environ, path)
    assert environ["GENESIS_GITHUB_APP_ID"] == "42"
    assert environ["GENESIS_GITHUB_APP_SECRET"].startswith("-----BEGIN RSA PRIVATE KEY-----")
    assert environ["GENESIS_GITHUB_APP_SECRET"].endswith("-----END RSA PRIVATE KEY-----")


# ---------- shielding `gh` from per-command keyring overrides (issue #192) ----------


def _fake_real_gh(bin_dir) -> tuple:
    """Install a stand-in `gh` that logs every invocation and its token env."""
    log_file = bin_dir / "gh.calls.jsonl"
    real_gh = bin_dir / "gh"
    real_gh.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"with open({str(log_file)!r}, 'a') as f:\n"
        "    f.write(json.dumps({\n"
        "        'argv': sys.argv[1:],\n"
        "        'GH_TOKEN': os.environ.get('GH_TOKEN'),\n"
        "        'GITHUB_TOKEN': os.environ.get('GITHUB_TOKEN'),\n"
        "    }) + '\\n')\n"
        "if sys.argv[1:3] == ['auth', 'token']:\n"
        "    print('gho_operator_keyring_secret')\n"
        "else:\n"
        "    print('ok')\n"
    )
    real_gh.chmod(0o755)
    return real_gh, log_file


def test_shim_preserves_app_identity_across_gh_auth_token_override(tmp_path) -> None:
    """Measured failure (genesis issue #192): personal agent instructions told
    child sessions to run `GH_TOKEN=$(gh auth token --user <login>) gh ...`,
    pulling the operator's keyring token and opening PRs/comments as the human."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _, log_file = _fake_real_gh(bin_dir)
    shim_dir = tmp_path / ".genesis" / ".gh-shim"

    env = {"PATH": f"{bin_dir}:{os.defpath}"}
    appauth.bind_app_token(env, "ghs_app_minted", shim_dir=shim_dir)

    assert "ghs_app_minted" not in (shim_dir / "gh").read_text(), (
        "the token belongs in the environment, never on disk"
    )

    proc = subprocess.run(
        ["/bin/sh", "-c", 'GH_TOKEN="$(gh auth token --user operator)" gh pr create --fill'],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == "ok"
    calls = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert calls == [
        {
            "argv": ["pr", "create", "--fill"],
            "GH_TOKEN": "ghs_app_minted",
            "GITHUB_TOKEN": "ghs_app_minted",
        }
    ], "real `gh auth token` must never be reached, and `gh pr create` must run as the App"


def test_shim_restores_app_token_even_under_direct_inline_override(tmp_path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _, log_file = _fake_real_gh(bin_dir)
    shim_dir = tmp_path / ".genesis" / ".gh-shim"

    env = {"PATH": f"{bin_dir}:{os.defpath}"}
    appauth.bind_app_token(env, "ghs_app_minted", shim_dir=shim_dir)

    subprocess.run(
        ["/bin/sh", "-c", "GH_TOKEN=gho_manual GITHUB_TOKEN=gho_manual gh issue comment 1 --body hi"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    calls = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert calls[0]["GH_TOKEN"] == "ghs_app_minted"
    assert calls[0]["GITHUB_TOKEN"] == "ghs_app_minted"


@pytest.mark.parametrize("subcmd", ["login", "logout", "switch", "refresh", "setup-git"])
def test_shim_refuses_gh_auth_mutations_inside_app_sessions(tmp_path, subcmd) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _, log_file = _fake_real_gh(bin_dir)
    shim_dir = tmp_path / ".genesis" / ".gh-shim"

    env = {"PATH": f"{bin_dir}:{os.defpath}"}
    appauth.bind_app_token(env, "ghs_app_minted", shim_dir=shim_dir)

    proc = subprocess.run(
        ["/bin/sh", "-c", f"gh auth {subcmd}"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "disabled inside an App-authenticated genesis session" in proc.stderr
    assert not log_file.exists()


def test_shim_falls_back_to_path_walk_and_never_recurses_into_itself(tmp_path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _, log_file = _fake_real_gh(bin_dir)
    shim_dir = tmp_path / ".genesis" / ".gh-shim"

    env = {"PATH": f"{bin_dir}:{os.defpath}"}
    appauth.bind_app_token(env, "ghs_first", shim_dir=shim_dir)
    appauth.bind_app_token(env, "ghs_second", shim_dir=shim_dir)
    assert env["PATH"].split(os.pathsep).count(os.path.realpath(shim_dir)) == 1

    # Drop the pre-resolved hint so the shell PATH walk has to skip self_dir.
    env.pop(appauth.REAL_GH_ENV, None)
    subprocess.run(
        ["/bin/sh", "-c", "gh issue list"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    calls = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert calls[0]["GH_TOKEN"] == "ghs_second"


def test_clear_app_token_shim_restores_personal_credential_mode(tmp_path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_real_gh(bin_dir)
    shim_dir = tmp_path / ".genesis" / ".gh-shim"

    env = {"PATH": f"{bin_dir}:{os.defpath}"}
    appauth.bind_app_token(env, "ghs_app_minted", shim_dir=shim_dir)
    appauth.clear_app_token_shim(env, shim_dir=shim_dir)

    assert appauth.APP_TOKEN_ENV not in env
    assert appauth.REAL_GH_ENV not in env
    assert os.path.realpath(shim_dir) not in env["PATH"].split(os.pathsep)

