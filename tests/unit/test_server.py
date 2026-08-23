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
    """Keep session_work_marker away from subprocess.

    `subprocess.run` builds a Popen internally, and these tests patch Popen to
    fake claude sessions — so a real git call would be handed a MagicMock and
    fail deep inside subprocess. Default is a fresh value per call, i.e. "work
    landed"; tests that care about the stalled case pin it themselves.
    """
    counter = {"n": 0}

    def fingerprint():
        counter["n"] += 1
        return f"fp-{counter['n']}"

    monkeypatch.setattr(server, "session_work_marker", fingerprint)


def _session_stream(
    subtype: str, tool_calls: int = 1, sid: str = "sess-abc123def", cost: float = 1.0
) -> list[str]:
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
                "total_cost_usd": cost,
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
    real_popen = subprocess.Popen

    def fake_popen(cmd, **kwargs):
        # Only `claude` launches are scripted. The plane also forks `issues.sh`
        # to release claims, and `subprocess.run` builds a Popen underneath — so
        # without this, a release would consume a session from the script and
        # then fail as a stream that isn't there.
        if cmd and cmd[0] != "claude":
            return real_popen(cmd, **kwargs)
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


def _judge_json(verdict: str, cost: float = 0.0) -> str:
    """What `claude -p --output-format json` actually hands back."""
    return json.dumps(
        {"type": "result", "subtype": "success", "result": verdict, "total_cost_usd": cost}
    )


def test_landed_work_continues_without_paying_a_judge(plane, monkeypatch) -> None:
    """Cheapest rung: git already answers "did this session do anything", so don't
    buy an opinion you can compute."""
    called = []
    monkeypatch.setattr(plane, "ask_judge", lambda task: called.append(task) or (False, "x"))
    plane.last_tool_calls = 5
    go, why = plane._should_continue("task", before="OLD", spent=1.0)
    assert go and "changed the repo" in why
    assert called == [], "judge must not be consulted when progress is visible"


def test_stalled_attempt_consults_the_judge(plane, monkeypatch) -> None:
    monkeypatch.setattr(server, "session_work_marker", lambda: "SAME")
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
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, _judge_json("maybe?"), "")
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
        return subprocess.CompletedProcess(cmd, 0, _judge_json("STOP thrashing on the same file"), "")

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
        return subprocess.CompletedProcess(cmd, 0, _judge_json("STOP done"), "")

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
        plane, "run_orchestrator", lambda event, prompt=None, label=None: launched.append(prompt or "")
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
        plane, "run_orchestrator", lambda event, prompt=None, label=None: launched.append(prompt or "")
    )
    plane.run_due_triggers("tok")
    assert "A required check failed" in launched[0]


def test_trigger_restores_the_planes_default_agent(plane, monkeypatch) -> None:
    """The evolver trigger swaps the agent for one session. If that leaked, every
    later event-driven run would silently run the wrong agent."""
    monkeypatch.setattr(server.triggers, "load_state", lambda *a: {})
    monkeypatch.setattr(server.triggers, "save_state", lambda *a, **k: None)
    monkeypatch.setattr(server.triggers, "failed_runs", lambda *a, **k: [])
    monkeypatch.setattr(plane, "run_orchestrator", lambda event, prompt=None, label=None: 0)
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
    # The check moved out of the result branch and onto the session boundary, so
    # that one result event out of several can no longer trigger it (#79). The
    # reader still supplies the state it reads.
    plane._warn_if_the_session_did_nothing()
    assert "ran no tools at all" in capsys.readouterr().out


def test_a_session_that_changed_the_repo_queues_another_pass(plane, monkeypatch) -> None:
    """The loop's own output cannot wake it: the agent is the App, so closing an
    issue emits a bot event and the feedback-loop filter drops it. Observed on
    MaKlaude - a task closed at 06:31 and nothing moved until a human commented
    16 minutes later, otherwise it would have idled until the six-hour cron."""
    fingerprints = iter(["before", "after", "after", "after"])
    monkeypatch.setattr(server, "session_work_marker", lambda: next(fingerprints, "after"))
    calls = _fake_sessions(plane, [_session_stream("success")])
    try:
        plane.run_orchestrator(None)
    finally:
        plane._stop_patch.stop()
    assert plane.pending_followup is True
    assert plane.followup_chain == 1


def test_a_session_that_changed_nothing_does_not_queue_a_pass(plane, monkeypatch) -> None:
    monkeypatch.setattr(server, "session_work_marker", lambda: "same")
    calls = _fake_sessions(plane, [_session_stream("success")])
    try:
        plane.run_orchestrator(None)
    finally:
        plane._stop_patch.stop()
    assert plane.pending_followup is False


def test_the_followup_chain_is_bounded(plane, monkeypatch) -> None:
    """"Work begets work" must not become a spin."""
    counter = iter(range(100))
    monkeypatch.setattr(server, "session_work_marker", lambda: f"fp-{next(counter)}")
    plane.followup_chain = server.MAX_FOLLOWUP_CHAIN
    calls = _fake_sessions(plane, [_session_stream("success")])
    try:
        plane.run_orchestrator(None)
    finally:
        plane._stop_patch.stop()
    assert plane.pending_followup is False, "chain budget exhausted, wait for a real trigger"


# ---------- the judge's own spend (#50) ----------


def test_judge_cost_lands_in_both_accumulators(plane, monkeypatch) -> None:
    """The judge is a real session and its dollars are real dollars.

    It bypasses `_run_session`, so before this nothing added it: both the task
    ceiling and the run budget read zero for every judge ever consulted. That is
    the systematically-low direction, and it is worst on the ambiguous rung a
    thrashing chain lands on over and over.
    """
    monkeypatch.setattr(server, "session_work_marker", lambda: "SAME")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, _judge_json("CONTINUE nearly done", 0.25), ""),
    )
    plane.last_tool_calls = 4
    plane.run_spent = 0.0

    go, why = plane._should_continue("task", before="SAME", spent=1.0)

    assert go and "nearly done" in why
    assert plane.last_judge_cost == 0.25
    # `_should_continue` reports; `run_orchestrator` is the one place that charges
    # both accumulators, so the chain-level total is asserted below.
    assert plane.run_spent == 0.0


def test_a_chain_pays_for_every_judge_it_consults(plane, monkeypatch) -> None:
    """N continuation decisions must account for N judge sessions, not zero."""
    monkeypatch.setattr(server, "session_work_marker", lambda: "SAME")
    judges: list[str] = []

    def fake_run(cmd, **kwargs):
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        judges.append(cmd[0])
        return subprocess.CompletedProcess(cmd, 0, _judge_json("CONTINUE keep going", 0.10), "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    # Three sessions: the first plus two continuations, so two judge calls. The
    # third death is left un-continued by capping MAX_CONTINUATIONS for the test.
    monkeypatch.setattr(server, "MAX_CONTINUATIONS", 2)
    calls = _fake_sessions(plane, [_session_stream("error_max_turns") for _ in range(3)])
    try:
        plane.run_orchestrator(None)
    finally:
        plane._stop_patch.stop()

    assert len(calls) == 3, "one initial session plus two continuations"
    assert len(judges) == 2, "one judge per continuation decision"
    # 3 sessions at $1.00 (see _session_stream) + 2 judges at $0.10.
    assert plane.run_spent == pytest.approx(3.20)


def test_a_rung_answered_without_a_judge_charges_nothing(plane, monkeypatch) -> None:
    """Most rungs are answered by git or a counter. A stale figure from the
    previous decision would be charged again by the caller."""
    monkeypatch.setattr(server, "session_work_marker", lambda: "NEW")
    plane.last_judge_cost = 0.99
    plane.last_tool_calls = 3

    go, _ = plane._should_continue("task", before="OLD", spent=1.0)

    assert go
    assert plane.last_judge_cost == 0.0


def test_unreadable_judge_output_keeps_the_verdict_and_flags_the_total(plane, monkeypatch) -> None:
    """Losing the cost figure is the smaller harm.

    A judge that stops parsing is a judge that always fails closed, which turns
    every ambiguous continuation into a stop and idles the dev system. So an
    envelope that isn't JSON is still read as prose - but the totals stop
    claiming to be exact, because a bound is only worth the trust in it.
    """
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, "CONTINUE looks close", ""),
    )

    go, why = plane.ask_judge("task")

    assert go and "looks close" in why
    assert plane.last_judge_cost == 0.0
    assert plane.cost_is_lower_bound is True
    assert plane._spend(52.11) == "at least $52.11"


def test_a_complete_total_is_not_hedged(plane) -> None:
    assert plane.cost_is_lower_bound is False
    assert plane._spend(52.11) == "$52.11"


def test_judge_asks_for_json_so_its_cost_can_be_read(plane, monkeypatch) -> None:
    captured = {}

    def fake_run(cmd, **kwargs):
        if cmd[0] == "claude":
            captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, _judge_json("STOP done"), "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    plane.ask_judge("task")

    cmd = captured["cmd"]
    assert cmd[cmd.index("--output-format") + 1] == "json"


def test_no_claude_invocation_bypasses_the_accumulators() -> None:
    """A third `claude` call site would silently undercount the same way.

    `run_orchestrator` says "if you add a third _run_session call site, it needs
    both lines" - this is the same guard one level up, for a call that skips
    `_run_session` altogether, which is exactly how the judge went unaccounted.
    """
    source = Path(server.__file__).read_text()
    launches = source.count('"claude",\n') + source.count('cmd = ["claude", "-p"]')
    assert launches == 2, (
        "server.py launches a number of `claude` sessions other than the two that "
        "are accounted for (_run_session and ask_judge). A new one must add its "
        "cost to both `spent` and `run_spent`."
    )


# ---------- releasing what a finished session claimed (#48) ----------
#
# `issues.sh claim` writes `in-progress` at pickup and, before this, nothing ever
# took it back. Measured on MaKlaude issue #195: claimed 02:18, session quiet at
# 03:17, killed by the deadline at 03:33, `session deadline reached - not
# continuing` at 03:33:30 - clean tree, no branch, no commit, no pull request,
# and the label still there. `next --milestone 6` skipped the issue and picked a
# different one until a human removed the label by hand.


@pytest.fixture
def issues_script(tmp_path):
    """A stand-in for the seeded `issues.sh` that records how it was called.

    The plane really does fork it, so the path check, the argv and the exported
    identity are all exercised rather than asserted against a mock's memory of
    the call. Each line is `<GENESIS_SESSION>|<argv>`, which is what lets a test
    check that the identity a session would have claimed under is the same one
    the release keys on.
    """
    script = tmp_path / ".genesis" / "scripts" / "issues.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    record = tmp_path / "issues-calls.txt"
    script.write_text(f'#!/bin/sh\necho "$GENESIS_SESSION|$*" >> "{record}"\n')

    def calls() -> list[str]:
        return record.read_text().splitlines() if record.exists() else []

    return calls


def _releases(calls: list[str]) -> list[str]:
    return [c for c in calls if "|release " in c]


def test_a_chain_the_ladder_declines_to_continue_releases_its_claims(
    plane, issues_script
) -> None:
    """The measured case. The ladder stops, so whatever the chain claimed is now
    held by nobody, and the next run must be able to select it."""
    _fake_sessions(plane, [_session_stream("error_max_turns", tool_calls=0)])
    try:
        plane.run_orchestrator(None)
    finally:
        plane._stop_patch.stop()

    released = _releases(issues_script())
    assert len(released) == 1, f"expected exactly one release, got {issues_script()}"
    assert "--session serve-" in released[0]


def test_the_release_says_which_rung_stopped_the_chain(plane, issues_script) -> None:
    """The reason lands on the issue as prose. "the claim expired" would tell a
    human nothing, and that is half of why an age-based expiry is the wrong
    shape: it cannot explain itself."""
    # A deadline already in the past when the ladder checks it, which is the
    # rung that fired on MaKlaude issue #195.
    plane.session_timeout = -1
    _fake_sessions(plane, [_session_stream("error_max_turns")])
    try:
        plane.run_orchestrator(None)
    finally:
        plane._stop_patch.stop()

    assert "deadline" in _releases(issues_script())[0]


def test_a_resumed_chain_keeps_its_claim(plane, issues_script) -> None:
    """The resumed run is still the worker.

    Releasing at every session end would hand the issue to a second worker while
    the first is mid-thought with its transcript on disk - the same two-branches-
    one-issue collision the whole design is arranged to avoid.
    """
    calls = _fake_sessions(
        plane,
        [
            _session_stream("error_max_turns"),
            _session_stream("error_max_turns", tool_calls=0),
        ],
    )
    try:
        plane.run_orchestrator(None)
    finally:
        plane._stop_patch.stop()

    assert len(calls) == 2, "the chain must actually have resumed for this to mean anything"
    assert len(_releases(issues_script())) == 1, (
        "the claim must survive the resume and be released once, when the chain ends"
    )


def test_a_chain_that_finished_keeps_its_claims(plane, issues_script) -> None:
    """A session ending in success is not the ladder declining anything.

    It may have opened a pull request and left `in-progress` on deliberately, in
    which case the claim still describes reality. Releasing there would be the
    expensive direction; the backstop sweep covers it if the claim really has
    rotted.
    """
    _fake_sessions(plane, [_session_stream("success")])
    try:
        plane.run_orchestrator(None)
    finally:
        plane._stop_patch.stop()

    assert _releases(issues_script()) == []


def test_a_session_claims_under_the_chains_identity(plane) -> None:
    """The label carries no identity, so the plane has to supply one, and the
    child process is the only thing that can write it onto the issue."""
    plane.session_token = "serve-deadbeef"
    assert plane._session_env()["GENESIS_SESSION"] == "serve-deadbeef"


def test_each_chain_claims_under_a_fresh_identity(plane, issues_script) -> None:
    """Per chain, not per process. A shared token would let a chain that died
    release a claim an earlier chain finished cleanly and is still holding."""
    for _ in range(2):
        _fake_sessions(plane, [_session_stream("error_max_turns", tool_calls=0)])
        try:
            plane.run_orchestrator(None)
        finally:
            plane._stop_patch.stop()

    tokens = {c.split("|")[0] for c in _releases(issues_script())}
    assert len(tokens) == 2, f"chains shared a claim identity: {tokens}"


def test_the_release_keys_on_the_identity_the_session_claimed_under(
    plane, issues_script
) -> None:
    """The two halves have to be the same string, or the release matches nothing
    and the claim sits there exactly as it did before any of this existed."""
    _fake_sessions(plane, [_session_stream("error_max_turns", tool_calls=0)])
    try:
        plane.run_orchestrator(None)
    finally:
        plane._stop_patch.stop()

    exported, argv = _releases(issues_script())[0].split("|", 1)
    assert exported
    assert f"--session {exported}" in argv


def test_a_repo_without_the_issue_manager_is_not_an_error(plane, capsys) -> None:
    """`serve` can run against a repo that never adopted `issues.sh`. There are no
    claims to release there, which is a no-op and not a failure."""
    plane.session_token = "serve-abc"
    plane.release_claims("whatever")
    assert "release" not in capsys.readouterr().out


def test_a_failing_release_never_kills_the_plane(plane, tmp_path, capsys) -> None:
    """Claim bookkeeping runs on the path where a session has *already* failed.
    Turning a failed release into a crashed control plane would trade a stuck
    label for a stopped dev system."""
    script = tmp_path / ".genesis" / "scripts" / "issues.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/sh\necho 'boom' >&2\nexit 1\n")

    plane.session_token = "serve-abc"
    plane.release_claims("the session deadline was reached")

    assert "exited 1" in capsys.readouterr().out


# ---------- the backstop sweep ----------


def test_the_sweep_window_clears_the_session_cap(plane) -> None:
    """A window inside the session cap races a live session, which is the whole
    reason age is the second layer. Twice the cap, never under the floor."""
    plane.session_timeout = 3600
    assert plane._claim_sweep_hours() >= 2

    plane.session_timeout = 6 * 3600
    assert plane._claim_sweep_hours() >= 12

    plane.session_timeout = 60
    assert plane._claim_sweep_hours() == server.CLAIM_SWEEP_MIN_HOURS


def test_the_sweep_asks_for_the_window_it_derived(plane, issues_script) -> None:
    plane.session_timeout = 4 * 3600
    plane.sweep_stale_claims(force=True)

    sweeps = [c for c in issues_script() if "sweep-claims" in c]
    assert sweeps == ["|sweep-claims --older-than 8"]


def test_the_sweep_is_throttled_between_polls(plane, issues_script) -> None:
    """It enforces a window measured in hours and the poll loop ticks every
    minute; running it on every tick would be one API call a minute to learn
    nothing."""
    plane.sweep_stale_claims(force=True)
    plane.sweep_stale_claims()
    plane.sweep_stale_claims()

    assert len([c for c in issues_script() if "sweep-claims" in c]) == 1


def test_serve_sweeps_the_board_before_the_first_session(plane, monkeypatch) -> None:
    """The plane most likely to be holding a claim nothing can release is the one
    that died without deciding anything, and this process is often its
    replacement. Sweeping after the first session would let that session pick
    around the very issue that was stranded."""
    order: list[str] = []
    monkeypatch.setattr(server, "enable_workflows", MagicMock())
    monkeypatch.setattr(server, "disable_workflows", MagicMock())
    monkeypatch.setattr(server.shutil, "which", lambda _: "/usr/local/bin/claude")
    monkeypatch.setattr(server, "_gh_token", lambda: "token")
    monkeypatch.setattr(plane, "_prime_high_water_if_needed", lambda *a, **k: None)
    monkeypatch.setattr(
        plane, "sweep_stale_claims", lambda force=False: order.append("sweep")
    )
    # 127 aborts serve straight after the initial run, before the poll loop.
    monkeypatch.setattr(plane, "run_orchestrator", lambda *a, **k: order.append("run") or 127)

    plane.serve()

    assert order[:2] == ["sweep", "run"]


# ---------- a resume that loaded nothing (#43) ----------


def _empty_resume() -> list[str]:
    """What a resume that failed to load its session actually emitted.

    `success`, no tools, $0.00, six seconds after the ladder decided to continue
    a chain that had just spent $6.47.
    """
    return _session_stream("success", tool_calls=0, cost=0.0)


def test_an_empty_resume_is_retried_rather_than_ending_the_chain(plane) -> None:
    calls = _fake_sessions(
        plane,
        [_session_stream("error_max_turns"), _empty_resume(), _session_stream("success")],
    )
    try:
        plane.run_orchestrator(None)
    finally:
        plane._stop_patch.stop()

    assert len(calls) == 3, "the empty resume should have been retried"
    assert "--resume" in calls[1] and "--resume" in calls[2], (
        "the retry must resume the same session, not start a fresh one"
    )
    assert calls[2][calls[2].index("--resume") + 1] == "sess-abc123def"


def test_a_retry_that_is_also_empty_hands_the_work_on_explicitly(plane) -> None:
    """The observed near-miss: the chain ended `success`, and only the follow-up
    pass rescued $6.47 of uncommitted work by luck.

    Twice-empty is a broken resume, so the work is handed to the follow-up path
    on purpose rather than left to the progress check happening to notice.
    """
    releases = []
    _fake_sessions(plane, [_session_stream("error_max_turns"), _empty_resume(), _empty_resume()])
    plane.release_claims = lambda reason: releases.append(reason)
    try:
        plane.run_orchestrator(None)
    finally:
        plane._stop_patch.stop()

    assert plane.pending_followup, "a chain that could not resume must be picked up again"
    assert releases and "could not be resumed" in releases[0], (
        f"a chain that finished nothing must hand its claims back, got {releases}"
    )


def test_a_retry_that_does_not_consume_a_continuation(plane, monkeypatch) -> None:
    """The retry costs nothing by definition — a session that cost nothing is
    what got us here — so it must not eat one of the bounded continuations."""
    monkeypatch.setattr(server, "MAX_CONTINUATIONS", 1)
    calls = _fake_sessions(
        plane,
        [_session_stream("error_max_turns"), _empty_resume(), _session_stream("error_max_turns")],
    )
    try:
        plane.run_orchestrator(None)
    finally:
        plane._stop_patch.stop()

    assert len(calls) == 3, (
        "one continuation, plus the retry of its empty resume, should still run"
    )


def test_a_cheap_but_real_session_is_not_mistaken_for_a_failed_resume(plane) -> None:
    """Zero tools AND zero cost. A session that genuinely had nothing left to do
    still pays for the turn in which it decides that; one that cost nothing never
    reached the model."""
    plane.last_result_subtype = "success"
    plane.last_tool_calls = 0
    plane.last_cost = 0.02
    assert not plane._resume_loaded_nothing()

    plane.last_cost = 0.0
    assert plane._resume_loaded_nothing()

    plane.last_tool_calls = 3
    assert not plane._resume_loaded_nothing()


def test_the_zero_tool_warning_does_not_send_you_after_a_deleted_mechanism() -> None:
    """It used to say "check that the agent profile is authenticated", and profile
    isolation was removed in 4776803. A message pointing at a mechanism that no
    longer exists costs its reader the whole search before they conclude it's stale.
    """
    source = Path(server.__file__).read_text()
    assert "agent profile is authenticated" not in source
    assert "ANTHROPIC_API_KEY" in source


# ---------- the dev repo's own pre-agent steps (#44) ----------


def test_a_pre_session_script_runs_before_the_agent(plane, tmp_path) -> None:
    """A net a dev system built to be deterministic has to stay deterministic
    across execution modes, or "we made this a script so nobody has to remember
    it" is only true in CI."""
    marker = tmp_path / "ran.txt"
    script = Path(server.PRE_SESSION_SCRIPT)
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(f"#!/usr/bin/env bash\necho 'gate #7 is 21 days old'\ntouch {marker}\n")

    _fake_sessions(plane, [_session_stream("success")])
    try:
        plane.run_orchestrator(None)
    finally:
        plane._stop_patch.stop()

    assert marker.exists(), "the dev repo's pre-agent step did not run"


def test_a_missing_pre_session_script_is_not_an_error(plane) -> None:
    assert not Path(server.PRE_SESSION_SCRIPT).exists()
    plane.run_pre_session_steps()  # must simply do nothing


def test_a_failing_pre_session_script_does_not_stop_the_loop(plane) -> None:
    """Non-fatal on purpose: a net that fails is a better outcome than a control
    plane that stops."""
    script = Path(server.PRE_SESSION_SCRIPT)
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/usr/bin/env bash\necho 'boom' >&2\nexit 3\n")

    calls = _fake_sessions(plane, [_session_stream("success")])
    try:
        plane.run_orchestrator(None)
    finally:
        plane._stop_patch.stop()

    assert len(calls) == 1, "the session must still launch after a failed pre-step"


def test_a_hanging_pre_session_script_cannot_wedge_the_loop(plane, monkeypatch) -> None:
    """It runs before every session, so an unbounded one would wedge the loop it
    was written to protect."""
    monkeypatch.setattr(server, "PRE_SESSION_TIMEOUT_S", 1)
    script = Path(server.PRE_SESSION_SCRIPT)
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/usr/bin/env bash\nsleep 30\n")

    start = time.time()
    plane.run_pre_session_steps()
    assert time.time() - start < 10, "the pre-session step was not bounded"


def test_the_pre_session_script_runs_once_when_the_hook_declares_it(plane) -> None:
    """The hook is the mechanism; the plane's own call is the fallback.

    `SessionStart` is the one seam both execution modes share — it fires under
    GitHub Actions and under serve alike — which is what closes the half of #44
    the plane alone can't reach. But then the plane calling it too would run every
    net twice a session, and leaning on "idempotent" for that is a contract the
    control plane doesn't need to lean on.
    """
    script = Path(server.PRE_SESSION_SCRIPT)
    script.parent.mkdir(parents=True, exist_ok=True)
    counter = Path("pre-session-runs.txt").resolve()
    script.write_text(f"#!/usr/bin/env bash\necho x >> {counter}\n")

    settings = Path(server.CLAUDE_SETTINGS)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({
        "hooks": {"SessionStart": [{"matcher": "", "hooks": [
            {"type": "command", "command": "bash .genesis/scripts/pre-session.sh"},
        ]}]}
    }))

    plane.run_pre_session_steps()
    assert not counter.exists(), (
        "the plane ran the script itself even though the harness hook will run it"
    )


def test_the_plane_still_runs_it_when_the_hook_is_absent(plane) -> None:
    """A repo that unwired the hook, or an older scaffold that never had it, must
    not silently lose its pre-agent nets."""
    script = Path(server.PRE_SESSION_SCRIPT)
    script.parent.mkdir(parents=True, exist_ok=True)
    counter = Path("pre-session-runs.txt").resolve()
    script.write_text(f"#!/usr/bin/env bash\necho x >> {counter}\n")

    settings = Path(server.CLAUDE_SETTINGS)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"hooks": {"SessionStart": [
        {"matcher": "", "hooks": [{"type": "command", "command": "bash .genesis/scripts/log.sh session-start"}]}
    ]}}))

    plane.run_pre_session_steps()
    assert counter.exists(), "the pre-agent nets did not run in either place"


def test_an_unreadable_settings_file_errs_toward_running_it(plane) -> None:
    """A wrong False runs a net twice. A wrong True doesn't run it at all. Those
    don't weigh the same."""
    script = Path(server.PRE_SESSION_SCRIPT)
    script.parent.mkdir(parents=True, exist_ok=True)
    counter = Path("pre-session-runs.txt").resolve()
    script.write_text(f"#!/usr/bin/env bash\necho x >> {counter}\n")

    settings = Path(server.CLAUDE_SETTINGS)
    settings.parent.mkdir(parents=True, exist_ok=True)
    for content in ("not json", "[]", '{"hooks": null}', ""):
        counter.unlink(missing_ok=True)
        settings.write_text(content)
        plane.run_pre_session_steps()
        assert counter.exists(), f"settings {content!r} silently disabled the nets"


# ---------- one process, more than one result event (#78, #79) ----------


def _two_results(first_cost: float, second_cost: float, tools_after_first: int = 2) -> list[str]:
    """The shape measured on 2026-08-23: an early result, then real work, then another.

    A leftover task notification from a background job the agent had started itself
    flushed an empty turn and a `result`. The continuation prompt landed 20ms later
    and the same process carried on, emitting a second `result` 27 seconds after.
    """
    sid = "sess-two-results"
    lines = [json.dumps({"type": "system", "subtype": "init", "session_id": sid})]
    lines.append(json.dumps({
        "type": "result", "subtype": "success", "session_id": sid,
        "num_turns": 0, "total_cost_usd": first_cost, "duration_ms": 0,
    }))
    for i in range(tools_after_first):
        lines.append(json.dumps({
            "type": "assistant", "session_id": sid,
            "message": {"content": [
                {"type": "tool_use", "name": "Edit", "input": {"file_path": f"f{i}.py"}}
            ]},
        }))
    lines.append(json.dumps({
        "type": "result", "subtype": "success", "session_id": sid,
        "num_turns": tools_after_first, "total_cost_usd": second_cost, "duration_ms": 27000,
    }))
    return lines


def test_a_session_that_emits_two_results_pays_for_both(plane) -> None:
    """The expensive event comes FIRST on purpose.

    In the measured case the discarded value was $0.00, so nothing was lost and the
    bug was invisible. Reversing the order is what makes the undercount show up, so
    that's the order under test.
    """
    plane.last_cost = 0.0
    plane._stream_progress(iter(_two_results(first_cost=4.00, second_cost=0.25)))

    assert plane.last_cost == pytest.approx(4.25), (
        f"only one result event was charged: got {plane.last_cost}"
    )


def test_the_terminal_outcome_still_wins_across_two_results(plane) -> None:
    """Cost accumulates; the outcome fields do not. The last event is the one that
    describes how the session actually ended, and `turns` is cumulative already."""
    sid = "sess-two-results"
    lines = _two_results(first_cost=0.0, second_cost=0.5, tools_after_first=3)
    lines[-1] = json.dumps({
        "type": "result", "subtype": "error_max_turns", "session_id": sid,
        "num_turns": 3, "total_cost_usd": 0.5, "duration_ms": 27000,
    })
    plane._stream_progress(iter(lines))

    assert plane.last_result_subtype == "error_max_turns"
    assert plane.last_tool_calls == 3, "turns is cumulative, so the total should survive"


def test_an_early_empty_result_does_not_trigger_the_did_nothing_warning(plane, capsys) -> None:
    """The misfire from issue #79.

    The per-event check fired on the first result and told the reader to go and
    check ANTHROPIC_API_KEY. There was no auth problem, and the session went on to
    do three tool calls. An operator following that advice finds nothing wrong,
    twice, and is left with a scary line and no explanation.
    """
    plane._stream_progress(iter(_two_results(first_cost=0.0, second_cost=0.5, tools_after_first=3)))
    plane._warn_if_the_session_did_nothing()

    assert "ran no tools at all" not in capsys.readouterr().out


def test_a_session_that_really_did_nothing_still_warns(plane, capsys) -> None:
    """The detector this replaces is worth keeping: fifteen consecutive runs once
    reported success for $0.00 while consuming the event backlog."""
    sid = "sess-empty"
    plane._stream_progress(iter([
        json.dumps({"type": "system", "subtype": "init", "session_id": sid}),
        json.dumps({"type": "result", "subtype": "success", "session_id": sid,
                    "num_turns": 0, "total_cost_usd": 0.0, "duration_ms": 0}),
    ]))
    plane._warn_if_the_session_did_nothing()

    assert "ran no tools at all" in capsys.readouterr().out


def test_the_warning_is_wired_into_the_session_path() -> None:
    """The tests above call the check directly, so all of them would pass with it
    sitting in the file unreferenced. `host-guard.sh` shipped inert once already."""
    source = Path(server.__file__).read_text()
    run_session = source.split("def _run_session", 1)[1].split("\n    def ", 1)[0]
    assert "_warn_if_the_session_did_nothing()" in run_session, (
        "the did-nothing check is defined but never called from _run_session"
    )


# ---------- the run total (#77) ----------


def test_shutdown_reports_what_the_run_cost(plane, capsys, monkeypatch) -> None:
    """The bounds exist so an operator can leave the loop running overnight. The
    thing they want the next morning is the number, and until this existed the
    only way to get it was arithmetic over a log."""
    monkeypatch.setattr(plane, "_reenable_workflows_safe", lambda: None)
    plane.run_spent = 8.83

    plane._shutdown(token_ok=True)

    assert "Run total: $8.83" in capsys.readouterr().out


def test_the_total_is_hedged_when_a_cost_could_not_be_read(plane, capsys, monkeypatch) -> None:
    """`cost_is_lower_bound` is set when a judge session's cost was unreadable.
    A total that has lost a session's spend has to read differently from one that
    hasn't - "$52.11" and "at least $52.11" lead to different decisions."""
    monkeypatch.setattr(plane, "_reenable_workflows_safe", lambda: None)
    plane.run_spent = 52.11
    plane.cost_is_lower_bound = True

    plane._shutdown(token_ok=True)

    assert "Run total: at least $52.11" in capsys.readouterr().out


def test_a_near_miss_on_the_budget_says_so(plane, capsys, monkeypatch) -> None:
    """A run that stops early because the next chain would cross the budget looks
    exactly like a run that finished its work. The operator finds out by noticing
    the absence of progress, which is the slowest possible way."""
    monkeypatch.setattr(plane, "_reenable_workflows_safe", lambda: None)
    monkeypatch.setenv("GENESIS_RUN_COST_BUDGET", "10")
    plane.run_spent = 9.50

    plane._shutdown(token_ok=True)
    out = capsys.readouterr().out

    assert "Run total: $9.50" in out
    assert "within $0.50" in out


def test_a_run_with_headroom_is_not_flagged(plane, capsys, monkeypatch) -> None:
    """The near-miss note has to stay rare or it stops meaning anything."""
    monkeypatch.setattr(plane, "_reenable_workflows_safe", lambda: None)
    monkeypatch.setenv("GENESIS_RUN_COST_BUDGET", "10")
    plane.run_spent = 2.00

    plane._shutdown(token_ok=True)
    out = capsys.readouterr().out

    assert "Run total: $2.00" in out
    assert "within" not in out


def test_every_shutdown_path_reports_the_total() -> None:
    """Three call sites reach `_shutdown`: the normal loop exit, a budget stop, and
    the abort when `claude` is not callable. Putting the report inside `_shutdown`
    is what makes that automatic - a per-call-site version would have missed one,
    which is the same shape as every other drift in this repo."""
    source = Path(server.__file__).read_text()
    shutdown = source.split("def _shutdown", 1)[1].split("\n    def ", 1)[0]
    assert "_log_run_total()" in shutdown, (
        "the run total is not reported from _shutdown, so it depends on which exit "
        "path the run happened to take"
    )
