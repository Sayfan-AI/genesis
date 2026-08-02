"""Tests for the local trigger layer.

Each seeded workflow is a (condition, agent) pair. Local mode disables the
workflows, so if `serve` doesn't reproduce the pairs, the dev system silently
loses capabilities by being driven from a laptop instead of from CI.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from genesis import triggers


HOUR = 3600


def test_a_fresh_checkout_runs_its_schedule_immediately() -> None:
    """No recorded run means "long ago", not "wait a full interval". Otherwise a
    dev system started at noon does nothing at all until six."""
    assert triggers.scheduled_due({}, now=1000.0) is not None
    assert triggers.evolver_due({}, now=1000.0) is not None


def test_scheduled_orchestrator_respects_its_interval() -> None:
    now = 100 * HOUR
    assert triggers.scheduled_due({"scheduled": now - 5 * HOUR}, now) is None
    due = triggers.scheduled_due({"scheduled": now - 7 * HOUR}, now)
    assert due and due.agent.endswith("orchestrator.md")
    assert "scheduled run" in due.prompt


def test_evolver_is_daily_and_skipped_when_absent() -> None:
    now = 100 * HOUR
    assert triggers.evolver_due({"evolver": now - 20 * HOUR}, now) is None
    assert triggers.evolver_due({"evolver": now - 30 * HOUR}, now) is not None
    # A dev system without an evolver shouldn't burn a session on a missing file.
    assert triggers.evolver_due({}, now, agent_exists=False) is None


def test_corrupt_state_does_not_wedge_the_schedule() -> None:
    for bad in ({"scheduled": "yesterday"}, {"scheduled": None}, {}):
        assert triggers.scheduled_due(bad, now=1000.0) is not None


def test_ci_failure_prompt_names_the_run() -> None:
    runs = [{"name": "CI", "headBranch": "feat/x", "url": "https://example/run/9", "createdAt": "2026-08-02T01:00:00Z"}]
    due = triggers.ci_failure_due(runs)
    assert due and due.name == "ci-failure"
    assert "CI" in due.prompt and "feat/x" in due.prompt and "run/9" in due.prompt
    assert "heroic" in due.prompt  # triage, don't attempt an inline rewrite


def test_no_failures_means_no_trigger() -> None:
    assert triggers.ci_failure_due([]) is None


def _gh_returns(payload) -> object:
    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

    return run


def test_failed_runs_ignores_genesis_own_workflows(monkeypatch) -> None:
    """Genesis workflows escalate their own failures through escalate.sh.
    Re-triaging them here would double-report a failure that already has an issue."""
    monkeypatch.setattr(
        triggers.subprocess,
        "run",
        _gh_returns([
            {"name": "Genesis Evolver", "createdAt": "2026-08-02T02:00:00Z"},
            {"name": "CI", "createdAt": "2026-08-02T01:00:00Z"},
        ]),
    )
    runs = triggers.failed_runs("o/r", since_iso=None)
    assert [r["name"] for r in runs] == ["CI"]


def test_failed_runs_only_reports_what_is_new(monkeypatch) -> None:
    monkeypatch.setattr(
        triggers.subprocess,
        "run",
        _gh_returns([
            {"name": "CI", "createdAt": "2026-08-02T01:00:00Z"},
            {"name": "E2E (kind)", "createdAt": "2026-08-02T03:00:00Z"},
        ]),
    )
    runs = triggers.failed_runs("o/r", since_iso="2026-08-02T02:00:00Z")
    assert [r["name"] for r in runs] == ["E2E (kind)"]


def test_failed_runs_survives_a_broken_gh(monkeypatch) -> None:
    def boom(*a, **k):
        raise OSError("gh missing")

    monkeypatch.setattr(triggers.subprocess, "run", boom)
    assert triggers.failed_runs("o/r", None) == []


def test_state_roundtrip_and_unreadable_state(tmp_path) -> None:
    p = tmp_path / ".trigger-state"
    triggers.save_state({"scheduled": 123.0}, p)
    assert triggers.load_state(p)["scheduled"] == 123.0
    p.write_text("{ not json")
    assert triggers.load_state(p) == {}
