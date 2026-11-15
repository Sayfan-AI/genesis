"""Agent engines: how the local control plane launches, reads and resumes a session.

The plane's policy lives in `server.py`: when to resume, when to stop, what the
judge sees, how much a chain may spend. None of that depends on which coding agent
runs the session. What does depend on it is narrow, and it all lives here:

- the command line for a session, a resume, and the judge
- how the agent's machine-readable output becomes the events the plane acts on
- how the judge's answer and cost are read back
- where the agent's hooks are declared, and what to tell an operator when the
  binary is missing or a session does nothing

An engine builds commands and parses output. It never starts a process itself:
the plane owns every subprocess, which keeps the process lifecycle (deadlines,
shutdown, the reader thread) in one place, and keeps the test suite's subprocess
fakes working for every engine.

`server.py` acts only on the three event types below, so a second engine is a
second translation into them, not a second control plane.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SessionStarted:
    """The session's id became known. Capturing it is what makes a resume possible."""

    session_id: str


@dataclass(frozen=True)
class ToolCall:
    """One tool call the agent made, rendered for the progress feed and the judge."""

    summary: str


@dataclass(frozen=True)
class SessionResult:
    """A session (or a turn of one) ended.

    `subtype` uses the plane's vocabulary, which is Claude Code's: `success` for a
    clean finish, `error_max_turns` for an exhausted turn budget, and any other
    non-success value for an abnormal ending. An engine whose agent speaks
    differently translates into these.

    `cost` is in the engine's own unit (see `Engine.cost_unit`) and covers only
    this event, never a running total, so the plane can add it up.
    """

    subtype: str | None
    is_error: bool = False
    turns: int | None = None
    cost: float = 0.0
    duration_s: int = 0
    session_id: str | None = None


SessionEvent = SessionStarted | ToolCall | SessionResult


@dataclass(frozen=True)
class JudgeReply:
    """What the judge said, what it cost, and whether that cost could be read.

    `cost_known` is False when the output wasn't the expected envelope. The
    verdict is still read from the text, because a judge that stops parsing would
    fail closed on every call and idle the dev system, but the totals downstream
    become a lower bound and have to say so.
    """

    text: str
    cost: float = 0.0
    cost_known: bool = True


def brief(tool_input: object, limit: int = 88) -> str:
    """Render a tool's input as one short line for the progress feed.

    Prefers the field that says what the call is actually doing (a command, a
    path, a pattern) and falls back to a truncated repr.
    """
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "file_path", "pattern", "path", "query", "description"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            flat = " ".join(value.split())
            return flat[:limit] + ("…" if len(flat) > limit else "")
    flat = " ".join(str(tool_input).split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


class Engine:
    """The interface `server.py` drives. See the module docstring."""

    name: str = ""
    binary: str = ""
    # How costs reported by this engine read in the log, e.g. "$" or "tokens".
    cost_unit: str = ""
    # The prompt a resumed session gets instead of the original one. A resumed
    # session already holds the task, the plan, and what it has done. Restating
    # the original prompt would invite it to start the work over, not finish it.
    resume_prompt: str = "Continue the work you were doing. You ran out of turns, not out of task."

    def session_command(
        self, prompt: str, *, resume: str | None, max_turns: int, tools: str
    ) -> list[str]:
        raise NotImplementedError

    def judge_command(self, prompt: str, *, max_turns: int) -> list[str]:
        raise NotImplementedError

    def stream_parser(self) -> StreamParser:
        raise NotImplementedError

    def read_judge_output(self, stdout: str | None) -> JudgeReply:
        raise NotImplementedError

    def format_cost(self, cost: float) -> str:
        raise NotImplementedError

    def pre_session_hook_declared(self, script_name: str) -> bool:
        """True when the agent's own hooks already run `script_name` at session
        start, so the plane mustn't run it a second time."""
        raise NotImplementedError

    def missing_binary_message(self) -> str:
        raise NotImplementedError

    def did_nothing_hint(self) -> str:
        """What to check when a session reports success having run no tools."""
        raise NotImplementedError


class StreamParser:
    """Turns one session's output lines into `SessionEvent`s. One per session,
    because a parser may keep state across lines."""

    def feed(self, event: dict) -> list[SessionEvent]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------


@dataclass
class ClaudeStreamParser(StreamParser):
    """Claude Code's `--output-format stream-json`.

    Every event carries the session id, so the first one seen is reported as the
    start. A process can emit more than one `result` event (#78, #79), so each is
    reported as it comes, with its own cost, and the plane accumulates.
    """

    started: bool = field(default=False)

    def feed(self, event: dict) -> list[SessionEvent]:
        out: list[SessionEvent] = []
        session_id = event.get("session_id")
        if session_id and not self.started:
            self.started = True
            out.append(SessionStarted(session_id))
        kind = event.get("type")
        if kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    out.append(ToolCall(f"{block.get('name')} {brief(block.get('input'))}"))
        elif kind == "result":
            out.append(
                SessionResult(
                    subtype=event.get("subtype"),
                    is_error=bool(event.get("is_error")),
                    turns=event.get("num_turns"),
                    cost=float(event.get("total_cost_usd") or 0),
                    duration_s=round((event.get("duration_ms") or 0) / 1000),
                    session_id=event.get("session_id"),
                )
            )
        return out


class ClaudeEngine(Engine):
    name = "claude"
    binary = "claude"
    cost_unit = "$"
    settings_path = Path(".claude/settings.json")

    def session_command(
        self, prompt: str, *, resume: str | None, max_turns: int, tools: str
    ) -> list[str]:
        cmd = [self.binary, "-p"]
        if resume:
            cmd += ["--resume", resume, self.resume_prompt]
        else:
            cmd.append(prompt or "")
        return cmd + [
            "--max-turns",
            str(max_turns),
            "--allowedTools",
            tools,
            # Without this, `claude -p` buffers everything until the session ends,
            # so a 25-minute run prints nothing at all. Hook stderr doesn't help
            # either, because Claude Code captures it into its own transcript
            # rather than passing it through. Streaming turns a silent box into a
            # feed.
            "--output-format",
            "stream-json",
            "--verbose",
        ]

    def judge_command(self, prompt: str, *, max_turns: int) -> list[str]:
        return [
            self.binary,
            "-p",
            prompt,
            "--max-turns",
            str(max_turns),
            # Empty: the judge reads the evidence it's handed and uses no tools.
            "--allowedTools",
            "",
            # Asked for so the judge's own spend can be read back. Without it
            # `claude -p` prints bare prose, nothing carries `total_cost_usd`, and
            # both bounds undercount by one judge session per continuation
            # decision (#50), precisely on the ambiguous rung a thrashing chain
            # keeps landing on, so the calls cluster where spend already matters.
            "--output-format",
            "json",
        ]

    def stream_parser(self) -> StreamParser:
        return ClaudeStreamParser()

    def read_judge_output(self, stdout: str | None) -> JudgeReply:
        text = (stdout or "").strip()
        if not text:
            return JudgeReply("")
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
        if not isinstance(payload, dict) or "result" not in payload:
            return JudgeReply(text, 0.0, cost_known=False)
        return JudgeReply(str(payload.get("result") or "").strip(), float(payload.get("total_cost_usd") or 0))

    def format_cost(self, cost: float) -> str:
        return f"${cost:.2f}"

    def pre_session_hook_declared(self, script_name: str) -> bool:
        # Reads defensively and answers False on anything it can't parse: the cost
        # of a wrong False is running a net twice, and the cost of a wrong True is
        # not running it at all. Those don't weigh the same.
        try:
            settings = json.loads(self.settings_path.read_text())
            entries = (settings.get("hooks") or {}).get("SessionStart") or []
            return any(
                script_name in (hook.get("command") or "")
                for entry in entries
                for hook in (entry.get("hooks") or [])
            )
        except (OSError, ValueError, AttributeError, TypeError):
            return False

    def missing_binary_message(self) -> str:
        return "Error: 'claude' command not found. Install Claude Code and ensure it's on PATH."

    def did_nothing_hint(self) -> str:
        return (
            "`claude -p` either could not authenticate (check ANTHROPIC_API_KEY) or, "
            "on a resume, failed to load the session it was given"
        )


ENGINES: dict[str, type[Engine]] = {"claude": ClaudeEngine}
DEFAULT_ENGINE = "claude"


def get_engine(name: str | None = None) -> Engine:
    key = (name or DEFAULT_ENGINE).strip().lower()
    if key not in ENGINES:
        raise ValueError(f"unknown engine {name!r}; choose one of: {', '.join(sorted(ENGINES))}")
    return ENGINES[key]()
