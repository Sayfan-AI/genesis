"""Local equivalent of the `genesis-merge.yml` workflow.

In GitHub Actions, a pull request going green triggers `workflow_run` and a merge
agent decides whether to land it. Local mode had no counterpart, so a green pull
request waited for a human. Two things conspired:

- `serve` disables `genesis-merge.yml`, since the whole point of local mode is
  that GHA is not driving.
- The poller wakes on issue, comment, and pull-request events, and **drops bot
  actors** to avoid feedback loops. Once local sessions began authenticating as
  the App, the agent's own pull requests became bot events and stopped waking
  anything at all.

CI-completion is also not an event the poller sees, so "checks just went green"
could never wake anything even without the actor filter.

This closes that gap by sweeping for mergeable pull requests on every poll tick,
which is the state-derived version of the same rule rather than an event-derived
one - the shape every other fix in this system converged on. It is deterministic
on purpose: it costs nothing, it cannot exhaust a turn budget, and the merge rule
("bot-authored, not draft, every check green") is a predicate, not a judgement.
"""

from __future__ import annotations

import json
import os
import subprocess

# Matches the merge agent's own rule in genesis-merge.yml: the dev system lands
# its own work, and a human's pull request stays a human's decision.
BOT_SUFFIX = "[bot]"


def _gh(args: list[str], token: str | None, timeout: int = 60) -> tuple[int, str]:
    env = dict(os.environ)
    if token:
        env["GH_TOKEN"] = token
        env["GITHUB_TOKEN"] = token
    try:
        result = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=timeout, env=env, check=False
        )
        return result.returncode, (result.stdout or result.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


def ready_to_merge(repo: str, token: str | None = None) -> list[dict]:
    """Pull requests that satisfy every merge precondition, most stale first.

    Deliberately strict about checks. `mergeStateStatus == CLEAN` is not enough:
    GitHub reports CLEAN when no branch protection *requires* the failing check,
    so a red pull request passes that test. Every check must have concluded
    SUCCESS, and a pull request with no checks at all is never merged - that
    usually means CI has not started yet.
    """
    code, out = _gh(
        [
            "pr", "list", "--repo", repo, "--state", "open", "--json",
            "number,title,isDraft,mergeable,author,statusCheckRollup,createdAt",
        ],
        token,
    )
    if code != 0:
        return []
    try:
        prs = json.loads(out)
    except ValueError:
        return []

    ready = []
    for pr in prs:
        if pr.get("isDraft"):
            continue
        if not str(pr.get("author", {}).get("login", "")).endswith(BOT_SUFFIX):
            continue
        if pr.get("mergeable") not in (None, "MERGEABLE"):
            continue
        checks = pr.get("statusCheckRollup") or []
        if not checks:
            continue
        if any((c.get("conclusion") or c.get("state")) not in ("SUCCESS", "SKIPPED") for c in checks):
            continue
        ready.append(pr)
    ready.sort(key=lambda p: p.get("createdAt") or "")
    return ready


def merge_ready(repo: str, token: str | None = None, log=print) -> list[int]:
    """Squash-merge everything currently mergeable. Returns the numbers merged."""
    merged: list[int] = []
    for pr in ready_to_merge(repo, token):
        number = pr["number"]
        code, out = _gh(
            ["pr", "merge", str(number), "--repo", repo, "--squash", "--delete-branch"], token
        )
        if code == 0:
            log(f"  merged PR #{number}: {str(pr.get('title', ''))[:60]}")
            merged.append(number)
        else:
            # Not fatal: a merge can lose a race, or branch protection can refuse.
            # The sweep runs again next tick.
            log(f"  could not merge PR #{number}: {out.splitlines()[0][:100] if out else 'unknown'}")
    return merged
