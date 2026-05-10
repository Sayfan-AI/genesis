"""GitHub Actions workflow management.

Thin wrapper around `gh workflow` for enabling/disabling workflows in the
current repository. Used by the local control plane to prevent GHA from
running orchestrator sessions while a local one is active.
"""

from __future__ import annotations

import json
import subprocess


def list_workflows() -> list[dict]:
    """Return all GitHub Actions workflows in the current repository."""
    result = subprocess.run(
        ["gh", "workflow", "list", "--all", "--json", "id,name,state"],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def disable_workflows() -> list[str]:
    """Disable all currently-active workflows. Returns names of disabled workflows."""
    disabled: list[str] = []
    for wf in list_workflows():
        if wf["state"] == "active":
            print(f"Disabling workflow: {wf['name']}")
            subprocess.run(
                ["gh", "workflow", "disable", str(wf["id"])],
                check=True,
            )
            disabled.append(wf["name"])
    return disabled


def enable_workflows() -> list[str]:
    """Enable all manually-disabled workflows. Returns names of re-enabled workflows."""
    enabled: list[str] = []
    for wf in list_workflows():
        if wf["state"] == "disabled_manually":
            print(f"Enabling workflow: {wf['name']}")
            subprocess.run(
                ["gh", "workflow", "enable", str(wf["id"])],
                check=True,
            )
            enabled.append(wf["name"])
    return enabled
