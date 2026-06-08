"""Unit tests for the adopter-local .env provisioning (ensure_local_env)."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from genesis import scaffold


@pytest.fixture(autouse=True)
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "genesis-config"
    monkeypatch.setenv("GENESIS_CONFIG_DIR", str(cfg))
    return cfg


def test_creates_env_with_placeholders_when_missing(config_dir: Path) -> None:
    env_path = scaffold.ensure_local_env()

    assert env_path == config_dir / ".env"
    assert env_path.exists()
    content = env_path.read_text()
    # Placeholders for all three vars, no real values.
    assert "ANTHROPIC_API_KEY=\n" in content
    assert "GENESIS_GITHUB_APP_ID=\n" in content
    assert "GENESIS_GITHUB_APP_SECRET=" in content


def test_env_and_dir_have_locked_down_permissions(config_dir: Path) -> None:
    env_path = scaffold.ensure_local_env()

    assert stat.S_IMODE(config_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_existing_env_is_left_untouched(config_dir: Path) -> None:
    config_dir.mkdir(parents=True)
    env_path = config_dir / ".env"
    env_path.write_text("ANTHROPIC_API_KEY=already-populated\n")

    returned = scaffold.ensure_local_env()

    assert returned == env_path
    # The human's populated file must survive - never overwritten.
    assert env_path.read_text() == "ANTHROPIC_API_KEY=already-populated\n"
