"""Local equivalent of the `genesis-merge.yml` workflow.

KEEP IN SYNC with `templates/workflows/genesis-merge.yml`, which runs the same
predicate as a jq filter on a GitHub Actions runner. The two can't share code -
this is Python inside the genesis package on an operator's laptop, that is jq in
a scaffolded repo with no dependency on genesis - so `tests/e2e/test_workflows.py`
runs both against the same fixtures and fails if they ever disagree. Change the
rule here, change it there.

In GitHub Actions, a pull request going green triggers `workflow_run` and the
merge workflow lands it. Local mode had no counterpart, so a green pull request
waited for a human. Two things conspired:

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

# Matches the author predicate in genesis-merge.yml: the dev system lands its
# own work, and a human's pull request stays a human's decision.
BOT_SUFFIX = "[bot]"


def is_bot_author(author: object) -> bool:
    """True when a `gh pr list --json author` entry is a bot.

    `gh` reports an App author as `{"login": "app/genesis-dev-bot", "is_bot":
    true}`. `<slug>[bot]` is the REST and events-feed spelling, which is what
    `is_bot_actor` sees and what this predicate was first written against, so
    it matched no real bot pull request (genesis issue #93). `is_bot` is the
    signal and the suffix is a fallback for REST-shaped input.
    """
    if not isinstance(author, dict):
        return False
    return author.get("is_bot") is True or str(author.get("login", "")).endswith(BOT_SUFFIX)


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
        if not is_bot_author(pr.get("author")):
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


# Paths an autonomous pull request can't land on its own. One prefix per line,
# `#` comments. A repo opts in by carrying the file; genesis seeds none, so a dev
# system without one merges exactly as before.
PROTECTED_PATHS_FILE = ".genesis/protected-paths"
HOLD_LABEL = "needs:human"


def _is_not_found(out: str) -> bool:
    return "HTTP 404" in out or '"status":"404"' in out


def protected_paths(repo: str, token: str | None = None) -> list[str] | None:
    """The protected prefixes, read from the default branch. None when unreadable.

    The default branch, never the pull request or a working tree: a list the
    pull request can edit is a list it can loosen. Genesis's served evolver
    works in the same clone the plane sweeps from, so the working tree may be
    sitting on the very branch under review.
    """
    code, out = _gh(
        ["api", f"repos/{repo}/contents/{PROTECTED_PATHS_FILE}",
         "-H", "Accept: application/vnd.github.raw"],
        token,
    )
    if code != 0:
        return [] if _is_not_found(out) else None
    prefixes = []
    for line in out.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            prefixes.append(line)
    return prefixes


def changed_files(repo: str, number: int, token: str | None = None) -> list[str] | None:
    """Every path a pull request touches, renamed-from paths included."""
    code, out = _gh(
        ["api", f"repos/{repo}/pulls/{number}/files", "--paginate",
         "--jq", ".[] | .filename, (.previous_filename // empty)"],
        token,
    )
    if code != 0:
        return None
    return [line for line in out.splitlines() if line]


def protected_hit(files: list[str], prefixes: list[str]) -> str | None:
    """The first changed path under a protected prefix, or None.

    A prefix matches itself and anything beneath it, whether or not it ends in
    `/`, so `src/genesis/automerge.py` and `.claude/` both mean what they say.
    """
    for f in files:
        for p in prefixes:
            base = p.rstrip("/")
            if f == base or f.startswith(base + "/"):
                return f
    return None


def _hold(repo: str, number: int, path: str, token: str | None, log) -> None:
    """Leave the pull request open, labelled for a person, and say why once."""
    code, out = _gh(["pr", "view", str(number), "--repo", repo, "--json", "labels",
                     "--jq", "[.labels[].name] | index(\"" + HOLD_LABEL + "\") != null"], token)
    if code == 0 and out.strip() == "true":
        return
    _gh(["label", "create", HOLD_LABEL, "--repo", repo, "--color", "B60205",
         "--description", "Waiting on a person"], token)
    _gh(["pr", "comment", str(number), "--repo", repo, "--body",
         f"Not auto-merged: this pull request changes `{path}`, which "
         f"`{PROTECTED_PATHS_FILE}` on the default branch reserves for a human. "
         "Everything else about it is green. A person merges it, or closes it."], token)
    _gh(["pr", "edit", str(number), "--repo", repo, "--add-label", HOLD_LABEL], token)
    log(f"  held PR #{number} for a human: it touches {path}")


def merge_ready(repo: str, token: str | None = None, log=print) -> list[int]:
    """Squash-merge everything currently mergeable. Returns the numbers merged.

    Pull requests that touch a protected path are held rather than merged. That
    check lives here, in the pinned plane, because the rules it protects - the
    guards, the agent's charter, the merge rule itself - are exactly what an
    agent with auto-merge could otherwise loosen for itself. It fails closed: if
    the list or a pull request's files can't be read, nothing is merged, and the
    next tick tries again.
    """
    merged: list[int] = []
    ready = ready_to_merge(repo, token)
    if not ready:
        return merged
    prefixes = protected_paths(repo, token)
    if prefixes is None:
        log(f"  could not read {PROTECTED_PATHS_FILE}; merging nothing this sweep")
        return merged
    for pr in ready:
        number = pr["number"]
        if prefixes:
            files = changed_files(repo, number, token)
            if files is None:
                log(f"  could not list PR #{number}'s files; leaving it for the next sweep")
                continue
            hit = protected_hit(files, prefixes)
            if hit:
                _hold(repo, number, hit, token, log)
                continue
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
