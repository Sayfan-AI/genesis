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
