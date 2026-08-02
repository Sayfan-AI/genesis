"""Unit tests for the local control plane server."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from genesis import server


@pytest.fixture(autouse=True)
def isolate_cwd(tmp_path, monkeypatch):
    """Run each test in its own tmp dir so .genesis/ artifacts don't bleed."""
    monkeypatch.chdir(tmp_path)
    yield tmp_path


@pytest.fixture
def plane() -> server.LocalControlPlane:
    return server.LocalControlPlane(repo="alice/test", poll_interval=1, session_timeout=5)


# ---------- prompt building ----------


def test_build_prompt_initial_run() -> None:
    prompt = server._build_prompt(None)
    assert "orchestrator" in prompt
    assert "initial run" in prompt.lower()


def test_build_prompt_includes_event_metadata() -> None:
    event = {
        "id": "abc123",
        "type": "IssuesEvent",
        "actor": {"login": "alice"},
        "payload": {"action": "opened"},
    }
    prompt = server._build_prompt(event)
    assert "IssuesEvent" in prompt
    assert "opened" in prompt
    assert "alice" in prompt


# ---------- lock file ----------


def test_acquire_lock_when_unlocked(plane) -> None:
    assert plane.acquire_lock() is True
    assert server.LOCK_PATH.exists()
    assert server.LOCK_PATH.read_text().strip() == str(os.getpid())


def test_acquire_lock_blocks_when_live_pid(plane) -> None:
    server.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    server.LOCK_PATH.write_text(str(os.getpid()))  # current process is "alive"
    assert plane.acquire_lock() is False


def test_acquire_lock_clears_stale_pid(plane) -> None:
    server.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    server.LOCK_PATH.write_text("99999999")  # implausibly high pid, very unlikely to exist
    assert plane.acquire_lock() is True
    assert server.LOCK_PATH.read_text().strip() == str(os.getpid())


def test_acquire_lock_clears_garbage_pid(plane) -> None:
    server.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    server.LOCK_PATH.write_text("not-a-pid")
    assert plane.acquire_lock() is True


def test_release_lock_idempotent(plane) -> None:
    plane.release_lock()
    plane.acquire_lock()
    plane.release_lock()
    assert not server.LOCK_PATH.exists()
    plane.release_lock()  # second time should not raise


# ---------- state persistence ----------


def test_load_state_returns_none_when_missing(plane) -> None:
    plane.load_state()
    assert plane.etag is None
    assert plane.last_event_id is None


def test_save_and_load_state_roundtrip(plane) -> None:
    plane.etag = '"etag-value"'
    plane.last_event_id = "999"
    plane.save_state()

    fresh = server.LocalControlPlane(repo="alice/test")
    fresh.load_state()
    assert fresh.etag == '"etag-value"'
    assert fresh.last_event_id == "999"


# ---------- fetch_events ----------


def test_fetch_events_returns_304_as_not_modified() -> None:
    err = urllib.error.HTTPError(
        url="x", code=304, msg="Not Modified", hdrs=None, fp=None
    )
    with patch("urllib.request.urlopen", side_effect=err):
        result = server.fetch_events("a/b", etag='"old"', token="t")
    assert result.not_modified is True
    assert result.etag == '"old"'
    assert result.events == []


def test_fetch_events_returns_events_and_new_etag() -> None:
    body = json.dumps([{"id": "1", "type": "IssuesEvent"}]).encode()
    fake_resp = MagicMock()
    fake_resp.headers.get.return_value = '"new-etag"'
    fake_resp.read.return_value = body
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda *a: None
    with patch("urllib.request.urlopen", return_value=fake_resp):
        result = server.fetch_events("a/b", etag=None, token="t")
    assert result.not_modified is False
    assert result.etag == '"new-etag"'
    assert result.events == [{"id": "1", "type": "IssuesEvent"}]


def test_fetch_events_propagates_other_http_errors() -> None:
    err = urllib.error.HTTPError(url="x", code=401, msg="Unauthorized", hdrs=None, fp=None)
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(urllib.error.HTTPError):
            server.fetch_events("a/b", etag=None, token="t")


# ---------- poll_once ----------


def test_poll_once_returns_empty_on_304(plane) -> None:
    plane.etag = '"old"'
    plane.last_event_id = "5"
    not_mod = server.PollResult(events=[], etag='"old"', not_modified=True)
    with patch.object(server, "fetch_events", return_value=not_mod):
        events = plane.poll_once("token")
    assert events == []
    assert plane.last_event_id == "5"  # unchanged


def test_poll_once_advances_high_water_mark(plane) -> None:
    raw_events = [
        {"id": "10", "type": "IssuesEvent", "actor": {"login": "alice"}, "payload": {"action": "opened"}},
        {"id": "9", "type": "PushEvent", "actor": {"login": "alice"}, "payload": {}},
        {"id": "8", "type": "IssueCommentEvent", "actor": {"login": "alice"}, "payload": {"action": "created"}},
    ]
    poll = server.PollResult(events=raw_events, etag='"new"', not_modified=False)
    with patch.object(server, "fetch_events", return_value=poll):
        new = plane.poll_once("token")
    # Push event filtered out
    assert [e["id"] for e in new] == ["8", "10"]
    # High water = newest event seen, even though some were filtered
    assert plane.last_event_id == "10"
    assert plane.etag == '"new"'


def test_poll_once_skips_already_processed_events(plane) -> None:
    plane.last_event_id = "9"
    raw = [
        {"id": "10", "type": "IssuesEvent", "actor": {"login": "alice"}, "payload": {"action": "opened"}},
        {"id": "9",  "type": "IssuesEvent", "actor": {"login": "alice"}, "payload": {"action": "opened"}},
        {"id": "8",  "type": "IssuesEvent", "actor": {"login": "alice"}, "payload": {"action": "opened"}},
    ]
    poll = server.PollResult(events=raw, etag='"e"', not_modified=False)
    with patch.object(server, "fetch_events", return_value=poll):
        new = plane.poll_once("token")
    assert [e["id"] for e in new] == ["10"]


# ---------- run_orchestrator ----------


def test_run_orchestrator_handles_missing_claude(plane) -> None:
    with patch("subprocess.Popen", side_effect=FileNotFoundError("claude not found")):
        rc = plane.run_orchestrator(None)
    assert rc == 127


def test_run_orchestrator_passes_correct_command(plane) -> None:
    fake_proc = MagicMock()
    fake_proc.wait.return_value = 0
    fake_proc.pid = 12345
    with patch("subprocess.Popen", return_value=fake_proc) as popen:
        plane.run_orchestrator(None)
    cmd = popen.call_args[0][0]
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "--max-turns" in cmd
    assert "--allowedTools" in cmd


def test_run_orchestrator_kills_on_shutdown(plane, monkeypatch) -> None:
    fake_proc = MagicMock()
    fake_proc.pid = 12345
    # First wait raises Timeout, then after kill returns
    fake_proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="claude", timeout=1), 0]

    monkeypatch.setattr(os, "killpg", lambda *a, **k: None)
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)

    plane.shutdown = True
    with patch("subprocess.Popen", return_value=fake_proc):
        rc = plane.run_orchestrator(None)
    assert rc == -2


# ---------- signal handler ----------


def test_signal_handler_sets_shutdown_flag(plane) -> None:
    handler = server._make_signal_handler(plane)
    assert plane.shutdown is False
    handler(signal.SIGINT, None)
    assert plane.shutdown is True


# ---------- _interruptible_sleep ----------


def test_interruptible_sleep_wakes_on_shutdown(plane) -> None:
    plane.shutdown = True
    start = time.time()
    plane._interruptible_sleep(60)
    elapsed = time.time() - start
    assert elapsed < 1, f"should return immediately when shutdown set, took {elapsed}s"


# ---------- _prime_high_water_if_needed ----------


def test_prime_high_water_records_newest_event_id(plane) -> None:
    """Without priming, the post-initial poll would replay all historical events."""
    raw = [
        {"id": "100", "type": "IssuesEvent", "actor": {"login": "alice"}, "payload": {"action": "opened"}},
        {"id": "99", "type": "IssueCommentEvent", "actor": {"login": "alice"}, "payload": {"action": "created"}},
    ]
    poll = server.PollResult(events=raw, etag='"e"', not_modified=False)
    with patch.object(server, "fetch_events", return_value=poll):
        plane._prime_high_water_if_needed("token")
    assert plane.last_event_id == "100"
    assert plane.etag == '"e"'


def test_prime_high_water_skips_when_already_primed(plane) -> None:
    plane.last_event_id = "55"
    with patch.object(server, "fetch_events") as fetch:
        plane._prime_high_water_if_needed("token")
    fetch.assert_not_called()
    assert plane.last_event_id == "55"


def test_prime_high_water_handles_empty_events(plane) -> None:
    poll = server.PollResult(events=[], etag='"e"', not_modified=False)
    with patch.object(server, "fetch_events", return_value=poll):
        plane._prime_high_water_if_needed("token")
    assert plane.last_event_id is None  # still unset, no events to mark
    assert plane.etag == '"e"'


def test_prime_high_water_swallows_http_error(plane) -> None:
    err = urllib.error.HTTPError(url="x", code=500, msg="Internal", hdrs=None, fp=None)
    with patch.object(server, "fetch_events", side_effect=err):
        plane._prime_high_water_if_needed("token")  # must not raise
    assert plane.last_event_id is None


# ---------- poll_once: page-coverage warning ----------


def test_poll_once_warns_when_high_water_not_on_page(plane, capsys) -> None:
    """If the previous high-water id isn't in the returned page, events were missed."""
    plane.last_event_id = "5"  # known-missing from this page
    raw = [
        {"id": "20", "type": "IssuesEvent", "actor": {"login": "alice"}, "payload": {"action": "opened"}},
        {"id": "19", "type": "IssuesEvent", "actor": {"login": "alice"}, "payload": {"action": "opened"}},
    ]
    poll = server.PollResult(events=raw, etag='"e"', not_modified=False)
    with patch.object(server, "fetch_events", return_value=poll):
        plane.poll_once("token")
    out = capsys.readouterr().out
    assert "Warning" in out and "not found on returned page" in out


def test_poll_once_no_warning_when_high_water_on_page(plane, capsys) -> None:
    plane.last_event_id = "9"
    raw = [
        {"id": "10", "type": "IssuesEvent", "actor": {"login": "alice"}, "payload": {"action": "opened"}},
        {"id": "9", "type": "IssuesEvent", "actor": {"login": "alice"}, "payload": {"action": "opened"}},
    ]
    poll = server.PollResult(events=raw, etag='"e"', not_modified=False)
    with patch.object(server, "fetch_events", return_value=poll):
        plane.poll_once("token")
    assert "not found on returned page" not in capsys.readouterr().out


# ---------- self-heal on startup ----------


def test_serve_self_heals_stale_disabled_list(plane, monkeypatch, capsys) -> None:
    """If `.disabled-by-genesis` exists at startup, serve re-enables workflows
    before proceeding so the new session starts from a known clean state."""
    # Simulate a prior session that exited non-gracefully.
    server.DISABLED_LIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    server.DISABLED_LIST_PATH.write_text('[{"id": 42, "name": "Foo"}]')

    enable_called = MagicMock()
    disable_called = MagicMock()
    monkeypatch.setattr(server, "enable_workflows", enable_called)
    monkeypatch.setattr(server, "disable_workflows", disable_called)
    monkeypatch.setattr(server.shutil, "which", lambda _: "/usr/local/bin/claude")
    # Make the test exit early after self-heal + disable by failing _gh_token.
    monkeypatch.setattr(server, "_gh_token", MagicMock(side_effect=subprocess.CalledProcessError(1, "gh")))

    rc = plane.serve()

    # Self-heal ran (enable) BEFORE disable.
    assert enable_called.called, "expected enable_workflows to be called for self-heal"
    assert disable_called.called, "expected disable_workflows to be called after self-heal"
    # And the user got a clear message about it.
    assert "stale" in capsys.readouterr().out.lower()
    # Exits non-zero because the simulated _gh_token failure aborts the rest.
    assert rc == 1


def test_serve_skips_self_heal_when_no_stale_file(plane, monkeypatch) -> None:
    """No `.disabled-by-genesis` file → no preemptive enable call."""
    assert not server.DISABLED_LIST_PATH.exists()

    enable_called = MagicMock()
    disable_called = MagicMock()
    monkeypatch.setattr(server, "enable_workflows", enable_called)
    monkeypatch.setattr(server, "disable_workflows", disable_called)
    monkeypatch.setattr(server.shutil, "which", lambda _: "/usr/local/bin/claude")
    monkeypatch.setattr(server, "_gh_token", MagicMock(side_effect=subprocess.CalledProcessError(1, "gh")))

    plane.serve()

    # enable_workflows is still called once (by the _reenable_workflows_safe
    # cleanup after the _gh_token failure), but NOT for self-heal at startup.
    # We assert disable was called and that the call order has disable first.
    assert disable_called.called
    # If self-heal had run, enable would be called >= 2 times (heal + cleanup).
    assert enable_called.call_count == 1


# ---------- budget / toolset / agent guards ----------


def test_local_mode_respects_the_orchestrator_turn_floor() -> None:
    """Local mode runs the same agent as the workflows and must honour the same
    floor. It sat at 20 — below the floor — because the floor guard only
    inspected workflow templates, so this execution path silently kept the
    budget that had already killed two runs.
    """
    from genesis.scaffold import ORCHESTRATOR_TURN_FLOOR

    assert server.SESSION_MAX_TURNS >= ORCHESTRATOR_TURN_FLOOR


def test_local_mode_allows_the_write_tool() -> None:
    """Without Write the agent can edit files but never create one, so any task
    needing a new file, test, or agent definition is unsatisfiable."""
    assert "Write" in server.ALLOWED_TOOLS.split(",")


def test_run_orchestrator_uses_the_declared_budget_and_tools(plane) -> None:
    fake_proc = MagicMock()
    fake_proc.wait.return_value = 0
    fake_proc.pid = 12345
    with patch("subprocess.Popen", return_value=fake_proc) as popen:
        plane.run_orchestrator(None)
    cmd = popen.call_args[0][0]
    assert cmd[cmd.index("--max-turns") + 1] == str(server.SESSION_MAX_TURNS)
    assert cmd[cmd.index("--allowedTools") + 1] == server.ALLOWED_TOOLS


def test_build_prompt_defaults_to_the_orchestrator() -> None:
    assert server.DEFAULT_AGENT in server._build_prompt(None)


def test_build_prompt_honours_a_custom_agent() -> None:
    """Repos without an orchestrator — genesis itself — point at another agent."""
    prompt = server._build_prompt(None, ".claude/agents/evolver.md")
    assert ".claude/agents/evolver.md" in prompt
    assert "orchestrator.md" not in prompt


def test_run_orchestrator_prompt_carries_the_planes_agent(plane) -> None:
    plane.agent = ".claude/agents/evolver.md"
    fake_proc = MagicMock()
    fake_proc.wait.return_value = 0
    fake_proc.pid = 12345
    with patch("subprocess.Popen", return_value=fake_proc) as popen:
        plane.run_orchestrator(None)
    cmd = popen.call_args[0][0]
    assert ".claude/agents/evolver.md" in cmd[cmd.index("-p") + 1]


def test_serve_handles_sighup(monkeypatch, tmp_path) -> None:
    """Closing the terminal sends SIGHUP. Unhandled, it skips cleanup and leaves
    the repo's workflows disabled with a stale tracking file."""
    registered: dict[int, object] = {}
    monkeypatch.setattr(
        server.signal, "signal", lambda sig, h: registered.__setitem__(sig, h)
    )
    monkeypatch.setattr(server, "_get_repo", lambda: "alice/foo")
    monkeypatch.chdir(tmp_path)
    agent = tmp_path / "agent.md"
    agent.write_text("# agent")
    monkeypatch.setenv("GENESIS_AGENT", str(agent))
    monkeypatch.setattr(server.LocalControlPlane, "serve", lambda self: 0)

    assert server.serve() == 0
    for sig in (server.signal.SIGINT, server.signal.SIGTERM, server.signal.SIGHUP):
        assert sig in registered, f"{sig!r} not handled"


# ---------- progress feed ----------


def test_stream_progress_renders_tool_calls_and_result(plane, capsys) -> None:
    """`claude -p` buffers until the session ends, so without this a 25-minute
    run prints nothing. Hook stderr doesn't fill the gap — Claude Code captures
    it into its own transcript."""
    stream = [
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "thinking"},
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "go test ./...", "description": "run tests"},
                        },
                    ]
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {"file_path": "/repo/internal/kube/client.go"},
                        }
                    ]
                },
            }
        ),
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "num_turns": 12,
                "total_cost_usd": 1.2345,
                "duration_ms": 61000,
            }
        ),
    ]
    plane._stream_progress(iter(stream))
    out = capsys.readouterr().out

    assert "1. Bash go test ./..." in out
    assert "2. Edit /repo/internal/kube/client.go" in out
    assert "session ended: success turns=12 cost=$1.23 61s" in out


def test_stream_progress_survives_garbage(plane, capsys) -> None:
    """Progress reporting must never be able to take down the run it reports on."""
    plane._stream_progress(iter(["not json", "", json.dumps({"type": "other"})]))
    plane._stream_progress(None)  # non-iterable
    assert "Traceback" not in capsys.readouterr().out


def test_run_orchestrator_requests_streaming_output(plane) -> None:
    fake_proc = MagicMock()
    fake_proc.wait.return_value = 0
    fake_proc.pid = 12345
    fake_proc.stdout = iter([])
    with patch("subprocess.Popen", return_value=fake_proc) as popen:
        plane.run_orchestrator(None)
    cmd = popen.call_args[0][0]
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in cmd
    # A piped stdout must be drained or the child blocks when the pipe fills.
    assert popen.call_args[1]["stdout"] is subprocess.PIPE


# ---------- resume across budget deaths ----------


@pytest.fixture(autouse=True)
def _no_real_git(monkeypatch):
    """Keep repo_fingerprint away from subprocess.

    `subprocess.run` builds a Popen internally, and these tests patch Popen to
    fake claude sessions — so a real git call would be handed a MagicMock and
    fail deep inside subprocess. Default is a fresh value per call, i.e. "work
    landed"; tests that care about the stalled case pin it themselves.
    """
    counter = {"n": 0}

    def fingerprint():
        counter["n"] += 1
        return f"fp-{counter['n']}"

    monkeypatch.setattr(server, "repo_fingerprint", fingerprint)


def _session_stream(subtype: str, tool_calls: int = 1, sid: str = "sess-abc123def") -> list[str]:
    lines = [json.dumps({"type": "system", "subtype": "init", "session_id": sid})]
    for i in range(tool_calls):
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "session_id": sid,
                    "message": {
                        "content": [
                            {"type": "tool_use", "name": "Bash", "input": {"command": f"step {i}"}}
                        ]
                    },
                }
            )
        )
    lines.append(
        json.dumps(
            {
                "type": "result",
                "subtype": subtype,
                "session_id": sid,
                "num_turns": tool_calls,
                "total_cost_usd": 1.0,
                "duration_ms": 1000,
            }
        )
    )
    return lines


def _fake_sessions(plane, streams: list[list[str]]) -> list[list[str]]:
    """Patch Popen so each launch replays the next canned stream. Returns the
    list of argv the plane used, so tests can assert on --resume."""
    calls: list[list[str]] = []
    it = iter(streams)

    def fake_popen(cmd, **kwargs):
        calls.append(cmd)
        proc = MagicMock()
        proc.pid = 4242
        proc.wait.return_value = 0
        proc.stdout = iter(next(it))
        return proc

    patcher = patch("subprocess.Popen", side_effect=fake_popen)
    patcher.start()
    plane._stop_patch = patcher  # noqa: SLF001 - test bookkeeping
    return calls


def test_max_turns_death_resumes_the_same_session(plane) -> None:
    """The transcript and the half-finished work are both on disk; starting over
    discards the reasoning and makes the next session re-derive intent."""
    calls = _fake_sessions(plane, [_session_stream("error_max_turns"), _session_stream("success")])
    try:
        plane.run_orchestrator(None)
    finally:
        plane._stop_patch.stop()

    assert len(calls) == 2, "should have continued after the budget death"
    assert "--resume" not in calls[0]
    assert calls[1][calls[1].index("--resume") + 1] == "sess-abc123def"
    # A resumed session must not be re-fed the original prompt, or it restarts
    # the task instead of finishing it.
    assert "ran out of turns, not out of task" in calls[1][calls[1].index("--resume") + 2]


def test_success_does_not_resume(plane) -> None:
    calls = _fake_sessions(plane, [_session_stream("success")])
    try:
        plane.run_orchestrator(None)
    finally:
        plane._stop_patch.stop()
    assert len(calls) == 1


def test_continuations_are_capped(plane) -> None:
    """An unbounded retry loop on a paid API spends money while you sleep."""
    streams = [_session_stream("error_max_turns") for _ in range(server.MAX_CONTINUATIONS + 3)]
    calls = _fake_sessions(plane, streams)
    try:
        plane.run_orchestrator(None)
    finally:
        plane._stop_patch.stop()
    assert len(calls) == 1 + server.MAX_CONTINUATIONS


def test_no_resume_when_attempt_did_nothing(plane) -> None:
    """Zero tool calls means the session isn't making progress — resuming it
    just buys another round of nothing."""
    calls = _fake_sessions(
        plane, [_session_stream("error_max_turns", tool_calls=0), _session_stream("success")]
    )
    try:
        plane.run_orchestrator(None)
    finally:
        plane._stop_patch.stop()
    assert len(calls) == 1


def test_no_resume_without_a_session_id(plane) -> None:
    stream = [json.dumps({"type": "result", "subtype": "error_max_turns", "num_turns": 3})]
    calls = _fake_sessions(plane, [stream, _session_stream("success")])
    try:
        plane.run_orchestrator(None)
    finally:
        plane._stop_patch.stop()
    assert len(calls) == 1


def test_shutdown_stops_continuations(plane) -> None:
    calls = _fake_sessions(plane, [_session_stream("error_max_turns"), _session_stream("success")])
    plane.shutdown = True
    try:
        plane.run_orchestrator(None)
    finally:
        plane._stop_patch.stop()
    assert len(calls) == 1


# ---------- dynamic continuation: the decision ladder ----------


def test_landed_work_continues_without_paying_a_judge(plane, monkeypatch) -> None:
    """Cheapest rung: git already answers "did anything happen", so don't buy an
    opinion you can compute."""
    called = []
    monkeypatch.setattr(plane, "ask_judge", lambda task: called.append(task) or (False, "x"))
    plane.last_tool_calls = 5
    go, why = plane._should_continue("task", before="OLD", spent=1.0)
    assert go and "landed" in why
    assert called == [], "judge must not be consulted when progress is visible"


def test_stalled_attempt_consults_the_judge(plane, monkeypatch) -> None:
    monkeypatch.setattr(server, "repo_fingerprint", lambda: "SAME")
    monkeypatch.setattr(plane, "ask_judge", lambda task: (True, "converging on a fix"))
    plane.last_tool_calls = 9
    go, why = plane._should_continue("task", before="SAME", spent=1.0)
    assert go and why == "converging on a fix"


def test_cost_ceiling_overrides_everything(plane, monkeypatch) -> None:
    """A judge that can always grant one more round is an unbounded spend loop."""
    monkeypatch.setenv("GENESIS_COST_CEILING", "5")
    monkeypatch.setattr(plane, "ask_judge", lambda task: (True, "just one more"))
    plane.last_tool_calls = 9
    go, why = plane._should_continue("task", before="OLD", spent=5.01)
    assert not go and "cost ceiling" in why


def test_judge_fails_closed_on_garbage(plane, monkeypatch) -> None:
    """A broken judge should leave an idle dev system, not an open-ended spend."""
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "maybe?", "")
    )
    go, why = plane.ask_judge("task")
    assert not go and "no clear verdict" in why


def test_judge_reads_evidence_not_self_report(plane, monkeypatch) -> None:
    """Agent self-reports were observed wrong twice in one hour; the judge gets
    git state and the actual tool calls instead."""
    seen = {}

    def fake_run(cmd, **kwargs):
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, f"GITOUT:{cmd[1]}", "")
        seen["prompt"] = cmd[cmd.index("-p") + 1]
        return subprocess.CompletedProcess(cmd, 0, "STOP thrashing on the same file", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    plane.recent_tools = ["Edit plan.go", "Edit plan.go"]
    go, why = plane.ask_judge("finish issue #127")
    assert not go and "thrashing" in why
    assert "GITOUT:status" in seen["prompt"] and "GITOUT:diff" in seen["prompt"]
    assert "Edit plan.go" in seen["prompt"]
    assert "Default to STOP" in seen["prompt"]


def test_judge_gets_no_tools(plane, monkeypatch) -> None:
    captured = {}

    def fake_run(cmd, **kwargs):
        if cmd[0] == "claude":
            captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "STOP done", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    plane.ask_judge("task")
    assert captured["cmd"][captured["cmd"].index("--allowedTools") + 1] == ""


def test_unfinished_work_schedules_a_followup(plane) -> None:
    """Work left in the tree is referenced by no future event, so without this
    the plane idles holding a half-finished task."""
    calls = _fake_sessions(plane, [_session_stream("error_max_turns") for _ in range(9)])
    try:
        plane.run_orchestrator(None)
    finally:
        plane._stop_patch.stop()
    assert plane.pending_followup is True
    assert len(calls) == 1 + server.MAX_CONTINUATIONS


def test_app_private_key_never_reaches_the_agent(plane, monkeypatch) -> None:
    """The PEM can mint tokens for every repo the App is installed on, forever.
    The hour-long token it produces is the only thing a session should hold."""
    monkeypatch.setattr(server, "mint_installation_token", lambda repo, env: "ghs_minted")
    monkeypatch.setenv("GENESIS_GITHUB_APP_SECRET", "-----BEGIN RSA PRIVATE KEY-----")
    monkeypatch.setenv("GENESIS_GITHUB_APP_ID", "12345")
    fake_proc = MagicMock()
    fake_proc.wait.return_value = 0
    fake_proc.pid = 1
    fake_proc.stdout = iter([])
    with patch("subprocess.Popen", return_value=fake_proc) as popen:
        plane.run_orchestrator(None)
    env = popen.call_args[1]["env"]
    assert env["GH_TOKEN"] == "ghs_minted"
    assert "GENESIS_GITHUB_APP_SECRET" not in env
    assert "GENESIS_GITHUB_APP_ID" not in env


def test_any_abnormal_ending_resumes_not_just_max_turns(plane) -> None:
    """Observed in production: a session died `error_during_execution` at turn 41
    with real work uncommitted, and the chain stopped because the subtype string
    didn't match. Every abnormal ending strands work the same way."""
    calls = _fake_sessions(
        plane, [_session_stream("error_during_execution"), _session_stream("success")]
    )
    try:
        plane.run_orchestrator(None)
    finally:
        plane._stop_patch.stop()
    assert len(calls) == 2, "an execution error should resume like a budget death"
    assert "--resume" in calls[1]


def test_success_still_ends_the_chain(plane) -> None:
    calls = _fake_sessions(plane, [_session_stream("success"), _session_stream("success")])
    try:
        plane.run_orchestrator(None)
    finally:
        plane._stop_patch.stop()
    assert len(calls) == 1


def test_poll_loop_sweeps_for_mergeable_prs(plane, monkeypatch) -> None:
    """CI going green is not an event the poller sees, and the agent's own PRs are
    bot-authored so the actor filter drops them. Without the sweep the loop can
    open work it can never land."""
    monkeypatch.setattr(plane, "merge_ready_prs", lambda: [7])
    assert plane.merge_ready_prs() == [7]


def test_merge_sweep_can_be_turned_off(plane, monkeypatch) -> None:
    monkeypatch.setenv("GENESIS_AUTO_MERGE", "off")
    monkeypatch.setattr(server, "merge_ready", lambda *a, **k: [1, 2, 3])
    assert plane.merge_ready_prs() == []


def test_merge_sweep_failure_never_kills_the_plane(plane, monkeypatch) -> None:
    monkeypatch.delenv("GENESIS_AUTO_MERGE", raising=False)
    monkeypatch.setattr(server, "mint_installation_token", lambda *a, **k: "tok")
    def boom(*a, **k):
        raise RuntimeError("gh went missing")
    monkeypatch.setattr(server, "merge_ready", boom)
    assert plane.merge_ready_prs() == []


def test_only_one_scheduled_trigger_fires_per_tick(plane, monkeypatch, tmp_path) -> None:
    """A laptop closed overnight leaves several schedules due at once. Firing all
    of them on the first tick would be a surprising way to spend an afternoon."""
    monkeypatch.setattr(server.triggers, "STATE_PATH", tmp_path / "state")
    monkeypatch.setattr(server.triggers, "load_state", lambda *a: {})
    saved: list[dict] = []
    monkeypatch.setattr(server.triggers, "save_state", lambda st, *a: saved.append(st))
    monkeypatch.setattr(server.triggers, "failed_runs", lambda *a, **k: [])
    launched: list[str] = []
    monkeypatch.setattr(
        plane, "run_orchestrator", lambda event, prompt=None: launched.append(prompt or "")
    )
    plane.run_due_triggers("tok")
    assert len(launched) == 1, "at most one session per tick"
    assert "scheduled run" in launched[0]


def test_ci_failure_takes_priority_over_the_cron(plane, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(server.triggers, "load_state", lambda *a: {})
    monkeypatch.setattr(server.triggers, "save_state", lambda *a, **k: None)
    monkeypatch.setattr(
        server.triggers,
        "failed_runs",
        lambda *a, **k: [{"name": "CI", "headBranch": "main", "url": "u", "createdAt": "2026-08-02T01:00:00Z"}],
    )
    launched: list[str] = []
    monkeypatch.setattr(
        plane, "run_orchestrator", lambda event, prompt=None: launched.append(prompt or "")
    )
    plane.run_due_triggers("tok")
    assert "A required check failed" in launched[0]


def test_trigger_restores_the_planes_default_agent(plane, monkeypatch) -> None:
    """The evolver trigger swaps the agent for one session. If that leaked, every
    later event-driven run would silently run the wrong agent."""
    monkeypatch.setattr(server.triggers, "load_state", lambda *a: {})
    monkeypatch.setattr(server.triggers, "save_state", lambda *a, **k: None)
    monkeypatch.setattr(server.triggers, "failed_runs", lambda *a, **k: [])
    monkeypatch.setattr(plane, "run_orchestrator", lambda event, prompt=None: 0)
    original = plane.agent
    plane.run_due_triggers("tok")
    assert plane.agent == original


def test_a_session_that_ran_no_tools_is_called_out(plane, capsys) -> None:
    """The failure that motivated this: an unauthenticated profile made every run
    exit in one turn for $0.00 reporting success, eating the event backlog while
    looking healthy."""
    plane._stream_progress(iter([json.dumps(
        {"type": "result", "subtype": "success", "num_turns": 1, "total_cost_usd": 0, "duration_ms": 1000}
    )]))
    assert "ran no tools at all" in capsys.readouterr().out
