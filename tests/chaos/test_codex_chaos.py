"""The control plane driving a fake `codex` as a real child process.

Codex has no turn budget of its own, so the plane stops the session itself, and
it reports usage as a running total per session, so each resume must be charged
only what it added. Both are claims about real process behavior (a SIGTERM to a
process group, a session log written by the child), which is why these run the
fake as a subprocess rather than replaying a canned stream.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from genesis import server
from genesis.engines import CodexEngine

FAKE = Path(__file__).parent / "fake_codex.py"
THREAD = "019a0000-0000-7000-8000-000000000000"


@pytest.fixture
def codex(repo, tmp_path, monkeypatch):
    """Install the fake codex on PATH with a private CODEX_HOME, and return a
    writer for its script plus a reader for the argv it was called with."""
    bindir = repo / "fakebin-codex"
    bindir.mkdir()
    shim = bindir / "codex"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    spec = repo / "codex-script.json"
    monkeypatch.setenv("FAKE_CODEX_SCRIPT", str(spec))

    class Fake:
        @staticmethod
        def write(sessions, judge="STOP"):
            spec.write_text(json.dumps({"sessions": sessions, "judge": judge}))

        @staticmethod
        def calls() -> list[list[str]]:
            path = Path(f"{spec}.argv")
            return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    return Fake


@pytest.fixture
def codex_plane(repo, monkeypatch):
    monkeypatch.setattr(server, "_gh_token", lambda: "fake-token")
    monkeypatch.setattr(server, "mint_installation_token", lambda *a, **k: None)
    monkeypatch.setattr(server, "loki_push", lambda *a, **k: True)
    return server.LocalControlPlane(
        repo="chaos/repo", poll_interval=1, session_timeout=60,
        agent=".claude/agents/orchestrator.md", engine=CodexEngine(),
    )


def test_the_plane_stops_a_codex_session_at_the_budget_and_resumes_it(codex, codex_plane, repo) -> None:
    """The scenario the engine exists for. The first session would run 60 tool
    calls; the plane stops it at 40, the judge is asked, and the resumed session
    finishes the work. Each is charged only the tokens it added."""
    codex.write(
        [
            {"tools": 60, "tokens_per_tool": 1000, "delay": 0.05},
            {"tools": 3, "tokens_per_tool": 1000, "commit": "done.txt"},
        ],
        judge="CONTINUE converging on the fix",
    )
    codex_plane.run_orchestrator(None)

    calls = codex.calls()
    sessions = [c for c in calls if "read-only" not in c]
    judges = [c for c in calls if "read-only" in c]
    assert len(sessions) == 2 and len(judges) == 1
    assert "resume" not in sessions[0]
    assert sessions[1][:3] == ["exec", "resume", THREAD], "must resume the stopped session, not start over"
    assert (repo / "done.txt").exists(), "the resumed session finished the work"

    # The stopped session ran 40 or 41 commands (the child may emit one more
    # before the signal lands), the resume added 3, the judge 510. A cumulative
    # count would have charged the resume 43,000 or more.
    assert 40_000 + 3_000 + 510 <= codex_plane.run_spent <= 41_000 + 3_000 + 510


def test_a_codex_session_that_finishes_is_not_resumed(codex, codex_plane) -> None:
    codex.write([{"tools": 5, "tokens_per_tool": 2000, "commit": "done.txt"}])
    codex_plane.run_orchestrator(None)
    assert len(codex.calls()) == 1
    assert codex_plane.run_spent == 10_000


def test_the_token_ceiling_stops_a_codex_chain(codex, codex_plane, monkeypatch) -> None:
    """The task ceiling is in tokens for Codex, read from its own env var."""
    monkeypatch.setenv("GENESIS_TASK_TOKEN_CEILING", "20000")
    codex.write([{"tools": 60, "tokens_per_tool": 1000, "delay": 0.05}], judge="CONTINUE")
    codex_plane.run_orchestrator(None)
    assert len(codex.calls()) == 1, "40,000 tokens spent is over the 20,000 ceiling, so no resume"


def test_a_crashed_codex_session_is_not_resumed(codex, codex_plane) -> None:
    """No terminal event means no outcome to act on, the same as a Claude
    session that dies without a result."""
    codex.write([{"tools": 2, "end": "crash"}])
    codex_plane.run_orchestrator(None)
    assert len(codex.calls()) == 1
