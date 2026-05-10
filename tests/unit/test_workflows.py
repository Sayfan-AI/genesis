"""Unit tests for workflow enable/disable logic."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterable
from typing import Any

import pytest

from genesis import workflows


class FakeRun:
    """Records subprocess.run calls and replays canned responses."""

    def __init__(self, list_responses: Iterable[list[dict]]) -> None:
        self._list_iter = iter(list_responses)
        self.disable_calls: list[str] = []
        self.enable_calls: list[str] = []

    def __call__(self, cmd, **kwargs):
        if cmd[:4] == ["gh", "workflow", "list", "--all"]:
            payload = next(self._list_iter)
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        if cmd[:3] == ["gh", "workflow", "disable"]:
            self.disable_calls.append(cmd[3])
            return subprocess.CompletedProcess(args=cmd, returncode=0)
        if cmd[:3] == ["gh", "workflow", "enable"]:
            self.enable_calls.append(cmd[3])
            return subprocess.CompletedProcess(args=cmd, returncode=0)
        raise AssertionError(f"unexpected command: {cmd}")


def test_disable_only_active_workflows(monkeypatch) -> None:
    fake = FakeRun(
        [
            [
                {"id": 1, "name": "events", "state": "active"},
                {"id": 2, "name": "scheduled", "state": "active"},
                {"id": 3, "name": "old", "state": "disabled_manually"},
                {"id": 4, "name": "inactive", "state": "disabled_inactivity"},
            ]
        ]
    )
    monkeypatch.setattr(subprocess, "run", fake)

    disabled = workflows.disable_workflows()
    assert disabled == ["events", "scheduled"]
    assert fake.disable_calls == ["1", "2"]


def test_enable_only_manually_disabled_workflows(monkeypatch) -> None:
    fake = FakeRun(
        [
            [
                {"id": 1, "name": "events", "state": "disabled_manually"},
                {"id": 2, "name": "scheduled", "state": "active"},
                {"id": 3, "name": "inactive", "state": "disabled_inactivity"},
            ]
        ]
    )
    monkeypatch.setattr(subprocess, "run", fake)

    enabled = workflows.enable_workflows()
    assert enabled == ["events"]
    assert fake.enable_calls == ["1"]


def test_disable_with_no_active_workflows_is_noop(monkeypatch) -> None:
    fake = FakeRun(
        [
            [
                {"id": 1, "name": "old", "state": "disabled_manually"},
            ]
        ]
    )
    monkeypatch.setattr(subprocess, "run", fake)
    assert workflows.disable_workflows() == []
    assert fake.disable_calls == []


def test_enable_with_no_disabled_workflows_is_noop(monkeypatch) -> None:
    fake = FakeRun(
        [
            [
                {"id": 1, "name": "events", "state": "active"},
            ]
        ]
    )
    monkeypatch.setattr(subprocess, "run", fake)
    assert workflows.enable_workflows() == []
    assert fake.enable_calls == []


def test_list_workflows_parses_json(monkeypatch) -> None:
    fake = FakeRun([[{"id": 1, "name": "events", "state": "active"}]])
    monkeypatch.setattr(subprocess, "run", fake)
    result = workflows.list_workflows()
    assert result == [{"id": 1, "name": "events", "state": "active"}]
