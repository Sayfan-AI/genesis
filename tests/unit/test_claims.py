"""Tests for background-task failure detection and stranded-claim recovery."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock

import pytest

from genesis import claims, engines, server


@pytest.fixture(autouse=True)
def isolate_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(server, "loki_push", lambda *a, **k: True)
    yield tmp_path


def _fake_gh(responses: list[tuple[int, object]]):
    queue = list(responses)

    def _call(_args: list[str], _token: str | None) -> tuple[int, str]:
        code, payload = queue.pop(0)
        return code, payload if isinstance(payload, str) else json.dumps(payload)

    return _call


# ---------- claims.unbacked_claimed_issues ----------


def test_unbacked_claim_is_flagged_when_no_open_pr_matches() -> None:
    issues_payload = [
        {
            "number": 103,
            "comments": [
                {"body": "Claimed.\n<!-- genesis-claim session=serve-abc123 -->"}
            ],
        }
    ]
    prs_payload = [
        {
            "number": 99,
            "title": "unrelated fix",
            "body": "Closes #80",
            "headRefName": "issue-80-fix",
            "createdAt": "2026-11-20T00:00:00Z",
            "updatedAt": "2026-11-20T00:05:00Z",
        }
    ]
    gh = _fake_gh([(0, issues_payload), (0, prs_payload)])

    assert (
        claims.unbacked_claimed_issues("o/r", "serve-abc123", 1_900_000_000.0, "tok", gh=gh)
        == [103]
    )


def test_claim_backed_by_matching_pr_body_or_branch_is_kept() -> None:
    issues_payload = [
        {
            "number": 103,
            "comments": [
                {"body": "Claimed.\n<!-- genesis-claim session=serve-abc123 -->"}
            ],
        },
        {
            "number": 104,
            "comments": [
                {"body": "Claimed.\n<!-- genesis-claim session=serve-abc123 -->"}
            ],
        },
    ]
    prs_payload = [
        {
            "number": 120,
            "title": "wire session kinds",
            "body": "Closes #103",
            "headRefName": "session-kinds",
            "createdAt": "2026-11-20T00:00:00Z",
            "updatedAt": "2026-11-20T00:00:00Z",
        },
        {
            "number": 121,
            "title": "follow up",
            "body": "",
            "headRefName": "fix/issue-104-cleanup",
            "createdAt": "2026-11-20T00:00:00Z",
            "updatedAt": "2026-11-20T00:00:00Z",
        },
    ]
    gh = _fake_gh([(0, issues_payload), (0, prs_payload)])

    assert (
        claims.unbacked_claimed_issues("o/r", "serve-abc123", 1_900_000_000.0, "tok", gh=gh)
        == []
    )


def test_claim_backed_by_pr_opened_during_chain_is_kept() -> None:
    """Workers don't always remember `Closes #N`, so any open PR updated since
    this chain began counts as backing work."""
    issues_payload = [
        {
            "number": 103,
            "comments": [
                {"body": "Claimed.\n<!-- genesis-claim session=serve-abc123 -->"}
            ],
        }
    ]
    prs_payload = [
        {
            "number": 122,
            "title": "implement session kinds",
            "body": "Implements the feature.",
            "headRefName": "feat/session-kinds",
            "createdAt": "2026-11-22T01:10:00Z",
            "updatedAt": "2026-11-22T01:10:00Z",
        }
    ]
    chain_start = claims._parse_iso("2026-11-22T01:02:00Z")
    assert chain_start is not None
    gh = _fake_gh([(0, issues_payload), (0, prs_payload)])

    assert claims.unbacked_claimed_issues("o/r", "serve-abc123", chain_start, "tok", gh=gh) == []


def test_claim_owned_by_another_session_or_when_gh_fails_is_untouched() -> None:
    issues_payload = [
        {
            "number": 103,
            "comments": [
                {"body": "Claimed.\n<!-- genesis-claim session=serve-abc123 -->"},
                {"body": "Claimed.\n<!-- genesis-claim session=serve-other999 -->"},
            ],
        }
    ]
    gh_superseded = _fake_gh([(0, issues_payload)])
    assert (
        claims.unbacked_claimed_issues(
            "o/r", "serve-abc123", 1_700_000_000.0, "tok", gh=gh_superseded
        )
        == []
    )

    # Network / API failure fails open rather than releasing a legitimate claim.
    gh_failing = _fake_gh([(0, issues_payload), (1, "rate limit")])
    assert (
        claims.unbacked_claimed_issues(
            "o/r", "serve-other999", 1_700_000_000.0, "tok", gh=gh_failing
        )
        == []
    )


# ---------- LocalControlPlane integration ----------


def test_session_env_propagates_engine_session_env(monkeypatch) -> None:
    monkeypatch.setattr(server, "mint_installation_token", lambda _repo, _env=None: "tok")

    claude_plane = server.LocalControlPlane(repo="o/r", engine=engines.ClaudeEngine())
    claude_plane.session_token = "serve-1"
    c_env = claude_plane._session_env()
    assert c_env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
    assert c_env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] == "0"

    codex_plane = server.LocalControlPlane(repo="o/r", engine=engines.CodexEngine())
    codex_plane.session_token = "serve-2"
    x_env = codex_plane._session_env()
    assert "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS" not in x_env


def test_stderr_background_kill_overrides_false_success_without_double_charging(monkeypatch) -> None:
    """Reproduces the exact sweatless issue #103 transcript: stdout emits
    `subtype=success` ($0.45), then stderr announces that a background task hit
    the ceiling and was stopped. The plane must override the outcome to an
    error, keep spend at $0.45, release the claim, and queue a follow-up pass."""
    plane = server.LocalControlPlane(repo="o/r", engine=engines.ClaudeEngine())
    monkeypatch.setattr(plane, "run_pre_session_steps", lambda: None)
    monkeypatch.setattr(plane, "_session_env", lambda: {})
    monkeypatch.setattr(server, "session_work_marker", lambda: "unchanged")

    released: list[str] = []
    monkeypatch.setattr(plane, "release_claims", released.append)

    stdout_lines = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "s-bg"}) + "\n",
        json.dumps({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "bash .genesis/scripts/issues.sh next --claim"}}
                ]
            },
        }) + "\n",
        json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": "s-bg",
            "num_turns": 1,
            "total_cost_usd": 0.45,
            "duration_ms": 5200,
        }) + "\n",
    ]
    stderr_lines = [
        "Background tasks still running 10m after the last turn (subagent: Implement Task 2); stopping them.\n"
    ]

    def fake_popen(cmd, **kwargs):
        proc = MagicMock()
        proc.pid = 4321
        proc.wait.return_value = 0
        proc.stdout = iter(stdout_lines)
        proc.stderr = iter(stderr_lines)
        return proc

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    plane.run_orchestrator(None, prompt="work the backlog")

    assert plane.last_result_subtype == "error_background_tasks_stopped"
    assert plane.run_spent == pytest.approx(0.45)
    assert released == ["the session exited while background work was still running"]
    assert plane.pending_followup is True
    assert any("background work was still running" in r for r in plane.followup_reasons)


def test_clean_session_leaving_unbacked_claim_releases_and_queues_followup(
    tmp_path, monkeypatch
) -> None:
    """Option 2A: when a session ran `issues.sh next --claim`, reported
    `success`, and left its claimed issue `in-progress` with no open PR, the
    plane releases the claim immediately and queues a follow-up pass."""
    issues_sh = tmp_path / ".genesis" / "scripts" / "issues.sh"
    issues_sh.parent.mkdir(parents=True, exist_ok=True)
    issues_sh.write_text("#!/usr/bin/env bash\nexit 0\n")

    plane = server.LocalControlPlane(repo="o/r", engine=engines.ClaudeEngine())
    monkeypatch.setattr(plane, "run_pre_session_steps", lambda: None)
    monkeypatch.setattr(plane, "_session_env", lambda: {})
    monkeypatch.setattr(server, "session_work_marker", lambda: "unchanged")
    monkeypatch.setattr(plane, "_chain_left_unbacked_claims", lambda _ts: [103])

    released: list[str] = []
    monkeypatch.setattr(plane, "release_claims", released.append)

    stdout_lines = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "s-claim"}) + "\n",
        json.dumps({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "bash .genesis/scripts/issues.sh next --claim"}}
                ]
            },
        }) + "\n",
        json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": "s-claim",
            "num_turns": 2,
            "total_cost_usd": 0.25,
            "duration_ms": 3000,
        }) + "\n",
    ]

    def fake_popen(cmd, **kwargs):
        proc = MagicMock()
        proc.pid = 4322
        proc.wait.return_value = 0
        proc.stdout = iter(stdout_lines)
        proc.stderr = iter([])
        return proc

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    plane.run_orchestrator(None, prompt="work the backlog")

    assert released == ["the session ended without opening a pull request or closing this issue"]
    assert plane.pending_followup is True
    assert any("ended without opening a pull request" in r for r in plane.followup_reasons)


def test_clean_session_with_backed_claim_keeps_its_claim(tmp_path, monkeypatch) -> None:
    issues_sh = tmp_path / ".genesis" / "scripts" / "issues.sh"
    issues_sh.parent.mkdir(parents=True, exist_ok=True)
    issues_sh.write_text("#!/usr/bin/env bash\nexit 0\n")

    plane = server.LocalControlPlane(repo="o/r", engine=engines.ClaudeEngine())
    monkeypatch.setattr(plane, "run_pre_session_steps", lambda: None)
    monkeypatch.setattr(plane, "_session_env", lambda: {})
    monkeypatch.setattr(server, "session_work_marker", lambda: "unchanged")
    monkeypatch.setattr(plane, "_chain_left_unbacked_claims", lambda _ts: [])

    released: list[str] = []
    monkeypatch.setattr(plane, "release_claims", released.append)

    stdout_lines = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "s-ok"}) + "\n",
        json.dumps({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "bash .genesis/scripts/issues.sh claim 103"}}
                ]
            },
        }) + "\n",
        json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": "s-ok",
            "num_turns": 5,
            "total_cost_usd": 1.10,
            "duration_ms": 20000,
        }) + "\n",
    ]

    def fake_popen(cmd, **kwargs):
        proc = MagicMock()
        proc.pid = 4323
        proc.wait.return_value = 0
        proc.stdout = iter(stdout_lines)
        proc.stderr = iter([])
        return proc

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    plane.run_orchestrator(None, prompt="work the backlog")

    assert released == []
    assert plane.pending_followup is False


def test_stale_claim_sweep_queues_followup_only_in_poll_loop(monkeypatch) -> None:
    plane = server.LocalControlPlane(repo="o/r")
    monkeypatch.setattr(plane, "_run_issues_script", lambda _args: ["released #103 (claimed 125m ago)"])

    plane.sweep_stale_claims(force=True)
    assert plane.pending_followup is False

    plane.last_claim_sweep = 0.0
    plane.sweep_stale_claims(force=False)
    assert plane.pending_followup is True
    assert plane.followup_reasons == ["the stale-claim sweep released #103 (claimed 125m ago)"]
