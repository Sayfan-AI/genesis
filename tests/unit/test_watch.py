"""Hermetic unit tests for `genesis watch` stall detection and aggregate alerting."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from genesis import plane_state, watch
from genesis.fleet import FleetRepo


def _iso(epoch: float) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_daemon_down_vs_budget_stopped_vs_manual_stop(tmp_path: Path) -> None:
    repo_dir = tmp_path / "sweatless"
    (repo_dir / ".genesis").mkdir(parents=True)
    (repo_dir / ".genesis" / ".disabled-by-genesis").write_text("genesis-merge.yml\n")
    entry = FleetRepo(name="sweatless", path=repo_dir, repo="sagrin/sweatless")

    def empty_gh(args, token):
        return 0, "[]"

    # 1. Unexpectedly dead with disabled workflows left behind.
    findings = watch.check_repo(
        entry,
        now=10_000.0,
        gh=empty_gh,
        env_loader=lambda _p: {},
        token_minter=lambda _r, _e: None,
    )
    assert len(findings) == 1
    assert findings[0].kind == "daemon_down"
    assert "workflows still disabled" in findings[0].summary

    # 2. Budget stop -> actionable `stopped:run_budget`.
    plane_state.write_stopped("run_budget", detail="$150 >= $150", base_dir=repo_dir)
    findings = watch.check_repo(
        entry,
        now=10_000.0,
        gh=empty_gh,
        env_loader=lambda _p: {},
        token_minter=lambda _r, _e: None,
    )
    assert len(findings) == 1
    assert findings[0].kind == "stopped:run_budget"

    # 3. Explicit operator `manual_stop` -> quiet (0 findings).
    plane_state.write_stopped("manual_stop", base_dir=repo_dir)
    findings = watch.check_repo(
        entry,
        now=10_000.0,
        gh=empty_gh,
        env_loader=lambda _p: {},
        token_minter=lambda _r, _e: None,
    )
    assert findings == []


def test_phantom_in_progress_auto_releases_and_nudges_idle_plane(tmp_path: Path) -> None:
    repo_dir = tmp_path / "sweatless"
    scripts_dir = repo_dir / ".genesis" / "scripts"
    scripts_dir.mkdir(parents=True)
    issues_sh = scripts_dir / "issues.sh"
    issues_sh.write_text("#!/usr/bin/env bash\necho 'released #103'\n")

    # Live idle plane holding `.orchestrator.lock`.
    (repo_dir / ".genesis" / ".orchestrator.lock").write_text(str(os.getpid()))
    plane_state.write_plane_state(
        repo="sagrin/sweatless",
        pid=os.getpid(),
        engine="claude",
        agent=".claude/agents/orchestrator.md",
        state="idle",
        started_at=9_900.0,
        last_poll_at=9_990.0,
        base_dir=repo_dir,
    )
    entry = FleetRepo(name="sweatless", path=repo_dir, repo="sagrin/sweatless")

    now = 10_000.0
    claim_body = "Claimed\n<!-- genesis-claim session=serve-deadbeef -->"

    def fake_gh(args, token):
        if args[:2] == ["issue", "list"] and "in-progress" in args:
            return 0, json.dumps(
                [
                    {
                        "number": 103,
                        "title": "Task 2",
                        "updatedAt": _iso(now - 900),
                        "comments": [{"body": claim_body, "createdAt": _iso(now - 900)}],
                    }
                ]
            )
        if args[:2] == ["run", "list"]:
            return 0, json.dumps([{"status": "completed"}])
        if args[:2] == ["pr", "list"]:
            return 0, "[]"
        return 0, "[]"

    release_calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        release_calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "released #103\n", "")

    findings = watch.check_repo(
        entry,
        now=now,
        gh=fake_gh,
        run=fake_run,
        env_loader=lambda _p: {},
        token_minter=lambda _r, _e: "ghs_app",
    )
    assert len(findings) == 1
    assert findings[0].kind == "phantom_in_progress"
    assert findings[0].remediated is True
    assert any("serve-deadbeef" in c for c in release_calls)
    assert plane_state.consume_nudge(repo_dir) is not None


def test_phantom_in_progress_skips_when_pr_or_actions_run_exists(tmp_path: Path) -> None:
    repo_dir = tmp_path / "sweatless"
    (repo_dir / ".genesis").mkdir(parents=True)
    (repo_dir / ".genesis" / ".orchestrator.lock").write_text(str(os.getpid()))
    plane_state.write_plane_state(
        repo="sagrin/sweatless",
        pid=os.getpid(),
        engine="claude",
        agent=".claude/agents/orchestrator.md",
        state="idle",
        started_at=9_950.0,
        base_dir=repo_dir,
    )
    entry = FleetRepo(name="sweatless", path=repo_dir, repo="sagrin/sweatless")
    now = 10_000.0
    issue_payload = [
        {
            "number": 11,
            "title": "Task 11",
            "updatedAt": _iso(now - 1200),
            "comments": [
                {
                    "body": "<!-- genesis-claim session=serve-111 -->",
                    "createdAt": _iso(now - 1200),
                }
            ],
        }
    ]

    # Case A: open PR backs issue #11 -> not phantom.
    def gh_with_pr(args, token):
        if args[:2] == ["issue", "list"]:
            return 0, json.dumps(issue_payload)
        if args[:2] == ["run", "list"]:
            return 0, "[]"
        if args[:2] == ["pr", "list"]:
            return 0, json.dumps(
                [
                    {
                        "number": 22,
                        "title": "Implement locations",
                        "body": "Closes #11",
                        "headRefName": "feat/locations",
                        "createdAt": _iso(now - 600),
                        "updatedAt": _iso(now - 300),
                    }
                ]
            )
        return 0, "[]"

    assert (
        watch.check_repo(
            entry,
            now=now,
            gh=gh_with_pr,
            env_loader=lambda _p: {},
            token_minter=lambda _r, _e: None,
        )
        == []
    )

    # Case B: GitHub Actions run is `in_progress` -> not phantom.
    def gh_with_active_run(args, token):
        if args[:2] == ["issue", "list"]:
            return 0, json.dumps(issue_payload)
        if args[:2] == ["run", "list"]:
            return 0, json.dumps([{"status": "in_progress"}])
        return 0, "[]"

    assert (
        watch.check_repo(
            entry,
            now=now,
            gh=gh_with_active_run,
            env_loader=lambda _p: {},
            token_minter=lambda _r, _e: None,
        )
        == []
    )


def test_hung_session_detected_when_turn_progress_stalls(tmp_path: Path) -> None:
    repo_dir = tmp_path / "sweatless"
    (repo_dir / ".genesis").mkdir(parents=True)
    (repo_dir / ".genesis" / ".orchestrator.lock").write_text(str(os.getpid()))
    now = 10_000.0
    plane_state.write_plane_state(
        repo="sagrin/sweatless",
        pid=os.getpid(),
        engine="claude",
        agent=".claude/agents/orchestrator.md",
        state="running_session",
        started_at=now - 3000,
        child_pid=os.getpid(),
        session_started_at=now - 1800,
        last_turn_at=now - 1500,  # 25 minutes ago (> 20m default)
        turns=3,
        base_dir=repo_dir,
    )
    entry = FleetRepo(name="sweatless", path=repo_dir, repo="sagrin/sweatless")
    findings = watch.check_repo(
        entry,
        now=now,
        gh=lambda _a, _t: (0, "[]"),
        env_loader=lambda _p: {},
        token_minter=lambda _r, _e: None,
    )
    assert len(findings) == 1
    assert findings[0].kind == "hung_session"
    assert "25m" in findings[0].summary


def test_starving_work_nudges_silently_first_then_alerts_if_still_starving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_dir = tmp_path / "genesis-serve"
    (repo_dir / ".genesis").mkdir(parents=True)
    (repo_dir / ".genesis" / ".orchestrator.lock").write_text(str(os.getpid()))
    now = 10_000.0
    plane_state.write_plane_state(
        repo="Sayfan-AI/genesis",
        pid=os.getpid(),
        engine="claude",
        agent=".claude/agents/evolver.md",
        state="idle",
        started_at=now - 3600,
        last_poll_at=now - 20,
        base_dir=repo_dir,
    )
    monkeypatch.setattr(watch.triggers, "open_human_gates", lambda repo, tok: [])
    entry = FleetRepo(
        name="genesis-serve",
        path=repo_dir,
        repo="Sayfan-AI/genesis",
        agent=".claude/agents/evolver.md",
    )

    def gh_starving(args, token):
        if "in-progress" in args:
            return 0, "[]"
        if args[:2] == ["pr", "list"]:
            return 0, "[]"
        if args[:2] == ["issue", "list"]:
            return 0, json.dumps(
                [
                    {
                        "number": 158,
                        "labels": [{"name": "needs:evolver"}],
                        "updatedAt": _iso(now - 3600),
                    }
                ]
            )
        return 0, "[]"

    w_state: dict[str, object] = {}
    # First check: writes `.nudge`, emits 0 alert findings.
    first = watch.check_repo(
        entry,
        now=now,
        watch_state=w_state,
        gh=gh_starving,
        env_loader=lambda _p: {},
        token_minter=lambda _r, _e: None,
    )
    assert first == []
    assert plane_state.consume_nudge(repo_dir) is not None

    # Second check 20m later while still idle and unpicked: emits `starving_work`.
    second = watch.check_repo(
        entry,
        now=now + 1200,
        watch_state=w_state,
        gh=gh_starving,
        env_loader=lambda _p: {},
        token_minter=lambda _r, _e: None,
    )
    assert len(second) == 1
    assert second[0].kind == "starving_work"
    assert "issue #158" in second[0].summary


def test_dispatch_aggregate_alert_sends_single_consolidated_notification_and_throttles() -> None:
    sent: list[tuple[str, str]] = []
    state: dict[str, object] = {}
    f1 = watch.Finding(repo="sweatless", kind="phantom_in_progress", summary="issue #103 released")
    f2 = watch.Finding(repo="genesis-serve", kind="daemon_down", summary="genesis serve isn't running")

    # 1. Two unhealthy repos -> ONE single aggregate alert.
    assert (
        watch.dispatch_aggregate_alert(
            [f1, f2], state, now=1_000.0, notifier=lambda t, m: sent.append((t, m))
        )
        is True
    )
    assert len(sent) == 1
    assert sent[0][0] == "Genesis Watch (2 repos)"
    assert "sweatless:" in sent[0][1] and "genesis-serve:" in sent[0][1]

    # 2. Same findings 3 minutes later -> throttled (no duplicate notification).
    assert (
        watch.dispatch_aggregate_alert(
            [f1, f2], state, now=1_180.0, notifier=lambda t, m: sent.append((t, m))
        )
        is False
    )
    assert len(sent) == 1

    # 3. Third repo fails 4 minutes in -> escalates immediately!
    f3 = watch.Finding(repo="alpha", kind="stopped:run_budget", summary="plane stopped on run_budget")
    assert (
        watch.dispatch_aggregate_alert(
            [f1, f2, f3], state, now=1_240.0, notifier=lambda t, m: sent.append((t, m))
        )
        is True
    )
    assert len(sent) == 2
    assert sent[1][0] == "Genesis Watch (3 repos)"

    # 4. All clear -> resets active_keys silently; subsequent recurrence alerts fresh.
    assert (
        watch.dispatch_aggregate_alert(
            [], state, now=1_300.0, notifier=lambda t, m: sent.append((t, m))
        )
        is False
    )
    assert state["active_keys"] == []
    assert (
        watch.dispatch_aggregate_alert(
            [f1], state, now=1_360.0, notifier=lambda t, m: sent.append((t, m))
        )
        is True
    )
    assert len(sent) == 3
    assert sent[2][0] == "Genesis Watch (1 repo)"


def test_load_loki_env_never_loads_anthropic_api_key(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ANTHROPIC_API_KEY=sk-secret\n"
        "GENESIS_LOKI_URL=https://logs.example\n"
        "GENESIS_LOKI_USER=123\n"
        "GENESIS_LOKI_TOKEN=glc_tok\n"
    )
    loaded = watch.load_loki_env({}, env_file)
    assert loaded == {
        "GENESIS_LOKI_URL": "https://logs.example",
        "GENESIS_LOKI_USER": "123",
        "GENESIS_LOKI_TOKEN": "glc_tok",
    }
    assert "ANTHROPIC_API_KEY" not in loaded
