"""Harness for chaos-testing the local control plane.

The dev system's interesting failures live in the Python control plane rather than
in the model: a session that dies mid-task, a chain that will not stop, a state
file that wedges the loop, a crash that strands a lock. None of those need a real
Claude session to reproduce, which is why this suite runs offline.

Two things are faked and nothing else. `claude` is replaced by a scripted
stand-in on PATH, so the real session plumbing runs (streaming, timeouts, process
groups, the continuation ladder) against a deterministic outcome. GitHub is
stubbed at the `gh`-and-events boundary, because the chaos under test is not
GitHub's behaviour.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from genesis import server

FAKE = Path(__file__).parent / "fake_claude.py"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A throwaway git repo laid out like a dev system, and cwd pointed at it."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".genesis").mkdir()
    (tmp_path / ".genesis" / "config.toml").write_text('name = "chaos"\n')
    (tmp_path / ".claude" / "agents").mkdir(parents=True)
    (tmp_path / ".claude" / "agents" / "orchestrator.md").write_text("# orchestrator\n")
    subprocess.run(["git", "init", "-q", "."], check=True)
    (tmp_path / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(
        ["git", "-c", "user.email=c@x", "-c", "user.name=c", "commit", "-qm", "seed"],
        check=True,
    )
    return tmp_path


@pytest.fixture
def script(repo, monkeypatch):
    """Install the fake claude on PATH and return a writer for its script."""
    bindir = repo / "fakebin"
    bindir.mkdir()
    shim = bindir / "claude"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

    spec_path = repo / "claude-script.json"
    monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", str(spec_path))

    def write(sessions, judge="STOP"):
        spec_path.write_text(json.dumps({"sessions": sessions, "judge": judge}))
        return spec_path

    return write


@pytest.fixture
def plane(repo, monkeypatch):
    """A control plane with GitHub stubbed out, ready to run sessions."""
    monkeypatch.setattr(server, "_gh_token", lambda: "fake-token")
    monkeypatch.setattr(server, "mint_installation_token", lambda *a, **k: None)
    monkeypatch.setattr(server, "loki_push", lambda *a, **k: True)
    return server.LocalControlPlane(
        repo="chaos/repo", poll_interval=1, session_timeout=60,
        agent=".claude/agents/orchestrator.md",
    )


@pytest.fixture
def sessions_run(repo):
    """How many times the fake claude was invoked, judge calls included."""
    def count():
        counter = repo / "claude-script.json.n"
        return int(counter.read_text()) if counter.exists() else 0
    return count
