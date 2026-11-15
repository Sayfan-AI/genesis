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
import os
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

    # Whether the agent's own CLI stops a session at `max_turns`. When it
    # doesn't, the plane counts tool calls and stops the session itself, then
    # reports it as an exhausted budget so the continuation ladder treats it
    # exactly like Claude's `error_max_turns`.
    enforces_turn_budget: bool = True
    # Whether the session must be started with stdin closed. Some CLIs wait for
    # stdin to reach EOF before starting, even with the prompt as an argument.
    close_stdin: bool = False
    # Spending bounds in this engine's unit, and the env vars that override them.
    task_ceiling_default: float = 0.0
    task_ceiling_env: tuple[str, ...] = ()
    run_budget_default: float = 0.0
    run_budget_env: tuple[str, ...] = ()

    def settle_cost(self, session_id: str | None) -> float | None:
        """Cost of the process that just ended, for engines that can't report it
        in the stream. None means the stream's `SessionResult`s already carried it."""
        return None


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
    # Cost bounds in dollars. The reasoning behind both numbers lives with their
    # constants in server.py (TASK_COST_CEILING_USD, RUN_COST_BUDGET_USD).
    task_ceiling_default = 50.0
    # GENESIS_COST_CEILING is still read, second, because that is the name every
    # existing operator config uses, and silently ignoring it would loosen a
    # bound somebody had deliberately tightened.
    task_ceiling_env = ("GENESIS_TASK_COST_CEILING", "GENESIS_COST_CEILING")
    run_budget_default = 150.0
    run_budget_env = ("GENESIS_RUN_COST_BUDGET",)

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


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------
#
# Measured on codex-cli 0.166.0 before any of this was written (genesis issue
# #146), because each of these differs from Claude Code in a way the plane cares
# about:
#
# - `codex exec` waits for stdin to reach EOF before starting, even when the
#   prompt is an argument. Launched with stdin inherited, it sat for five
#   minutes printing "Reading additional input from stdin...". So stdin is
#   closed.
# - There is no `--max-turns`. The plane counts tool calls and stops the
#   session itself. A session killed with SIGTERM after two of five steps
#   resumed with `codex exec resume <id>` and finished steps 3 to 5 without
#   repeating any, which is exactly what the continuation ladder needs.
# - Codex's sandbox makes `.git` read-only and turns the network off, so an
#   unattended session couldn't commit, push or run `gh`. Sessions run with it
#   off, the same machine reach a Claude session has in local mode, and
#   host-guard.sh still guards through the hook.
# - Project hooks in `.codex/hooks.json` only load headless with
#   `--dangerously-bypass-hook-trust`. Their PreToolUse payload matches Claude
#   Code's (`tool_name: "Bash"`, `tool_input.command`), so the seeded guards
#   work unchanged, and exit 2 blocks the command before it runs.
# - Usage is tokens, never dollars, and `turn.completed.usage` is the running
#   total for the whole session, not the turn: a resumed turn reported 142,552
#   input tokens, of which its own share was 33,307. A session the plane kills
#   reports no usage in the stream at all. So cost is settled from Codex's own
#   session log after the process ends, as the new total minus the last total
#   seen for that session.
# - `error` items in the stream are warnings (user-config notices, the
#   hook-trust bypass notice), not failures. Only `turn.failed` and a top-level
#   `error` event end a session abnormally.

# The flags every unattended session runs with. See the notes above.
CODEX_UNATTENDED_FLAGS = (
    "--json",
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-bypass-hook-trust",
)


def codex_tokens(usage: dict | None) -> float:
    """The token count the Codex bounds are measured in: fresh input plus output.

    Cached input is left out. Each request re-sends the whole conversation, so
    counting cached tokens would mostly measure how long the session has been
    running rather than how much work it did. The first measured session read
    109,245 input tokens, 76,032 of them cached, for a task of four commands.
    """
    if not isinstance(usage, dict):
        return 0.0
    fresh = (usage.get("input_tokens") or 0) - (usage.get("cached_input_tokens") or 0)
    return float(max(fresh, 0) + (usage.get("output_tokens") or 0))


def _unwrap_shell(command: str) -> str:
    """Codex reports `/bin/zsh -lc 'git status'`. The feed wants `git status`."""
    for prefix in ("/bin/zsh -lc ", "/bin/bash -lc ", "bash -lc ", "zsh -lc ", "/bin/sh -c ", "sh -c "):
        if command.startswith(prefix):
            inner = command[len(prefix):].strip()
            if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "'\"":
                inner = inner[1:-1]
            return inner
    return command


@dataclass
class CodexStreamParser(StreamParser):
    """`codex exec --json`, one event per line.

    Tool calls are counted on `item.completed`, not `item.started`, so a call is
    counted once. A command a hook blocked never appears as an item at all, so it
    isn't counted against the budget.
    """

    def feed(self, event: dict) -> list[SessionEvent]:
        kind = event.get("type")
        if kind == "thread.started" and event.get("thread_id"):
            return [SessionStarted(event["thread_id"])]
        if kind == "item.completed":
            tool = self._tool(event.get("item") or {})
            return [ToolCall(tool)] if tool else []
        if kind == "turn.completed":
            # Cost is 0 here on purpose: the usage on this event is the session's
            # running total, and the plane settles the real figure from the
            # session log once the process ends (Engine.settle_cost).
            return [SessionResult("success")]
        if kind == "turn.failed":
            return [SessionResult("error_during_execution", is_error=True)]
        if kind == "error":
            return [SessionResult("error_during_execution", is_error=True)]
        return []

    @staticmethod
    def _tool(item: dict) -> str | None:
        kind = item.get("type")
        if kind == "command_execution":
            return f"Bash {brief({'command': _unwrap_shell(str(item.get('command') or ''))})}"
        if kind == "file_change":
            paths = [c.get("path", "") for c in item.get("changes") or [] if isinstance(c, dict)]
            return f"Edit {brief({'path': ', '.join(paths)})}"
        if kind == "mcp_tool_call":
            return f"{item.get('server')}.{item.get('tool')}"
        if kind == "web_search":
            return f"WebSearch {brief({'query': str(item.get('query') or '')})}"
        if kind == "collab_tool_call":
            return f"Agent {item.get('tool') or ''}".strip()
        return None


class CodexEngine(Engine):
    name = "codex"
    binary = "codex"
    cost_unit = "tokens"
    hooks_path = Path(".codex/hooks.json")
    enforces_turn_budget = False
    close_stdin = True
    # Token bounds, counted as fresh input plus output (codex_tokens). These are
    # provisional: they're sized from the first measured sessions and will be
    # revisited once a dev system has run on Codex for real (#146). An operator
    # tightens them through the env vars.
    task_ceiling_default = 3_000_000.0
    task_ceiling_env = ("GENESIS_TASK_TOKEN_CEILING",)
    run_budget_default = 10_000_000.0
    run_budget_env = ("GENESIS_RUN_TOKEN_BUDGET",)

    def __init__(self) -> None:
        # The last running total seen per session, so a resumed session is
        # charged only for what it added.
        self._totals: dict[str, float] = {}

    def session_command(
        self, prompt: str, *, resume: str | None, max_turns: int, tools: str
    ) -> list[str]:
        # `max_turns` and `tools` have no Codex flag. The plane enforces the
        # budget, and the tool grant is the sandbox setting in the flags.
        if resume:
            return [self.binary, "exec", "resume", resume, *CODEX_UNATTENDED_FLAGS, self.resume_prompt]
        return [self.binary, "exec", *CODEX_UNATTENDED_FLAGS, prompt or ""]

    def judge_command(self, prompt: str, *, max_turns: int) -> list[str]:
        # Read-only: the judge reads the evidence it's handed and changes
        # nothing. Ephemeral, so a judge leaves no session behind to resume.
        return [self.binary, "exec", "--json", "--sandbox", "read-only", "--ephemeral", prompt]

    def stream_parser(self) -> StreamParser:
        return CodexStreamParser()

    def read_judge_output(self, stdout: str | None) -> JudgeReply:
        text, usage = "", None
        for line in (stdout or "").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            item = event.get("item") or {}
            if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                text = str(item.get("text") or "").strip()
            elif event.get("type") == "turn.completed":
                usage = event.get("usage")
        if usage is None:
            return JudgeReply(text or (stdout or "").strip(), 0.0, cost_known=False)
        # An ephemeral session has one turn, so its running total is its own.
        return JudgeReply(text, codex_tokens(usage))

    def format_cost(self, cost: float) -> str:
        return f"{cost:,.0f} tokens"

    def settle_cost(self, session_id: str | None) -> float | None:
        if not session_id:
            return 0.0
        total = self._family_total(session_id)
        if total is None:
            return 0.0
        added = max(total - self._totals.get(session_id, 0.0), 0.0)
        self._totals[session_id] = total
        return added

    @staticmethod
    def _sessions_dir() -> Path:
        return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"

    @classmethod
    def _family_total(cls, session_id: str) -> float | None:
        """The session's running total plus every subagent it spawned, at any depth.

        A subagent writes its own session log, linked to its parent only by
        `parent_thread_id` in that log's first line, and its tokens aren't in the
        parent's total. Measured on the first trial session (#146): the parent's
        log said 68,301 tokens and its human-interaction subagent's said 15,721
        more. Counting only the parent would undercount every session that
        delegates.
        """
        logs = cls._recent_logs()
        own = next((path for sid, (path, _parent) in logs.items() if sid == session_id), None)
        if own is None:
            return None
        children: dict[str, list[str]] = {}
        for sid, (_path, parent) in logs.items():
            if parent:
                children.setdefault(parent, []).append(sid)
        total, queue, seen = 0.0, [session_id], set()
        while queue:
            sid = queue.pop()
            if sid in seen:
                continue
            seen.add(sid)
            total += cls._log_total(logs[sid][0]) or 0.0
            queue += children.get(sid, [])
        return total

    @classmethod
    def _recent_logs(cls) -> dict[str, tuple[Path, str | None]]:
        """Session id -> (log path, parent id) for logs touched in the last three
        days. The window keeps this from reading months of history on every
        settle, and clears any session the plane could still be accounting for:
        the plane's own deadline is an hour."""
        import time

        cutoff = time.time() - 3 * 86400
        out: dict[str, tuple[Path, str | None]] = {}
        try:
            paths = list(cls._sessions_dir().glob("*/*/*/rollout-*.jsonl"))
        except OSError:
            return out
        for path in paths:
            try:
                if path.stat().st_mtime < cutoff:
                    continue
                with path.open() as f:
                    meta = (json.loads(f.readline() or "{}").get("payload") or {})
            except (OSError, ValueError):
                continue
            sid = meta.get("id") or meta.get("session_id")
            if not isinstance(sid, str):
                continue
            spawn = ((meta.get("source") or {}).get("subagent") or {}).get("thread_spawn") or {} \
                if isinstance(meta.get("source"), dict) else {}
            parent = meta.get("parent_thread_id") or spawn.get("parent_thread_id")
            out[sid] = (path, parent if isinstance(parent, str) else None)
        return out

    @staticmethod
    def _log_total(path: Path) -> float | None:
        """The running total from the last `token_count` event in one session log."""
        total = None
        try:
            with path.open() as f:
                for line in f:
                    if '"token_count"' not in line:
                        continue
                    try:
                        info = (json.loads(line).get("payload") or {}).get("info") or {}
                    except ValueError:
                        continue
                    if info.get("total_token_usage"):
                        total = codex_tokens(info["total_token_usage"])
        except OSError:
            return None
        return total

    def pre_session_hook_declared(self, script_name: str) -> bool:
        # Same hooks shape as Claude Code's settings.json, and the same reasoning:
        # anything unreadable answers False, because running a net twice is the
        # cheaper mistake.
        try:
            hooks = json.loads(self.hooks_path.read_text())
            entries = (hooks.get("hooks") or {}).get("SessionStart") or []
            return any(
                script_name in (hook.get("command") or "")
                for entry in entries
                for hook in (entry.get("hooks") or [])
            )
        except (OSError, ValueError, AttributeError, TypeError):
            return False

    def missing_binary_message(self) -> str:
        return "Error: 'codex' command not found. Install the Codex CLI and ensure it's on PATH."

    def did_nothing_hint(self) -> str:
        return (
            "`codex exec` either could not authenticate (run `codex login`, or set "
            "CODEX_API_KEY) or, on a resume, failed to load the session it was given"
        )


ENGINES: dict[str, type[Engine]] = {"claude": ClaudeEngine, "codex": CodexEngine}
DEFAULT_ENGINE = "claude"


def get_engine(name: str | None = None) -> Engine:
    key = (name or DEFAULT_ENGINE).strip().lower()
    if key not in ENGINES:
        raise ValueError(f"unknown engine {name!r}; choose one of: {', '.join(sorted(ENGINES))}")
    return ENGINES[key]()
