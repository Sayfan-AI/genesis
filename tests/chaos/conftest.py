"""Harness for chaos-testing the local control plane.

The dev system's interesting failures live in the Python control plane rather than
in the model: a session that dies mid-task, a chain that will not stop, a state
file that wedges the loop, a crash that strands a lock. None of those need a real
Claude session to reproduce, which is why this suite runs offline.

Two things are faked and nothing else. `claude` is replaced by a scripted
stand-in on PATH, so the real session plumbing runs (streaming, timeouts, process
groups, the continuation ladder) against a deterministic outcome. GitHub is
stubbed at the `gh`-and-events boundary, because the chaos under test is not
GitHub's behaviour.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from genesis import server, triggers

FAKE = Path(__file__).parent / "fake_claude.py"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A throwaway git repo laid out like a dev system, and cwd pointed at it.

    It has a real `origin` because the progress signal asks whether a commit is
    reachable from a remote-tracking ref, and a repo with no remote answers "no"
    for everything. Without an origin the outside-writer scenario cannot be
    written at all - somebody else's merge arriving over the wire is precisely
    what rung 3 used to score as this session's work (#47).
    """
    # The working repo is a *subdirectory* of tmp_path, not tmp_path itself, so
    # origin and the outsider clone have somewhere private to live. Putting them
    # in tmp_path.parent instead would share one origin across every test in the
    # session, and the second push would be rejected as non-fast-forward.
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    (work / ".genesis").mkdir()
    (work / ".genesis" / "config.toml").write_text('name = "chaos"\n')
    (work / ".claude" / "agents").mkdir(parents=True)
    (work / ".claude" / "agents" / "orchestrator.md").write_text("# orchestrator\n")
    subprocess.run(["git", "init", "-q", "-b", "main", "."], check=True)
    (work / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(
        ["git", "-c", "user.email=c@x", "-c", "user.name=c", "commit", "-qm", "seed"],
        check=True,
    )

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], check=True)
    subprocess.run(["git", "push", "-q", "-u", "origin", "main"], check=True)
    return work


@pytest.fixture
def outside_writer(repo, tmp_path):
    """Land a commit on origin that this repo did not author.

    A human merging a pull request while a session runs, or auto-merge landing a
    bot PR in the GitHub Actions mode. The session sees it only once it pulls.
    """
    clone = tmp_path / "outsider"

    def land(message: str = "somebody else's merge") -> None:
        if not clone.exists():
            subprocess.run(
                ["git", "clone", "-q", str(tmp_path / "origin.git"), str(clone)],
                check=True,
            )
        (clone / "outsider.txt").write_text(message + "\n")
        subprocess.run(["git", "-C", str(clone), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(clone), "-c", "user.email=o@x", "-c", "user.name=outsider",
             "commit", "-qm", message],
            check=True,
        )
        subprocess.run(["git", "-C", str(clone), "push", "-q", "origin", "main"], check=True)

    return land


@pytest.fixture
def timeline(repo, monkeypatch):
    """One ordered record of everything the plane forks, across both children.

    `sessions_run` and `issues_script` each answer "how many, and with what
    arguments", and neither can answer "which came first". Ordering is a property
    the control plane asserts in prose and nothing checks: a chain that ends
    without finishing releases its claims *before* the follow-up pass, because
    that pass is a fresh session that re-selects work through `issues.sh next`,
    and `next` skips anything still labelled `in-progress`. Release second and
    the rescue pass walks straight past the task it was queued to rescue.
    """
    path = repo / "timeline.txt"
    monkeypatch.setenv("FAKE_CLAUDE_TIMELINE", str(path))

    def entries() -> list[str]:
        return path.read_text().splitlines() if path.exists() else []

    entries.path = path  # type: ignore[attr-defined]
    return entries


@pytest.fixture
def script(repo, timeline, monkeypatch):
    """Install the fake claude on PATH and return a writer for its script."""
    bindir = repo / "fakebin"
    bindir.mkdir()
    shim = bindir / "claude"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

    spec_path = repo / "claude-script.json"
    monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", str(spec_path))

    def write(sessions, judge="STOP"):
        spec_path.write_text(json.dumps({"sessions": sessions, "judge": judge}))
        return spec_path

    return write


@pytest.fixture
def plane(repo, monkeypatch):
    """A control plane with GitHub stubbed out, ready to run sessions."""
    monkeypatch.setattr(server, "_gh_token", lambda: "fake-token")
    monkeypatch.setattr(server, "mint_installation_token", lambda *a, **k: None)
    monkeypatch.setattr(server, "loki_push", lambda *a, **k: True)
    return server.LocalControlPlane(
        repo="chaos/repo", poll_interval=1, session_timeout=60,
        agent=".claude/agents/orchestrator.md",
    )


@pytest.fixture
def sessions_run(repo):
    """How many times the fake claude was invoked, judge calls included."""
    def count():
        counter = repo / "claude-script.json.n"
        return int(counter.read_text()) if counter.exists() else 0
    return count


@pytest.fixture
def issues_script(repo, timeline):
    """A stand-in for the seeded `issues.sh`, recording how the plane called it.

    Claim bookkeeping is the third thing the plane forks (after `claude` and
    `git`), and it runs on the path where a session has just died — which is the
    path this suite spends all its time on. Recorded rather than stubbed out, so
    a scenario can assert that a killed session actually handed its issue back.

    Every call also lands on the shared timeline, so a scenario can check where
    it fell relative to the sessions around it.
    """
    script = repo / ".genesis" / "scripts" / "issues.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    record = repo / "issues-calls.txt"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$GENESIS_SESSION|$*" >> "{record}"\n'
        f'echo "issues $1" >> "{timeline.path}"\n'
    )

    def calls() -> list[str]:
        return record.read_text().splitlines() if record.exists() else []

    return calls


@pytest.fixture
def loop(plane, monkeypatch):
    """Drive the real poll loop — `serve()` — across a scripted run of ticks.

    Every other scenario in this suite stops at `run_orchestrator`, one unit of
    work. The failures issue #33 named are one level up and only appear across
    *many* units: a merged pull request that wakes nothing, a follow-up budget
    that never resets, a task that dies and strands the ones queued behind it, a
    second control plane starting on top of the first. None of those are
    reachable from a single chain, so none of them had anywhere to be reproduced.

    Call it with one entry per poll, each entry the actor logins of the events
    that arrive on that poll. `[[], ["a-human"], []]` is three polls with one
    human event on the second. A bot login is fine to script: it goes through
    `poll_once` and the real feedback-loop filter, not through anything here.

    GitHub is stubbed at the same boundary the rest of the suite uses. What is
    NOT stubbed is the loop itself: the lock, the workflow disable and restore,
    the merge sweep, the claim sweep, the per-event reset and the follow-up pass
    all run for real.
    """

    def run(ticks: list[list[str]], merges: list[list[int]] | None = None) -> dict:
        merges = list(merges or [])
        state = {"polls": 0, "disabled": 0, "enabled": 0}

        # The plane's own scheduled triggers are a separate mechanism with its own
        # tests, and a fresh state file reads as "never fired", so every eventless
        # tick would launch a cron session and drown the counts these scenarios
        # are made of. Marked as just-fired instead of stubbed out, so the real
        # `run_due_triggers` still runs and still decides.
        triggers.save_state({"scheduled": time.time(), "evolver": time.time()})
        monkeypatch.setattr(triggers, "failed_runs", lambda *a, **k: [])

        monkeypatch.setattr(
            server, "disable_workflows",
            lambda *a, **k: state.__setitem__("disabled", state["disabled"] + 1),
        )
        monkeypatch.setattr(
            server, "enable_workflows",
            lambda *a, **k: state.__setitem__("enabled", state["enabled"] + 1),
        )
        # Priming reads the live events feed to find a high-water mark. There is
        # no feed here and the scripted ticks are the whole event history.
        monkeypatch.setattr(plane, "_prime_high_water_if_needed", lambda *a, **k: None)
        monkeypatch.setattr(
            plane, "merge_ready_prs",
            lambda: merges[state["polls"] - 1] if state["polls"] <= len(merges) else [],
        )

        def events_for(index: int) -> list[dict]:
            if index > len(ticks):
                return []
            # Newest first, which is the order the API returns and the order
            # `filter_relevant_events` expects.
            return [
                {
                    "id": f"e{index}-{n}",
                    "type": "IssuesEvent",
                    "actor": {"login": actor},
                    "payload": {"action": "opened"},
                }
                for n, actor in reversed(list(enumerate(ticks[index - 1])))
            ]

        monkeypatch.setattr(
            server, "fetch_events",
            lambda *a, **k: server.PollResult(
                events=events_for(state["polls"]), etag=None, not_modified=False
            ),
        )

        # The loop is `while not shutdown`, and the harness is what ends it: one
        # tick per scripted entry, then stop. `polls` is therefore also the
        # liveness assertion — a defect that shuts the plane down early leaves it
        # short of `len(ticks)`, which no session count would show. Stopping the
        # loop from here rather than from the code under test is deliberate: a
        # loop that fails to stop hangs, and a hang in CI reads as a flaky runner
        # instead of the defect it is.
        def tick(_seconds: float) -> None:
            if state["polls"] >= len(ticks):
                plane.shutdown = True
                return
            state["polls"] += 1

        monkeypatch.setattr(plane, "_interruptible_sleep", tick)

        state["rc"] = plane.serve()
        return state

    return run
