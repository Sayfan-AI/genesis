"""Tests for minting GitHub App installation tokens in local mode.

The behaviour under test is an identity boundary: with an App token the agent
acts as `<app>[bot]`; without one it is indistinguishable from the operator, and
an approval gate whose premise is "approver is not actor" has nothing to check.
"""

from __future__ import annotations

import os
import subprocess
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
