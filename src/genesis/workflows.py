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
    so a later `enable_workflows()` call can restore only what genesis disabled.
    Returns the names of newly-disabled workflows.
    """
    disabled: list[dict] = []
    for wf in list_workflows(repo):
        if wf["state"] == "active":
            print(f"Disabling workflow: {wf['name']}")
            cmd = ["gh", "workflow", "disable", str(wf["id"])] + _gh_repo_args(repo)
            subprocess.run(cmd, check=True)
            disabled.append({"id": wf["id"], "name": wf["name"]})
    if disabled:
        _persist_disabled(disabled)
    return [wf["name"] for wf in disabled]


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
