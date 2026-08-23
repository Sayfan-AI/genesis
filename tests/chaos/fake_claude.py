#!/usr/bin/env python3
"""A stand-in for the `claude` CLI that replays scripted sessions.

`server.py` invokes `claude -p ... --output-format stream-json`, resolved through
PATH. Putting this script on PATH ahead of the real binary makes the entire local
control plane testable offline: no API key, no network, no cost, and the same
outcome every run. That last property is what a chaos suite needs, because a
scenario you cannot repeat is an anecdote rather than a regression test.

The script it replays lives in JSON at $FAKE_CLAUDE_SCRIPT:

    {"sessions": [{"tools": 3, "subtype": "error_max_turns", "turns": 41,
                   "cost": 4.2, "touch": "worked.txt"}],
     "judge": "STOP"}

One list entry per session, consumed in order, with the position kept in a
sibling counter file so it survives across the separate process invocations a
continuation chain makes. Running past the end repeats the final entry, so a
scenario probing "does this ever stop" cannot be rescued by the fake running dry.

Per-session keys:
    tools    how many tool_use events to emit (0 exercises the no-tools rung)
    subtype  terminal result subtype: success, error_max_turns, error_during_execution
    turns    num_turns to report
    cost     total_cost_usd to report
    touch    write an untracked file in cwd (ambiguous churn, not progress)
    commit   write AND commit this file, which is progress the session authored
    pull     fast-forward from origin, i.e. somebody else's work arriving
    hang     sleep forever instead of finishing, to exercise the deadline
    crash    exit non-zero without emitting a result event
    garbage  emit an unparseable line before the result

When $FAKE_CLAUDE_TIMELINE is set, every invocation also appends one word to that
file - `session`, `resume` or `judge` - interleaved with the lines the fake
`issues.sh` writes there. See `note()`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

SESSION_ID = "chaos00-0000-0000-0000-00000000000"


def emit(obj: dict) -> None:
    print(json.dumps(obj), flush=True)


def note(kind: str) -> None:
    """Append one line to the shared timeline, when a scenario asked for one.

    The counter file says how many times this ran; the timeline says *when*,
    relative to the other thing the plane forks. Ordering is a property the
    control plane makes claims about in prose — a killed chain hands its issue
    back "before the follow-up pass rather than after", because that pass
    re-selects work through `issues.sh next` and `next` skips anything still
    labelled `in-progress`. A count can't tell a release that happened first
    from one that happened second, and second is the bug.
    """
    path = os.environ.get("FAKE_CLAUDE_TIMELINE")
    if not path:
        return
    with open(path, "a") as handle:
        handle.write(kind + "\n")


def main() -> int:
    spec_path = os.environ.get("FAKE_CLAUDE_SCRIPT")
    if not spec_path:
        print("FAKE_CLAUDE_SCRIPT not set", file=sys.stderr)
        return 1
    spec = json.loads(Path(spec_path).read_text())
    argv = sys.argv[1:]

    # The judge is the one session launched with no tools and a tiny budget. It
    # answers with a single JSON envelope rather than a stream-json feed, so it is
    # handled separately. Emitting the envelope rather than bare prose matters:
    # server.py reads `total_cost_usd` out of it to charge the judge to both cost
    # accumulators, and a harness that printed prose would exercise only the
    # fallback and leave the accounting untested.
    if "--max-turns" in argv:
        budget = int(argv[argv.index("--max-turns") + 1])
        if budget <= 2:
            note("judge")
            print(json.dumps({
                "type": "result",
                "subtype": "success",
                "result": spec.get("judge", "STOP"),
                "total_cost_usd": float(spec.get("judge_cost", 0.0)),
            }))
            return 0

    counter = Path(spec_path + ".n")
    n = int(counter.read_text()) if counter.exists() else 0
    sessions = spec.get("sessions") or [{}]
    step = sessions[min(n, len(sessions) - 1)]
    counter.write_text(str(n + 1))
    note("resume" if "--resume" in argv else "session")

    emit({"type": "system", "subtype": "init", "session_id": SESSION_ID})

    if step.get("hang"):
        time.sleep(3600)
        return 0

    for i in range(int(step.get("tools", 0))):
        emit({
            "type": "assistant",
            "session_id": SESSION_ID,
            "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": f"step-{i}"}}
            ]},
        })

    if step.get("touch"):
        Path(step["touch"]).write_text(f"session {n}\n")

    # Work that actually lands, as distinct from `touch`, which leaves an untracked
    # file. The two used to be interchangeable because the progress signal hashed
    # `git status --porcelain`, so a stray temporary file scored the same as a
    # commit. They are no longer interchangeable, which is the point (#47).
    if step.get("commit"):
        Path(step["commit"]).write_text(f"session {n}\n")
        subprocess.run(["git", "add", "--", step["commit"]], check=True)
        subprocess.run(
            ["git", "-c", "user.email=f@x", "-c", "user.name=fake",
             "commit", "-qm", f"session {n} work"],
            check=True,
        )

    # An outside writer: somebody else's commit arriving over the wire, which is
    # what a human merging a pull request mid-session looks like from here. The
    # session did not author it and must not be credited with it.
    if step.get("pull"):
        subprocess.run(["git", "pull", "-q", "--ff-only", "origin", "main"], check=True)

    if step.get("garbage"):
        print("this is not json", flush=True)

    if step.get("crash"):
        print("fake claude crashed", file=sys.stderr)
        return 1

    emit({
        "type": "result",
        "session_id": SESSION_ID,
        "subtype": step.get("subtype", "success"),
        "num_turns": int(step.get("turns", 1)),
        "total_cost_usd": float(step.get("cost", 0.0)),
        "duration_ms": 1000,
        "is_error": step.get("subtype", "success") != "success",
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
