"""Trigger layer for local mode - the counterpart to the `genesis-*` workflows.

Each seeded workflow is a pair: a condition GitHub detects, and an agent it runs.
Local mode disables the workflows, so `serve` has to supply the same pairs, or the
dev system loses capabilities simply by being driven from a laptop.

| Workflow                | Condition                       | Action           |
|-------------------------|---------------------------------|------------------|
| `genesis-events`        | issue / PR / comment activity   | orchestrator     |
| `genesis-orchestrator`  | cron, every 6 hours             | orchestrator     |
| `genesis-evolver`       | cron, daily                     | evolver          |
| `genesis-merge`         | CI completed green on a bot PR  | merge (no agent) |
| `genesis-ci-failure`    | a required check failed         | triage           |
| `genesis-push-trigger`  | push to main                    | orchestrator     |

Two of those conditions are invisible to an events poller and are why this module
exists rather than a longer list of event types: a cron has no event at all, and
CI completing is a `workflow_run` that the repo events feed does not carry.

Everything here is a pure function over injected state and time so the schedule
can be tested without waiting six hours.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

STATE_PATH = Path(".genesis/.trigger-state")
# What a seeded `genesis-*` workflow's `if: failure()` step runs. Genesis's own
# repo doesn't carry it, so its own workflows don't escalate (issue #112).
ESCALATE_PATH = Path(".genesis/scripts/escalate.sh")

# Matches the cron in the seeded workflows.
SCHEDULED_ORCHESTRATOR_SECONDS = 6 * 60 * 60
EVOLVER_SECONDS = 24 * 60 * 60


@dataclass
class Due:
    """A trigger that has come due, and how to describe the run it starts."""

    name: str
    agent: str
    prompt: str


def load_state(path: Path = STATE_PATH) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))
    except OSError:
        pass


def _elapsed(state: dict, key: str, now: float) -> float:
    """Seconds since a trigger last fired.

    A missing entry counts as "long ago" so a fresh checkout runs its scheduled
    work once rather than waiting a full interval to do anything at all.
    """
    last = state.get(key)
    if not isinstance(last, (int, float)):
        return float("inf")
    return max(0.0, now - float(last))


# What the scheduled and CI-failure triggers run unless the plane was pointed at
# something else. They used to hardcode it, so `genesis serve --agent
# .claude/agents/evolver.md` on genesis - which has no orchestrator - would have
# launched a session every six hours, and on every red check, told to run a file
# that doesn't exist.
ORCHESTRATOR_AGENT = ".claude/agents/orchestrator.md"


def scheduled_due(state: dict, now: float, agent: str = ORCHESTRATOR_AGENT) -> Due | None:
    if _elapsed(state, "scheduled", now) < SCHEDULED_ORCHESTRATOR_SECONDS:
        return None
    return Due(
        name="scheduled",
        agent=agent,
        prompt=(
            f"Run the agent defined in {agent}.\n\n"
            "This is a scheduled run - assess project state and advance work."
        ),
    )


def evolver_due(state: dict, now: float, agent_exists: bool = True) -> Due | None:
    """The daily self-improvement cycle.

    Skipped when the repo has no evolver definition: not every dev system keeps
    one, and prompting for a file that does not exist wastes a session.
    """
    if not agent_exists:
        return None
    if _elapsed(state, "evolver", now) < EVOLVER_SECONDS:
        return None
    return Due(
        name="evolver",
        agent=".claude/agents/evolver.md",
        prompt=(
            "Run the agent defined in .claude/agents/evolver.md.\n\n"
            "This is a scheduled review cycle - review recent system behavior and "
            "apply improvements."
        ),
    )


def failed_runs(repo: str, since_iso: str | None, token: str | None = None) -> list[dict]:
    """Workflow runs that failed since we last looked.

    This is the `genesis-ci-failure` trigger. It has to be polled rather than
    observed: a `workflow_run` conclusion is not in the repo events feed, so a red
    check would otherwise be invisible to local mode until a human noticed.
    """
    import os

    env = dict(os.environ)
    if token:
        env["GH_TOKEN"] = token
        env["GITHUB_TOKEN"] = token
    try:
        result = subprocess.run(
            [
                "gh", "run", "list", "--repo", repo, "--status", "failure",
                "--limit", "15", "--json", "databaseId,name,conclusion,createdAt,headBranch,url",
            ],
            capture_output=True, text=True, timeout=45, env=env, check=False,
        )
        runs = json.loads(result.stdout) if result.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, ValueError):
        return []

    # Seeded genesis workflows escalate their failures through escalate.sh, and
    # re-triaging those here would double-report a failure that already opened an
    # issue. Only the repo's own gates are this trigger's business. That premise
    # is checked, not assumed: genesis's own repo has no escalate.sh and its
    # workflows no failure step, so filtering there left a red Genesis Auto-Merge
    # on `main` for a human to find (issue #112).
    if ESCALATE_PATH.is_file():
        runs = [r for r in runs if not str(r.get("name", "")).startswith("Genesis")]
    if since_iso:
        runs = [r for r in runs if str(r.get("createdAt", "")) > since_iso]
    return sorted(runs, key=lambda r: str(r.get("createdAt", "")))


def open_human_gates(repo: str, token: str | None = None) -> list[dict] | None:
    """Open `needs:human` issues that a scheduled run has no business waking up for.

    The local-mode half of the "Skip the run if a human gate is open" step in
    `genesis-orchestrator.yml` (issue #15). `serve` disables that workflow, so
    without this a gate sat behind a full session every six hours: measured on
    genesis-dashboard, the scheduled tick a minute after onboarding labelled
    issue #1 spent $0.20 reading state to confirm the gate and exit (issue #87).

    Same predicate as the workflow, `automation:failure` excluded: an escalation
    that a dead run filed means the next run may succeed, so it isn't a reason
    to stay home - and gating on it would let one transient error silence the
    cron until a person closed the issue.

    Returns None when the query fails, which the caller treats as "no gate".
    Failing open costs one idle session; failing closed would stop the cron on
    a gh hiccup with nothing in the log to say why.
    """
    import os

    env = dict(os.environ)
    if token:
        env["GH_TOKEN"] = token
        env["GITHUB_TOKEN"] = token
    try:
        result = subprocess.run(
            [
                "gh", "issue", "list", "--repo", repo, "--label", "needs:human",
                "--state", "open", "--limit", "100", "--json", "number,title,labels",
            ],
            capture_output=True, text=True, timeout=45, env=env, check=False,
        )
        if result.returncode != 0:
            return None
        issues = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(issues, list):
        return None
    return [
        i for i in issues
        if isinstance(i, dict)
        and "automation:failure" not in {l.get("name") for l in i.get("labels") or []}
    ]


def ci_failure_due(runs: list[dict], agent: str = ORCHESTRATOR_AGENT) -> Due | None:
    if not runs:
        return None
    newest = runs[-1]
    return Due(
        name="ci-failure",
        agent=agent,
        prompt=(
            f"Run the agent defined in {agent}.\n\n"
            "A required check failed.\n"
            f"- Failing workflow: {newest.get('name', '?')}\n"
            f"- Branch: {newest.get('headBranch', '?')}\n"
            f"- Run: {newest.get('url', '?')}\n\n"
            "Triage it: read the failing run's logs, decide whether it is the "
            "code or the check that is wrong, and either fix it or escalate with "
            "what you found. Do not attempt a heroic inline rewrite."
        ),
    )
