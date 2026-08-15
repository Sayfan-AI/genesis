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
    touch    write this file in cwd, so the repo fingerprint changes
    hang     sleep forever instead of finishing, to exercise the deadline
    crash    exit non-zero without emitting a result event
    garbage  emit an unparseable line before the result
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

SESSION_ID = "chaos00-0000-0000-0000-00000000000"


def emit(obj: dict) -> None:
    print(json.dumps(obj), flush=True)


def main() -> int:
    spec_path = os.environ.get("FAKE_CLAUDE_SCRIPT")
    if not spec_path:
        print("FAKE_CLAUDE_SCRIPT not set", file=sys.stderr)
        return 1
    spec = json.loads(Path(spec_path).read_text())
    argv = sys.argv[1:]

    # The judge is the one session launched with no tools and a tiny budget. It
    # answers in prose rather than stream-json, so it is handled separately.
    if "--max-turns" in argv:
        budget = int(argv[argv.index("--max-turns") + 1])
        if budget <= 2:
            print(spec.get("judge", "STOP"))
            return 0

    counter = Path(spec_path + ".n")
    n = int(counter.read_text()) if counter.exists() else 0
    sessions = spec.get("sessions") or [{}]
    step = sessions[min(n, len(sessions) - 1)]
    counter.write_text(str(n + 1))

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
