"""GitHub Actions workflow management.

Thin wrapper around `gh workflow` for enabling/disabling workflows in a
repository. Used by the local control plane to prevent GHA from running
orchestrator sessions while a local one is active.

When genesis disables workflows, it persists the set it disabled to
`.genesis/.disabled-by-genesis`. On re-enable, only that set is restored,
so workflows the user had intentionally disabled before running
`genesis serve` stay disabled.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

DISABLED_LIST_PATH = Path(".genesis/.disabled-by-genesis")


def _gh_repo_args(repo: str | None) -> list[str]:
    return ["--repo", repo] if repo else []


def list_workflows(repo: str | None = None) -> list[dict]:
    """Return all GitHub Actions workflows in the target repository."""
    cmd = ["gh", "workflow", "list", "--all", "--json", "id,name,state"]
    cmd += _gh_repo_args(repo)
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def _persist_disabled(disabled: list[dict]) -> None:
    DISABLED_LIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    DISABLED_LIST_PATH.write_text(json.dumps(disabled))


def _load_disabled() -> list[dict] | None:
    try:
        return json.loads(DISABLED_LIST_PATH.read_text())
    except FileNotFoundError:
        return None


def _clear_disabled() -> None:
    DISABLED_LIST_PATH.unlink(missing_ok=True)


def disable_workflows(repo: str | None = None) -> list[str]:
    """Disable all currently-active workflows in the target repo.

    Persists the set of disabled workflow IDs to `.genesis/.disabled-by-genesis`
    after each successful disable so a partial failure (e.g. one of N gh calls
    raises) still leaves a recoverable record of what was disabled. If the
    tracking file already exists from a prior session, new disables are
    appended to it.

    Returns the names of newly-disabled workflows.
    """
    existing = _load_disabled() or []
    tracked_ids = {wf["id"] for wf in existing}
    disabled = list(existing)
    new_names: list[str] = []
    for wf in list_workflows(repo):
        if wf["state"] != "active":
            continue
        print(f"Disabling workflow: {wf['name']}")
        cmd = ["gh", "workflow", "disable", str(wf["id"])] + _gh_repo_args(repo)
        subprocess.run(cmd, check=True)
        if wf["id"] not in tracked_ids:
            disabled.append({"id": wf["id"], "name": wf["name"]})
            tracked_ids.add(wf["id"])
        new_names.append(wf["name"])
        _persist_disabled(disabled)
    return new_names


def enable_workflows(repo: str | None = None) -> list[str]:
    """Re-enable workflows in the target repo.

    Targeted mode: if `.genesis/.disabled-by-genesis` exists, re-enable only
    those IDs (and only if they're currently `disabled_manually`). This is the
    graceful-shutdown path — preserves user-intent for workflows the user had
    paused before running `genesis serve`.

    Recovery mode: if the tracking file is missing (e.g. the file was lost or
    `genesis workflows enable` is being used as a recovery hatch), fall back
    to enabling everything currently `disabled_manually`.

    Returns the names of newly-enabled workflows.
    """
    tracked = _load_disabled()
    workflows = list_workflows(repo)

    if tracked is not None:
        tracked_ids = {wf["id"] for wf in tracked}
        candidates = [
            wf
            for wf in workflows
            if wf["id"] in tracked_ids and wf["state"] == "disabled_manually"
        ]
    else:
        candidates = [wf for wf in workflows if wf["state"] == "disabled_manually"]

    enabled: list[str] = []
    for wf in candidates:
        print(f"Enabling workflow: {wf['name']}")
        cmd = ["gh", "workflow", "enable", str(wf["id"])] + _gh_repo_args(repo)
        subprocess.run(cmd, check=True)
        enabled.append(wf["name"])

    _clear_disabled()
    return enabled
