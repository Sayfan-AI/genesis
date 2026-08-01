"""Unit tests for workflow enable/disable logic."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterable

import pytest

from genesis import workflows


@pytest.fixture(autouse=True)
def isolate_cwd(tmp_path, monkeypatch):
    """Each test runs in its own tmp dir so .genesis/ artifacts don't bleed."""
    monkeypatch.chdir(tmp_path)
    yield tmp_path


class FakeRun:
    """Records subprocess.run calls and replays canned `gh workflow list` responses."""

    def __init__(self, list_responses: Iterable[list[dict]]) -> None:
        self._list_iter = iter(list_responses)
        self.disable_calls: list[list[str]] = []
        self.enable_calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        if cmd[:4] == ["gh", "workflow", "list", "--all"]:
            payload = next(self._list_iter)
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        if cmd[:3] == ["gh", "workflow", "disable"]:
            self.disable_calls.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0)
        if cmd[:3] == ["gh", "workflow", "enable"]:
            self.enable_calls.append(cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0)
        raise AssertionError(f"unexpected command: {cmd}")


def test_disable_only_active_workflows(monkeypatch) -> None:
    fake = FakeRun(
        [
            [
                {"id": 1, "name": "events", "state": "active", "path": ".github/workflows/genesis-events.yml"},
                {"id": 2, "name": "scheduled", "state": "active", "path": ".github/workflows/genesis-scheduled.yml"},
                {"id": 3, "name": "old", "state": "disabled_manually", "path": ".github/workflows/genesis-old.yml"},
                {"id": 4, "name": "inactive", "state": "disabled_inactivity", "path": ".github/workflows/genesis-inactive.yml"},
            ]
        ]
    )
    monkeypatch.setattr(subprocess, "run", fake)

    disabled = workflows.disable_workflows()
    assert disabled == ["events", "scheduled"]
    assert [c[3] for c in fake.disable_calls] == ["1", "2"]


def test_disable_persists_tracking_file(monkeypatch) -> None:
    fake = FakeRun(
        [[{"id": 1, "name": "events", "state": "active", "path": ".github/workflows/genesis-events.yml"}]]
    )
    monkeypatch.setattr(subprocess, "run", fake)

    workflows.disable_workflows()
    assert workflows.DISABLED_LIST_PATH.exists()
    tracked = json.loads(workflows.DISABLED_LIST_PATH.read_text())
    assert tracked == [{"id": 1, "name": "events"}]


def test_disable_persists_incrementally_on_partial_failure(monkeypatch) -> None:
    """If the 2nd disable call fails, the 1st must already be on disk for recovery."""

    list_payload = [
        {"id": 1, "name": "events", "state": "active", "path": ".github/workflows/genesis-events.yml"},
        {"id": 2, "name": "scheduled", "state": "active", "path": ".github/workflows/genesis-scheduled.yml"},
    ]

    def fake_run(cmd, **kwargs):
        if cmd[:4] == ["gh", "workflow", "list", "--all"]:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=json.dumps(list_payload), stderr=""
            )
        if cmd[:4] == ["gh", "workflow", "disable", "1"]:
            return subprocess.CompletedProcess(args=cmd, returncode=0)
        if cmd[:4] == ["gh", "workflow", "disable", "2"]:
            raise subprocess.CalledProcessError(returncode=1, cmd=cmd)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(subprocess.CalledProcessError):
        workflows.disable_workflows()
    # Workflow 1 was disabled before the failure — must be tracked on disk
    tracked = json.loads(workflows.DISABLED_LIST_PATH.read_text())
    assert tracked == [{"id": 1, "name": "events"}]


def test_disable_merges_with_existing_tracked_state(monkeypatch) -> None:
    """A second disable_workflows call must not erase prior tracked disables."""
    workflows._persist_disabled([{"id": 99, "name": "old-from-prior-run"}])
    fake = FakeRun(
        [[{"id": 1, "name": "events", "state": "active", "path": ".github/workflows/genesis-events.yml"}]]
    )
    monkeypatch.setattr(subprocess, "run", fake)

    workflows.disable_workflows()
    tracked = json.loads(workflows.DISABLED_LIST_PATH.read_text())
    assert tracked == [
        {"id": 99, "name": "old-from-prior-run"},
        {"id": 1, "name": "events"},
    ]


def test_disable_no_active_does_not_create_tracking_file(monkeypatch) -> None:
    fake = FakeRun(
        [[{"id": 1, "name": "old", "state": "disabled_manually", "path": ".github/workflows/genesis-old.yml"}]]
    )
    monkeypatch.setattr(subprocess, "run", fake)
    assert workflows.disable_workflows() == []
    assert not workflows.DISABLED_LIST_PATH.exists()


def test_enable_targeted_only_restores_tracked_workflows(monkeypatch) -> None:
    """If genesis tracked which workflows it disabled, only re-enable those.

    Workflows the user had disabled before `genesis serve` started must stay
    disabled.
    """
    workflows._persist_disabled([{"id": 1, "name": "events"}])
    fake = FakeRun(
        [
            [
                # genesis-disabled, should be re-enabled
                {"id": 1, "name": "events", "state": "disabled_manually", "path": ".github/workflows/genesis-events.yml"},
                # user-disabled before genesis ran, must NOT be re-enabled
                {"id": 99, "name": "user-paused", "state": "disabled_manually", "path": ".github/workflows/genesis-user-paused.yml"},
                {"id": 2, "name": "active", "state": "active", "path": ".github/workflows/genesis-active.yml"},
            ]
        ]
    )
    monkeypatch.setattr(subprocess, "run", fake)

    enabled = workflows.enable_workflows()
    assert enabled == ["events"]
    assert [c[3] for c in fake.enable_calls] == ["1"]
    assert not workflows.DISABLED_LIST_PATH.exists()  # cleared after enable


def test_enable_recovery_mode_when_no_tracking_file(monkeypatch) -> None:
    """No tracking file → recovery hatch: enable all disabled_manually workflows."""
    fake = FakeRun(
        [
            [
                {"id": 1, "name": "events", "state": "disabled_manually", "path": ".github/workflows/genesis-events.yml"},
                {"id": 2, "name": "scheduled", "state": "active", "path": ".github/workflows/genesis-scheduled.yml"},
                {"id": 3, "name": "inactive", "state": "disabled_inactivity", "path": ".github/workflows/genesis-inactive.yml"},
            ]
        ]
    )
    monkeypatch.setattr(subprocess, "run", fake)

    enabled = workflows.enable_workflows()
    assert enabled == ["events"]
    assert [c[3] for c in fake.enable_calls] == ["1"]


def test_enable_with_no_disabled_workflows_is_noop(monkeypatch) -> None:
    fake = FakeRun([[{"id": 1, "name": "events", "state": "active", "path": ".github/workflows/genesis-events.yml"}]])
    monkeypatch.setattr(subprocess, "run", fake)
    assert workflows.enable_workflows() == []
    assert fake.enable_calls == []


def test_list_workflows_parses_json(monkeypatch) -> None:
    fake = FakeRun([[{"id": 1, "name": "events", "state": "active", "path": ".github/workflows/genesis-events.yml"}]])
    monkeypatch.setattr(subprocess, "run", fake)
    result = workflows.list_workflows()
    assert result == [{"id": 1, "name": "events", "state": "active", "path": ".github/workflows/genesis-events.yml"}]


# ---------- --repo propagation ----------


def test_disable_threads_repo_arg_to_gh(monkeypatch) -> None:
    fake = FakeRun([[{"id": 1, "name": "events", "state": "active", "path": ".github/workflows/genesis-events.yml"}]])
    monkeypatch.setattr(subprocess, "run", fake)
    workflows.disable_workflows(repo="alice/foo")
    # disable cmd should carry --repo alice/foo
    assert fake.disable_calls == [
        ["gh", "workflow", "disable", "1", "--repo", "alice/foo"]
    ]


def test_enable_threads_repo_arg_to_gh(monkeypatch) -> None:
    fake = FakeRun(
        [[{"id": 1, "name": "events", "state": "disabled_manually", "path": ".github/workflows/genesis-events.yml"}]]
    )
    monkeypatch.setattr(subprocess, "run", fake)
    workflows.enable_workflows(repo="alice/foo")
    assert fake.enable_calls == [
        ["gh", "workflow", "enable", "1", "--repo", "alice/foo"]
    ]


def test_list_threads_repo_arg_to_gh(monkeypatch) -> None:
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    workflows.list_workflows(repo="alice/foo")
    assert captured[0][-2:] == ["--repo", "alice/foo"]


def test_repo_arg_omitted_when_none(monkeypatch) -> None:
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    workflows.list_workflows()
    assert "--repo" not in captured[0]


# ---------- genesis-only filtering ----------


def test_disable_leaves_non_genesis_workflows_alone(monkeypatch) -> None:
    """CI and other gates must keep running while the local plane drives.

    Disabling them was actively harmful: the merge agent requires "all checks
    passing", which a disabled workflow can never report.
    """
    fake = FakeRun(
        [
            [
                {
                    "id": 1,
                    "name": "Genesis Orchestrator (Events)",
                    "state": "active",
                    "path": ".github/workflows/genesis-events.yml",
                },
                {
                    "id": 2,
                    "name": "CI",
                    "state": "active",
                    "path": ".github/workflows/ci.yml",
                },
                {
                    "id": 3,
                    "name": "E2E (kind)",
                    "state": "active",
                    "path": ".github/workflows/e2e.yml",
                },
            ]
        ]
    )
    monkeypatch.setattr(subprocess, "run", fake)

    disabled = workflows.disable_workflows()
    assert disabled == ["Genesis Orchestrator (Events)"]
    assert [c[3] for c in fake.disable_calls] == ["1"]


def test_disable_all_workflows_when_genesis_only_is_false(monkeypatch) -> None:
    fake = FakeRun(
        [
            [
                {
                    "id": 1,
                    "name": "Genesis Evolver",
                    "state": "active",
                    "path": ".github/workflows/genesis-evolver.yml",
                },
                {
                    "id": 2,
                    "name": "CI",
                    "state": "active",
                    "path": ".github/workflows/ci.yml",
                },
            ]
        ]
    )
    monkeypatch.setattr(subprocess, "run", fake)

    assert workflows.disable_workflows(genesis_only=False) == ["Genesis Evolver", "CI"]


def test_is_genesis_workflow_falls_back_to_name_without_path() -> None:
    """Older `gh` output has no `path`; the display name is the only signal."""
    assert workflows.is_genesis_workflow({"name": "Genesis Evolver"})
    assert not workflows.is_genesis_workflow({"name": "CI"})
    # An explicit path always wins over the name.
    assert not workflows.is_genesis_workflow(
        {"name": "Genesis Evolver", "path": ".github/workflows/ci.yml"}
    )


def test_list_workflows_requests_path_field(monkeypatch) -> None:
    captured: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    workflows.list_workflows()
    assert "path" in captured[0][captured[0].index("--json") + 1]


# ---------- restart reconciliation ----------


def test_tracked_all_disabled_false_without_tracking_file(monkeypatch) -> None:
    fake = FakeRun([[]])
    monkeypatch.setattr(subprocess, "run", fake)
    assert workflows.tracked_all_disabled() is False


def test_tracked_all_disabled_true_when_state_matches(monkeypatch) -> None:
    """A restart that finds its own workflows still off must not re-arm GHA."""
    workflows._persist_disabled([{"id": 1, "name": "Genesis Evolver"}])
    fake = FakeRun(
        [
            [
                {
                    "id": 1,
                    "name": "Genesis Evolver",
                    "state": "disabled_manually",
                    "path": ".github/workflows/genesis-evolver.yml",
                }
            ]
        ]
    )
    monkeypatch.setattr(subprocess, "run", fake)
    assert workflows.tracked_all_disabled() is True


def test_tracked_all_disabled_false_when_one_was_re_enabled(monkeypatch) -> None:
    """Someone re-enabled a workflow behind our back — reconcile properly."""
    workflows._persist_disabled(
        [{"id": 1, "name": "Genesis Evolver"}, {"id": 2, "name": "Genesis Auto-Merge"}]
    )
    fake = FakeRun(
        [
            [
                {
                    "id": 1,
                    "name": "Genesis Evolver",
                    "state": "disabled_manually",
                    "path": ".github/workflows/genesis-evolver.yml",
                },
                {
                    "id": 2,
                    "name": "Genesis Auto-Merge",
                    "state": "active",
                    "path": ".github/workflows/genesis-merge.yml",
                },
            ]
        ]
    )
    monkeypatch.setattr(subprocess, "run", fake)
    assert workflows.tracked_all_disabled() is False
