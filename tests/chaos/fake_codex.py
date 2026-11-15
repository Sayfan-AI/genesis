#!/usr/bin/env python3
"""A stand-in for the `codex` CLI that replays scripted sessions.

The counterpart of fake_claude.py for the Codex engine. It speaks what
`codex exec --json` was measured to speak on codex-cli 0.166.0 (genesis issue
#146), including the parts that differ from Claude Code and that the plane has
to handle:

- `thread.started` carries the session id, and `item.completed` carries one
  item per command
- `turn.completed` carries usage, but as the session's running total
- a session killed before it finishes reports no usage in the stream at all,
  so the real figure lives in the session log under $CODEX_HOME/sessions, which
  this fake keeps the same way the real CLI does
- `error` items are warnings and appear before the first turn

The script lives in JSON at $FAKE_CODEX_SCRIPT:

    {"sessions": [{"tools": 50, "tokens_per_tool": 1000, "end": "completed",
                   "commit": "worked.txt", "delay": 0.05}],
     "judge": "STOP"}

Per-session keys:
    tools            how many command items to emit
    tokens_per_tool  fresh tokens each command adds to the session's total
    end              completed (turn.completed), failed (turn.failed), crash
                     (exit non-zero with no terminal event)
    commit           write AND commit this file before ending
    delay            seconds to sleep after each command, so the plane has time
                     to stop the session mid-stream
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

def thread_id_for(n: int) -> str:
    """A distinct id per fresh session, so a plane that keeps an earlier
    session's id charges and resumes the wrong one, visibly."""
    return f"019a0000-0000-7000-8000-{n:012d}"


def emit(obj: dict) -> None:
    print(json.dumps(obj), flush=True)


def log_path(thread: str) -> Path:
    home = Path(os.environ["CODEX_HOME"])
    path = home / "sessions" / "2026" / "11" / "15" / f"rollout-2026-11-15T00-00-00-{thread}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        # The real log's first line, which is where the plane learns the id.
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": thread}}) + "\n")
    return path


def running_total(thread: str) -> int:
    path = log_path(thread)
    if not path.exists():
        return 0
    total = 0
    for line in path.read_text().splitlines():
        info = (json.loads(line).get("payload") or {}).get("info") or {}
        if info.get("total_token_usage"):
            total = info["total_token_usage"]["input_tokens"]
    return total


def record(thread: str, total: int) -> None:
    """Append a token_count event the way the real session log does."""
    usage = {"input_tokens": total, "cached_input_tokens": 0, "output_tokens": 0}
    with log_path(thread).open("a") as f:
        f.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": usage, "last_token_usage": usage}}}) + "\n")


def main() -> int:
    spec = json.loads(Path(os.environ["FAKE_CODEX_SCRIPT"]).read_text())
    argv = sys.argv[1:]
    with open(os.environ["FAKE_CODEX_SCRIPT"] + ".argv", "a") as f:
        f.write(json.dumps(argv) + "\n")

    if argv[:1] != ["exec"]:
        print(f"fake codex: unexpected argv {argv}", file=sys.stderr)
        return 2

    # The judge: read-only and ephemeral, one turn, its own usage.
    if "--sandbox" in argv and argv[argv.index("--sandbox") + 1] == "read-only":
        emit({"type": "thread.started", "thread_id": "judge-thread"})
        emit({"type": "turn.started"})
        emit({"type": "item.completed", "item": {"type": "agent_message", "text": spec.get("judge", "STOP")}})
        emit({"type": "turn.completed", "usage": {"input_tokens": 500, "cached_input_tokens": 0, "output_tokens": 10}})
        return 0

    counter = Path(os.environ["FAKE_CODEX_SCRIPT"] + ".n")
    n = int(counter.read_text()) if counter.exists() else 0
    sessions = spec.get("sessions") or [{}]
    step = sessions[min(n, len(sessions) - 1)]
    counter.write_text(str(n + 1))

    # A resume continues the session it was given; a fresh run gets its own id.
    thread = argv[2] if argv[1:2] == ["resume"] else thread_id_for(n)
    emit({"type": "thread.started", "thread_id": thread})
    emit({"type": "item.completed", "item": {"id": "w0", "type": "error",
                                             "message": "`--dangerously-bypass-hook-trust` is enabled."}})
    emit({"type": "turn.started"})

    total = running_total(thread)
    per_tool = int(step.get("tokens_per_tool", 1000))
    for i in range(int(step.get("tools", 0))):
        cmd = f"/bin/zsh -lc 'echo step-{n}-{i}'"
        emit({"type": "item.started", "item": {"id": f"c{i}", "type": "command_execution", "command": cmd,
                                               "status": "in_progress"}})
        total += per_tool
        record(thread, total)
        emit({"type": "item.completed", "item": {"id": f"c{i}", "type": "command_execution", "command": cmd,
                                                 "exit_code": 0, "status": "completed"}})
        time.sleep(float(step.get("delay", 0)))

    if step.get("commit"):
        Path(step["commit"]).write_text(f"session {n}\n")
        subprocess.run(["git", "add", "--", step["commit"]], check=True)
        subprocess.run(["git", "-c", "user.email=f@x", "-c", "user.name=fake", "commit", "-qm",
                        f"session {n} work"], check=True)

    end = step.get("end", "completed")
    if end == "crash":
        return 1
    if end == "failed":
        emit({"type": "turn.failed", "error": {"message": "stream disconnected"}})
        return 1
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}})
    emit({"type": "turn.completed", "usage": {"input_tokens": total, "cached_input_tokens": 0, "output_tokens": 0}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
