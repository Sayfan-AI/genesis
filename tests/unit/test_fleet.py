"""Hermetic unit tests for plane_state, supervised server hooks, and `genesis fleet`."""

from __future__ import annotations

import os
from pathlib import Path
import plistlib
import subprocess
from unittest.mock import patch

import pytest

from genesis import fleet, plane_state, server
from genesis.server import LocalControlPlane


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GENESIS_FLEET_CONFIG", raising=False)
    monkeypatch.delenv("GENESIS_LAUNCH_AGENTS_DIR", raising=False)
    monkeypatch.delenv("GENESIS_SUPERVISED", raising=False)
    return tmp_path


def test_plane_state_roundtrip_and_stopped_sentinel(tmp_path: Path) -> None:
    plane_state.write_plane_state(
        repo="Sayfan-AI/genesis",
        pid=os.getpid(),
        engine="claude",
        agent=".claude/agents/evolver.md",
        state="running_session",
        started_at=100.0,
        child_pid=os.getpid(),
        session_token="serve-abc",
        session_started_at=110.0,
        last_turn_at=115.0,
        turns=4,
        run_spent=12.5,
        run_budget=150.0,
        base_dir=tmp_path,
    )
    loaded = plane_state.read_plane_state(tmp_path)
    assert loaded is not None
    assert loaded["repo"] == "Sayfan-AI/genesis"
    assert loaded["state"] == "running_session"
    assert loaded["turns"] == 4
    assert loaded["run_spent"] == 12.5

    assert plane_state.read_stopped(tmp_path) is None
    plane_state.write_stopped(
        "run_budget", detail="$150.00 >= $150.00", run_spent=150.0, run_budget=150.0, base_dir=tmp_path
    )
    stopped = plane_state.read_stopped(tmp_path)
    assert stopped is not None
    assert stopped["reason"] == "run_budget"
    assert plane_state.clear_stopped(tmp_path) is True
    assert plane_state.read_stopped(tmp_path) is None


def test_nudge_is_consumed_once(tmp_path: Path) -> None:
    assert plane_state.consume_nudge(tmp_path) is None
    plane_state.write_nudge(tmp_path, "released issue #103")
    assert plane_state.consume_nudge(tmp_path) == "released issue #103"
    assert plane_state.consume_nudge(tmp_path) is None


def test_run_budget_writes_stopped_sentinel_while_clean_shutdown_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plane = LocalControlPlane(repo="sagrin/sweatless")
    assert plane.acquire_lock() is True
    plane.run_spent = 150.0
    monkeypatch.setattr(server, "loki_push", lambda *a, **k: False)
    monkeypatch.setattr(server, "enable_workflows", lambda **k: None)

    rc = plane.run_orchestrator(None)
    assert rc == 0
    assert plane.shutdown is True
    stopped = plane_state.read_stopped(tmp_path)
    assert stopped is not None
    assert stopped["reason"] == "run_budget"

    plane._shutdown(token_ok=True)
    final_state = plane_state.read_plane_state(tmp_path)
    assert final_state is not None
    assert final_state["state"] == "stopped"
    assert final_state["stopped_reason"] == "run_budget"

    # Now verify a normal SIGTERM shutdown does NOT write `.genesis/.stopped`.
    plane_state.clear_stopped(tmp_path)
    plane2 = LocalControlPlane(repo="sagrin/sweatless")
    assert plane2.acquire_lock() is True
    plane2.shutdown = True
    plane2._shutdown(token_ok=True)
    assert plane_state.read_stopped(tmp_path) is None


def test_supervised_mode_honors_stopped_sentinel_without_touching_workflows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plane_state.write_stopped("run_budget", base_dir=tmp_path)
    disabled_called = False

    def boom(**kwargs):
        nonlocal disabled_called
        disabled_called = True

    monkeypatch.setattr(server, "disable_workflows", boom)
    plane = LocalControlPlane(repo="sagrin/sweatless", supervised=True)
    assert plane.serve() == 0
    assert disabled_called is False
    # Sentinel stays intact so launchd keeps holding off.
    assert plane_state.read_stopped(tmp_path) is not None


def test_interactive_start_clears_previous_stopped_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plane_state.write_stopped("run_budget", base_dir=tmp_path)
    monkeypatch.setattr(server.shutil, "which", lambda _b: "/usr/bin/true")
    monkeypatch.setattr(server, "disable_workflows", lambda **k: None)
    monkeypatch.setattr(server, "enable_workflows", lambda **k: None)
    monkeypatch.setattr(server, "_gh_token", lambda: "tok")
    monkeypatch.setattr(LocalControlPlane, "_prime_high_water_if_needed", lambda self, t: None)
    monkeypatch.setattr(LocalControlPlane, "_prime_ci_failure_cursor_if_needed", lambda self: None)
    monkeypatch.setattr(LocalControlPlane, "sweep_stale_claims", lambda self, force=False: None)
    monkeypatch.setattr(LocalControlPlane, "_gated", lambda self, r, t: True)

    plane = LocalControlPlane(repo="sagrin/sweatless", supervised=False)

    def stop_on_sleep(_seconds: float) -> None:
        plane.shutdown = True

    monkeypatch.setattr(plane, "_interruptible_sleep", stop_on_sleep)
    assert plane.serve() == 0
    assert plane_state.read_stopped(tmp_path) is None


def test_poll_tick_consumes_external_nudge_and_queues_followup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server.shutil, "which", lambda _b: "/usr/bin/true")
    monkeypatch.setattr(server, "disable_workflows", lambda **k: None)
    monkeypatch.setattr(server, "enable_workflows", lambda **k: None)
    monkeypatch.setattr(server, "_gh_token", lambda: "tok")
    monkeypatch.setattr(LocalControlPlane, "_prime_high_water_if_needed", lambda self, t: None)
    monkeypatch.setattr(LocalControlPlane, "_prime_ci_failure_cursor_if_needed", lambda self: None)
    monkeypatch.setattr(LocalControlPlane, "sweep_stale_claims", lambda self, force=False: None)
    monkeypatch.setattr(LocalControlPlane, "_gated", lambda self, r, t: True)
    monkeypatch.setattr(LocalControlPlane, "poll_once", lambda self, t: [])
    monkeypatch.setattr(LocalControlPlane, "merge_ready_prs", lambda self: [])
    monkeypatch.setattr(LocalControlPlane, "run_due_triggers", lambda self, t: None)
    monkeypatch.setattr(LocalControlPlane, "_followup_waits_on_checks", lambda self: False)

    plane_state.write_nudge(tmp_path, "released issue #103")
    plane = LocalControlPlane(repo="sagrin/sweatless")
    launched_labels: list[str] = []

    def fake_run(event, prompt=None, label=None):
        launched_labels.append(label or "")
        plane.shutdown = True
        return 0

    monkeypatch.setattr(plane, "run_orchestrator", fake_run)
    monkeypatch.setattr(plane, "_interruptible_sleep", lambda _s: None)

    assert plane.serve() == 0
    assert any("released issue #103" in lbl for lbl in launched_labels)
    assert plane_state.consume_nudge(tmp_path) is None


def test_fleet_config_ignores_genesis_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GENESIS_CONFIG_DIR", str(tmp_path / "sagrin-org"))
    expected = Path.home() / ".config" / "genesis" / "fleet.toml"
    assert fleet.fleet_config_path() == expected


def test_load_fleet_config_and_render_plists(tmp_path: Path) -> None:
    cfg_file = tmp_path / "fleet.toml"
    repo_dir = tmp_path / "sweatless"
    repo_dir.mkdir()
    plane_dir = tmp_path / "genesis-plane"
    cfg_file.write_text(
        f"""
[runtime]
plane_dir = "{plane_dir}"
watch_interval = 120

[[repos]]
name = "sweatless"
path = "{repo_dir}"
poll_interval = 45
enabled = true

[[repos]]
name = "genesis-serve"
path = "{tmp_path / 'genesis-serve'}"
agent = ".claude/agents/evolver.md"
enabled = false
"""
    )
    cfg = fleet.load_fleet_config(cfg_file)
    assert cfg.plane_dir == plane_dir
    assert cfg.watch_interval == 120
    assert len(cfg.repos) == 2

    plist_bytes = fleet.build_serve_plist(cfg.repos[0], cfg.plane_dir, home=tmp_path)
    parsed = plistlib.loads(plist_bytes)
    assert parsed["Label"] == "ai.sayfan.genesis.serve.sweatless"
    assert parsed["WorkingDirectory"] == str(repo_dir)
    stopped_key = str(repo_dir / ".genesis" / ".stopped")
    assert parsed["KeepAlive"] == {"PathState": {stopped_key: False}}
    cmd_str = parsed["ProgramArguments"][2]
    assert "--supervised" in cmd_str
    assert "--poll-interval 45" in cmd_str
    assert "direnv exec ." in cmd_str
    assert "cmux-cli-shims" not in parsed["EnvironmentVariables"]["PATH"]

    watch_plist = plistlib.loads(fleet.build_watch_plist(cfg, home=tmp_path))
    assert watch_plist["Label"] == "ai.sayfan.genesis.watch"
    assert watch_plist["StartInterval"] == 120
    assert watch_plist["ProgramArguments"] == [
        str(plane_dir / ".venv" / "bin" / "genesis"),
        "watch",
        "--config",
        str(cfg_file),
    ]


def test_fleet_sync_loads_enabled_and_removes_stale_plists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents_dir = tmp_path / "LaunchAgents"
    agents_dir.mkdir()
    monkeypatch.setenv("GENESIS_LAUNCH_AGENTS_DIR", str(agents_dir))

    plane_dir = tmp_path / "plane"
    genesis_bin = plane_dir / ".venv" / "bin" / "genesis"
    genesis_bin.parent.mkdir(parents=True)
    genesis_bin.write_text("#!/bin/sh\n")

    repo_a = tmp_path / "sweatless"
    repo_a.mkdir()
    stale_plist = agents_dir / "ai.sayfan.genesis.serve.obsolete.plist"
    stale_plist.write_text("stale")

    cfg_file = tmp_path / "fleet.toml"
    cfg_file.write_text(
        f"""
[runtime]
plane_dir = "{plane_dir}"

[[repos]]
name = "sweatless"
path = "{repo_dir if False else repo_a}"
enabled = true
"""
    )
    cfg = fleet.load_fleet_config(cfg_file)

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        # `print` returns 1 (not yet loaded); `bootstrap`/`bootout` return 0.
        rc = 1 if len(cmd) > 1 and cmd[1] == "print" else 0
        return subprocess.CompletedProcess(cmd, rc, "", "")

    assert fleet.fleet_sync(cfg, run=fake_run) == 0
    assert (agents_dir / "ai.sayfan.genesis.serve.sweatless.plist").exists()
    assert (agents_dir / "ai.sayfan.genesis.watch.plist").exists()
    assert not stale_plist.exists()
    assert any("bootstrap" in c and str(agents_dir / "ai.sayfan.genesis.serve.sweatless.plist") in c for c in calls)


def test_fleet_stop_and_resume_manage_stopped_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents_dir = tmp_path / "LaunchAgents"
    agents_dir.mkdir()
    monkeypatch.setenv("GENESIS_LAUNCH_AGENTS_DIR", str(agents_dir))

    repo_a = tmp_path / "sweatless"
    repo_a.mkdir()
    cfg_file = tmp_path / "fleet.toml"
    cfg_file.write_text(
        f"""
[[repos]]
name = "sweatless"
path = "{repo_a}"
enabled = true
"""
    )
    cfg = fleet.load_fleet_config(cfg_file)
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    assert fleet.fleet_stop(cfg, "sweatless", run=fake_run) == 0
    stopped = plane_state.read_stopped(repo_a)
    assert stopped is not None
    assert stopped["reason"] == "manual_stop"

    assert fleet.fleet_resume(cfg, "sweatless", run=fake_run) == 0
    assert plane_state.read_stopped(repo_a) is None
    assert any("kickstart" in c for c in calls)
