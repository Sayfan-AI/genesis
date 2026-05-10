"""Unit tests for the genesis CLI argument parsing and dispatch."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from genesis import cli


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("GENESIS_REPO", "GENESIS_POLL_INTERVAL", "GENESIS_SESSION_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)
    yield


def test_serve_invokes_server_with_no_args() -> None:
    with patch("genesis.cli.serve", return_value=0) as serve:
        rc = cli.main(["serve"])
    assert rc == 0
    serve.assert_called_once_with()
    assert "GENESIS_REPO" not in os.environ
    assert "GENESIS_POLL_INTERVAL" not in os.environ
    assert "GENESIS_SESSION_TIMEOUT" not in os.environ


def test_serve_propagates_repo_to_env() -> None:
    with patch("genesis.cli.serve", return_value=0):
        cli.main(["serve", "--repo", "alice/test"])
    assert os.environ["GENESIS_REPO"] == "alice/test"


def test_serve_propagates_poll_interval_to_env() -> None:
    with patch("genesis.cli.serve", return_value=0):
        cli.main(["serve", "--poll-interval", "30"])
    assert os.environ["GENESIS_POLL_INTERVAL"] == "30"


def test_serve_propagates_session_timeout_to_env() -> None:
    with patch("genesis.cli.serve", return_value=0):
        cli.main(["serve", "--session-timeout", "1800"])
    assert os.environ["GENESIS_SESSION_TIMEOUT"] == "1800"


def test_serve_returns_underlying_return_code() -> None:
    with patch("genesis.cli.serve", return_value=42):
        assert cli.main(["serve"]) == 42


def test_workflows_enable_dispatches() -> None:
    with patch("genesis.cli.enable_workflows") as enable, patch(
        "genesis.cli.disable_workflows"
    ) as disable:
        rc = cli.main(["workflows", "enable"])
    assert rc == 0
    enable.assert_called_once_with(repo=None)
    disable.assert_not_called()


def test_workflows_disable_dispatches() -> None:
    with patch("genesis.cli.disable_workflows") as disable, patch(
        "genesis.cli.enable_workflows"
    ) as enable:
        rc = cli.main(["workflows", "disable"])
    assert rc == 0
    disable.assert_called_once_with(repo=None)
    enable.assert_not_called()


def test_workflows_enable_threads_repo_arg() -> None:
    with patch("genesis.cli.enable_workflows") as enable:
        cli.main(["workflows", "enable", "--repo", "alice/foo"])
    enable.assert_called_once_with(repo="alice/foo")


def test_workflows_disable_threads_repo_arg() -> None:
    with patch("genesis.cli.disable_workflows") as disable:
        cli.main(["workflows", "disable", "--repo", "alice/foo"])
    disable.assert_called_once_with(repo="alice/foo")


def test_no_command_exits_with_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])
    assert excinfo.value.code != 0


def test_workflows_with_no_subcommand_exits_with_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["workflows"])
    assert excinfo.value.code != 0


def test_unknown_command_exits_with_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["bogus"])
    assert excinfo.value.code != 0
