"""Local orchestrator server (local control plane).

Runs the orchestrator agent locally, polling GitHub repo events and launching
fresh Claude sessions when relevant activity is detected. Disables GitHub
Actions workflows on start to prevent duplicate execution; re-enables them on
graceful shutdown.

Authentication uses the user's existing `gh` CLI auth (`gh auth token`) for
GitHub, and whatever the local `claude` CLI is already logged in with for the
model — a Claude subscription works, so no ANTHROPIC_API_KEY is required. That
is the main practical reason to prefer local mode: GitHub Actions needs a
credential in the repo, this needs none.

Configuration (environment variables):
    GENESIS_POLL_INTERVAL    seconds between polls (default: 60)
    GENESIS_SESSION_TIMEOUT  max seconds per orchestrator session (default: 3600)
    GENESIS_REPO             owner/repo (default: detected from git remote)
    GENESIS_AGENT            agent definition to run (default: the orchestrator)
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from genesis.appauth import mint_installation_token
from genesis.workflows import (
    DISABLED_LIST_PATH,
    disable_workflows,
    enable_workflows,
    tracked_all_disabled,
)

LOCK_PATH = Path(".genesis/.orchestrator.lock")
ETAG_PATH = Path(".genesis/.poll-etag")
HIGHWATER_PATH = Path(".genesis/.poll-highwater")

RELEVANT_EVENT_TYPES = frozenset(
    {"IssuesEvent", "IssueCommentEvent", "PullRequestEvent"}
)

DEFAULT_AGENT = ".claude/agents/orchestrator.md"

# Where the agent's own Claude Code profile lives. A local session otherwise
# inherits the operator's ~/.claude/CLAUDE.md, which is written as guidance for a
# human's assistant, not as policy for an autonomous system — and the agent obeys
# it. Observed on MaKlaude: a personal convention to qualify GitHub references
# ("issue #117") produced `Closes issue #117` in a PR body, which GitHub does not
# parse, so a merged PR silently left its task issue open. Rules like "never merge
# PRs" or "always commit with the gcm alias" land in the same lap.
#
# An isolated profile still loads the repo's own CLAUDE.md and .claude/settings.json
# (hooks included), so project memory and activity logging are unaffected. It only
# drops the operator's personal layer, which also makes a local run behave the same
# as the GitHub Actions run, where no such file exists.
DEFAULT_AGENT_HOME = Path.home() / ".config" / "genesis" / "claude-home"

# Local mode runs the same agent as the GHA workflows and must respect the same
# turn-budget floor (see ORCHESTRATOR_TURN_FLOOR in scaffold.py). This was 20 —
# below the floor — because the floor guard only inspected workflow templates,
# so local mode quietly kept the budget that had already killed two runs.
# Enforced by tests/unit/test_server.py.
SESSION_MAX_TURNS = 40

# Hard backstop on continuations. It is deliberately generous, because it is no
# longer the mechanism that decides when to stop — it exists so a bug in the
# decision logic cannot loop forever. See _should_continue for the real ladder.
MAX_CONTINUATIONS = 6

# The ceiling that actually binds. A turn count measures effort spent, not work
# completed, which is why raising it never helped: 20 died, 40 died, 60 died, and
# a single task was observed spending $10.09 across three sessions while making
# real progress the whole time. Dollars are what an operator actually wants to
# bound. Env: GENESIS_COST_CEILING.
COST_CEILING_USD = 15.0

# Budget for the judge itself. It reads evidence handed to it and answers with one
# word, so it needs no tools and almost no turns.
JUDGE_MAX_TURNS = 2

# `Write` is required: without it the agent can edit existing files but cannot
# create any, so any task needing a new file, test, or agent definition is
# impossible to satisfy.
ALLOWED_TOOLS = "Read,Write,Edit,Bash,Glob,Grep,Agent"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log(msg: str) -> None:
    print(f"[{_now_iso()}] {msg}", flush=True)


def _gh_token() -> str:
    result = subprocess.run(
        ["gh", "auth", "token"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _get_repo() -> str:
    env = os.environ.get("GENESIS_REPO")
    if env:
        return env
    result = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner"],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)["nameWithOwner"]


def is_bot_actor(actor_login: str) -> bool:
    return actor_login.endswith("[bot]") or actor_login == "github-actions"


def filter_relevant_events(
    events: list[dict], last_event_id: str | None
) -> list[dict]:
    """Filter raw GitHub events to those that should trigger the orchestrator.

    - Drops bot events (no feedback loops).
    - Keeps only IssuesEvent / IssueCommentEvent / PullRequestEvent.
    - Stops at last_event_id (high-water mark).
    - Returns events in chronological order (oldest first).
    """
    new_events = []
    for event in events:
        if last_event_id is not None and event.get("id") == last_event_id:
            break
        if event.get("type") not in RELEVANT_EVENT_TYPES:
            continue
        actor = event.get("actor", {}).get("login", "")
        if is_bot_actor(actor):
            continue
        new_events.append(event)
    new_events.reverse()
    return new_events


@dataclass
class PollResult:
    events: list[dict]
    etag: str | None
    not_modified: bool


def fetch_events(repo: str, etag: str | None, token: str) -> PollResult:
    """Fetch repo events. Returns 304 (not_modified=True) when ETag matches.

    Single page (max 100 events). If more than 100 events arrive between polls,
    older events on subsequent pages are missed. `poll_once` logs a warning when
    the previous high-water mark isn't visible in the returned page so the user
    can shorten `--poll-interval`.
    """
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/events?per_page=100",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "genesis-local-control-plane",
        },
    )
    if etag:
        req.add_header("If-None-Match", etag)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            new_etag = resp.headers.get("ETag")
            body = resp.read()
            events = json.loads(body) if body else []
            return PollResult(events=events, etag=new_etag, not_modified=False)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return PollResult(events=[], etag=etag, not_modified=True)
        raise


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return None


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def resolve_claude_home(env: dict[str, str] | None = None) -> Path | None:
    """Decide which Claude Code profile agent sessions should use.

    Returns the config dir to isolate into, or None to inherit the operator's
    personal profile.

    The profile is opt-out, not opt-in: isolation is the right default for an
    autonomous agent, and an operator who wants their own settings and memory in
    the loop says so explicitly with GENESIS_CLAUDE_PROFILE=personal.

    One step cannot be automated. Claude Code scopes its keychain credential to
    the config-dir path, so a fresh profile is unauthenticated until someone runs
    `claude` in it once and logs in — verified by elimination: symlinking the
    credentials file, copying the account record, and even symlinking all 36
    entries of the real profile all still report "Not logged in". So when the
    profile isn't set up yet we print the one command needed and fall back to the
    personal profile rather than refusing to run. A missing profile should cost a
    warning, not a stalled dev system.
    """
    env = os.environ if env is None else env

    if env.get("GENESIS_CLAUDE_PROFILE", "").strip().lower() == "personal":
        return None

    home = Path(env.get("GENESIS_CLAUDE_HOME") or DEFAULT_AGENT_HOME).expanduser()

    # `.claude.json` is written when a profile is first authenticated, so its
    # presence is the cheap "has anyone logged in here" check. Probing for real
    # would cost a model call on every session.
    if (home / ".claude.json").is_file():
        return home

    log(f"Agent Claude profile not set up at {home} — using your personal profile for now.")
    log("  One-time setup, so agent sessions stop inheriting your ~/.claude/CLAUDE.md:")
    log(f"    CLAUDE_CONFIG_DIR={home} claude   # then /login, then exit")
    log("  Silence this by choosing the personal profile: GENESIS_CLAUDE_PROFILE=personal")
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log(f"  (could not create {home}: {e})")
    return None


def _project_name() -> str:
    """Project label, matching what log.sh derives, so hook lines and outcome
    lines land in the same stream namespace and can be joined in one query."""
    path = Path(".genesis/config.toml")
    try:
        for line in path.read_text().splitlines():
            if line.startswith("name"):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "unknown"


def loki_push(hook_event: str, fields: dict[str, object]) -> bool:
    """Ship one logfmt line to Loki. Best-effort, never raises.

    Session outcomes — how a run ended, what it cost, how many turns it burned —
    previously existed only as stdout in whoever's terminal was running `serve`.
    Close the window and the record was gone, which made the most decision-
    relevant telemetry the system produces the one thing it could not query.

    Labels stay low-cardinality (project, hook_event, service_name); everything
    else is a logfmt field, promoted at query time with `| logfmt`.
    """
    url = os.environ.get("GENESIS_LOKI_URL", "").strip()
    if not url:
        return False

    ns = time.time_ns()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ns // 1_000_000_000))
    ordered = {"ts": f"{stamp}.{ns % 1_000_000_000 // 1_000_000:03d}Z", "event": hook_event}
    ordered.update({k: v for k, v in fields.items() if v is not None})

    def fmt(value: object) -> str:
        text = str(value)
        return json.dumps(text) if any(c in text for c in ' "=\\') else text

    line = " ".join(f"{k}={fmt(v)}" for k, v in ordered.items())
    project = _project_name()
    payload = json.dumps(
        {
            "streams": [
                {
                    "stream": {
                        "project": project,
                        "hook_event": hook_event,
                        "service_name": project,
                    },
                    "values": [[str(ns), line]],
                }
            ]
        }
    ).encode()

    req = urllib.request.Request(
        f"{url.rstrip('/')}/loki/api/v1/push",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    user = os.environ.get("GENESIS_LOKI_USER", "")
    token = os.environ.get("GENESIS_LOKI_TOKEN", "")
    if user and token:
        import base64

        cred = base64.b64encode(f"{user}:{token}".encode()).decode()
        req.add_header("Authorization", f"Basic {cred}")
    try:
        with urllib.request.urlopen(req, timeout=5):
            return True
    except Exception:  # noqa: BLE001 - telemetry must never break the run
        return False


def _died_mid_task(subtype: str | None) -> bool:
    """True for any abnormal session ending, not just an exhausted budget.

    This started as an `error_max_turns` check and that was too narrow. Observed
    in production: a session died `error_during_execution` at turn 41 with real
    work uncommitted in the tree, and the chain stopped because the subtype did
    not match - $6.63 spent and the task left stranded for no better reason than
    a string comparison.

    Every abnormal ending has the same shape: reasoning in a transcript on disk,
    work in the tree, nobody continuing it. Whether resuming is *wise* is not this
    function's business - that is what the evidence ladder in _should_continue is
    for, and its zero-tool-calls rung already stops a session that fails instantly
    and repeatedly.
    """
    return bool(subtype) and subtype != "success"


def _cost_ceiling() -> float:
    """Spend allowed per unit of work before continuations stop, whatever anyone
    thinks. Env override so an operator can tighten it without editing code."""
    raw = os.environ.get("GENESIS_COST_CEILING", "").strip()
    try:
        return float(raw) if raw else COST_CEILING_USD
    except ValueError:
        return COST_CEILING_USD


def _git(args: list[str]) -> str:
    """Run a read-only git command, returning "" on any failure."""
    try:
        out = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=15, check=False
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def repo_fingerprint() -> str:
    """A cheap hash of what the repo actually looks like right now.

    This is the evidence the continuation decision rests on, and it is deliberately
    not the agent's own account of what it did. Within one hour we watched a worker
    report it had not merged a PR it had merged, and an orchestrator conclude that
    auto-merge had closed an issue a human closed by hand. Self-reports are a
    narrative; HEAD and the working tree are facts.
    """
    return hashlib.sha256(
        "\n".join([_git(["rev-parse", "HEAD"]), _git(["status", "--porcelain"])]).encode()
    ).hexdigest()[:16]


def _brief(tool_input: object, limit: int = 88) -> str:
    """Render a tool's input as one short line for the progress feed.

    Prefers the field that says what the call is actually doing — a command, a
    path, a pattern — and falls back to a truncated repr.
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


def _build_prompt(event: dict | None, agent: str = DEFAULT_AGENT) -> str:
    if event is None:
        return (
            f"Run the agent defined in {agent}. "
            "This is a local control plane initial run — assess project state and advance work."
        )
    event_type = event.get("type", "UnknownEvent")
    action = event.get("payload", {}).get("action", "unknown")
    actor = event.get("actor", {}).get("login", "unknown")
    return (
        f"Run the agent defined in {agent}.\n\n"
        "An event triggered this run:\n"
        f"- Event: {event_type} / {action}\n"
        f"- Actor: {actor}\n\n"
        "Assess this event in context of the project state and take appropriate action."
    )


@dataclass
class LocalControlPlane:
    repo: str
    poll_interval: int = 60
    session_timeout: int = 3600
    agent: str = DEFAULT_AGENT
    claude_home: Path | None = None
    # Written by the progress reader, read by the continuation loop after wait().
    last_session_id: str | None = None
    last_result_subtype: str | None = None
    last_tool_calls: int = 0
    last_cost: float = 0.0
    recent_tools: list[str] = field(default_factory=list)
    pending_followup: bool = False
    continuation_index: int = 0
    identity_logged: bool = False
    all_workflows: bool = False
    shutdown: bool = False
    last_event_id: str | None = None
    etag: str | None = None
    orch_proc: subprocess.Popen | None = field(default=None, repr=False)

    # ----- lock -----

    def acquire_lock(self) -> bool:
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOCK_PATH.exists():
            existing = _read_text(LOCK_PATH) or ""
            try:
                pid = int(existing)
                os.kill(pid, 0)
                return False  # another instance alive
            except (ValueError, ProcessLookupError, PermissionError):
                log(f"Stale lock file (pid {existing!r}), removing")
                LOCK_PATH.unlink(missing_ok=True)
        LOCK_PATH.write_text(str(os.getpid()))
        return True

    def release_lock(self) -> None:
        LOCK_PATH.unlink(missing_ok=True)

    # ----- state persistence -----

    def load_state(self) -> None:
        self.etag = _read_text(ETAG_PATH)
        self.last_event_id = _read_text(HIGHWATER_PATH)

    def save_state(self) -> None:
        if self.etag is not None:
            _write_text(ETAG_PATH, self.etag)
        if self.last_event_id is not None:
            _write_text(HIGHWATER_PATH, self.last_event_id)

    # ----- orchestrator -----

    def _kill_orch(self) -> None:
        proc = self.orch_proc
        if proc is None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()

    def _stream_progress(self, stream) -> None:
        """Print one compact line per tool call from claude's stream-json output.

        Best-effort by construction: any parse failure, unexpected shape, or
        non-iterable stream ends the reader quietly. Progress reporting must
        never be able to take down the run it is reporting on.
        """
        try:
            turns = 0
            for raw in stream:  # noqa: PLR1702
                line = (raw or "").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                kind = event.get("type")
                # Every event carries the session id; the init event is simply the
                # first. Capturing it is what makes --resume possible at all.
                session_id = event.get("session_id")
                if session_id and not self.last_session_id:
                    self.last_session_id = session_id
                if kind == "assistant":
                    content = (event.get("message") or {}).get("content") or []
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_use":
                            turns += 1
                            summary = f"{block.get('name')} {_brief(block.get('input'))}"
                            log(f"  {turns:>3}. {summary}")
                            # Kept for the judge: what it actually did, not what it
                            # says it did. Bounded so a long session can't grow this
                            # without limit.
                            self.recent_tools.append(summary)
                            del self.recent_tools[:-12]
                elif kind == "result":
                    self.last_result_subtype = event.get("subtype")
                    loki_push(
                        "session-outcome",
                        {
                            "level": "error" if event.get("is_error") else "info",
                            "subtype": event.get("subtype"),
                            "turns": event.get("num_turns"),
                            "cost_usd": round(float(event.get("total_cost_usd") or 0), 4),
                            "duration_s": round((event.get("duration_ms") or 0) / 1000),
                            "tool_calls": turns,
                            "session": event.get("session_id"),
                            "continuation": self.continuation_index,
                        },
                    )
                    self.last_tool_calls = turns
                    self.last_cost = float(event.get("total_cost_usd") or 0)
                    if event.get("session_id"):
                        self.last_session_id = event["session_id"]
                    cost = event.get("total_cost_usd") or 0
                    secs = round((event.get("duration_ms") or 0) / 1000)
                    log(
                        f"  session ended: {event.get('subtype')} "
                        f"turns={event.get('num_turns')} cost=${cost:.2f} {secs}s"
                    )
        except Exception:  # noqa: BLE001 - reporting must never break the run
            return

    def run_orchestrator(self, event: dict | None) -> int:
        """Run one unit of work, continuing across budget deaths.

        A session that dies at `error_max_turns` has not failed at the task — it
        ran out of turns mid-thought, leaving its reasoning in a transcript on
        disk and its work uncommitted in the tree. Starting over throws away the
        first, and forces the next session to re-derive intent from the second.
        `claude --resume` carries both forward with a fresh budget, which is the
        "batches of N turns" shape rather than one ever-larger ceiling: raising
        the cap moved the wall (20 died, 40 died, 60 died), it never removed it.

        Bounded three ways, because an unbounded retry loop is a way to spend
        money in your sleep: a hard continuation cap, an overall deadline shared
        by every attempt, and a stop as soon as an attempt does nothing at all.
        """
        prompt = _build_prompt(event, self.agent)
        if event is None:
            log("Launching orchestrator (initial run)")
        else:
            event_type = event.get("type", "?")
            action = event.get("payload", {}).get("action", "?")
            event_id = event.get("id", "?")
            log(f"Launching orchestrator for {event_type}/{action} (id={event_id})")

        # One deadline for the whole chain, not per attempt — otherwise three
        # continuations could quietly run for three times the configured timeout.
        deadline = time.time() + self.session_timeout

        task = prompt.splitlines()[0] if prompt else "the current unit of work"
        self.continuation_index = 0
        before = repo_fingerprint()
        rc = self._run_session(prompt, deadline)
        spent = self.last_cost

        for attempt in range(1, MAX_CONTINUATIONS + 1):
            if self.shutdown or not _died_mid_task(self.last_result_subtype):
                break
            session_id = self.last_session_id
            if not session_id:
                log("  hit max turns but no session id was reported — cannot resume")
                break
            if time.time() > deadline:
                log("  session deadline reached — not continuing")
                break

            go, why = self._should_continue(task, before, spent)
            loki_push(
                "continuation-decision",
                {
                    "level": "info" if go else "warn",
                    "decision": "continue" if go else "stop",
                    "reason": why,
                    "spent_usd": round(spent, 4),
                    "attempt": attempt,
                    "session": session_id,
                },
            )
            if not go:
                log(f"  not continuing: {why}")
                break

            log(f"  hit max turns; resuming {session_id[:8]} "
                f"(continuation {attempt}, ${spent:.2f} spent) — {why}")
            before = repo_fingerprint()
            self.continuation_index = attempt
            rc = self._run_session(None, deadline, resume=session_id)
            spent += self.last_cost

        if _died_mid_task(self.last_result_subtype):
            # The work is real and uncommitted, and nothing else is scheduled to
            # touch it. Without this flag the plane would sit idle holding a
            # half-finished task until some unrelated repo event happened along.
            self.pending_followup = True
            log(f"  still incomplete (${spent:.2f} spent) — will pick it up on the next tick")
        return rc

    def _session_env(self) -> dict[str, str]:
        """Environment for a child `claude` process.

        One helper for both the agent and the judge, because the two used to
        build this separately and drifted: an edit meant for the agent landed in
        the judge, so the judge authenticated as the App while the agent it was
        judging still ran as the operator.
        """
        env = dict(os.environ)
        if self.claude_home is not None:
            env["CLAUDE_CONFIG_DIR"] = str(self.claude_home)

        # Act as the GitHub App, like the Actions path does, so the agent is a
        # distinguishable identity rather than a second copy of the operator.
        # Minted per session: installation tokens last an hour and a plane that
        # runs all afternoon would otherwise hold a dead one.
        app_token = mint_installation_token(self.repo, env)

        # The App private key is far stronger than the hour-long token minted
        # from it - it can mint tokens for every repo the App is installed on, at
        # any time. The plane needs it, a session never does.
        for secret in ("GENESIS_GITHUB_APP_SECRET", "GENESIS_GITHUB_APP_ID"):
            env.pop(secret, None)

        if app_token:
            env["GH_TOKEN"] = app_token
            env["GITHUB_TOKEN"] = app_token
            if not self.identity_logged:
                log("  agent authenticates as the Genesis App, not your account")
                self.identity_logged = True
        elif not self.identity_logged:
            log("  agent uses your personal gh credential - its commits will look like yours")
            self.identity_logged = True
        return env

    def ask_judge(self, task: str) -> tuple[bool, str]:
        """Ask a fresh session whether a stalled run deserves another continuation.

        Separate session, no shared context, and it is handed evidence rather than
        the previous session's summary. It is framed to justify *stopping*: a judge
        asked "may this continue?" tends to say yes, and the expensive mistake here
        is continuing, not halting.

        Fails closed. Any error, timeout, or unrecognised answer stops the chain,
        because the failure mode of a broken judge should be an idle dev system,
        not an open-ended spend.
        """
        evidence = "\n".join(
            [
                f"Task: {task}",
                "",
                "Uncommitted changes (git status --porcelain):",
                _git(["status", "--porcelain"]) or "(none)",
                "",
                "Diff stat (git diff --stat):",
                _git(["diff", "--stat"]) or "(none)",
                "",
                "Recent commits:",
                _git(["log", "--oneline", "-3"]) or "(none)",
                "",
                "What the last session actually did, most recent last:",
                "\n".join(f"  - {t}" for t in self.recent_tools) or "  (nothing)",
            ]
        )
        prompt = (
            "You are judging whether an autonomous coding session that ran out of "
            "turns should be given another one. It has already been resumed at "
            "least once.\n\n"
            "Default to STOP. Answer CONTINUE only if the evidence shows the "
            "session converging on a finish — edits narrowing toward a specific "
            "goal, tests being fixed, a diff that is coherent and nearly done. "
            "Answer STOP if it looks like it is thrashing: re-reading the same "
            "files, re-editing the same lines, broadening scope, or producing no "
            "durable change.\n\n"
            f"{evidence}\n\n"
            "Reply with exactly one word, CONTINUE or STOP, then a single short "
            "sentence of justification on the same line."
        )

        cmd = [
            "claude",
            "-p",
            prompt,
            "--max-turns",
            str(JUDGE_MAX_TURNS),
            "--allowedTools",
            "",
        ]
        child_env = self._session_env()
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True, timeout=180, env=child_env, check=False
            )
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"judge unavailable ({e})"

        answer = (out.stdout or "").strip()
        head = answer.upper()[:40]
        if head.startswith("CONTINUE"):
            return True, answer.splitlines()[0][:160]
        if head.startswith("STOP"):
            return False, answer.splitlines()[0][:160]
        return False, f"judge gave no clear verdict: {answer.splitlines()[0][:120] if answer else '(empty)'}"

    def _should_continue(
        self, task: str, before: str, spent: float
    ) -> tuple[bool, str]:
        """Decide whether to resume, cheapest evidence first.

        A model is only consulted for the genuinely ambiguous case — work was done
        but nothing landed — because every other rung is answerable by git or by a
        counter, and paying for an opinion you can compute is waste.
        """
        ceiling = _cost_ceiling()
        if spent >= ceiling:
            return False, f"cost ceiling reached (${spent:.2f} >= ${ceiling:.2f})"
        if self.last_tool_calls == 0:
            return False, "the attempt made no tool calls"
        if repo_fingerprint() != before:
            return True, "work landed in the repo"
        return self.ask_judge(task)

    def _run_session(
        self, prompt: str | None, deadline: float, resume: str | None = None
    ) -> int:
        """Launch one `claude -p` session and wait for it, streaming progress."""
        self.last_result_subtype = None
        self.last_tool_calls = 0

        cmd = ["claude", "-p"]
        if resume:
            cmd += [
                "--resume",
                resume,
                # A resumed session already holds the task, the plan, and what it
                # has done. Restating the original prompt would invite it to start
                # the work over rather than finish it.
                "Continue the work you were doing. You ran out of turns, not out of task.",
            ]
        else:
            cmd.append(prompt or "")
        cmd += [
            "--max-turns",
            str(SESSION_MAX_TURNS),
            "--allowedTools",
            ALLOWED_TOOLS,
            # Without this, `claude -p` buffers everything until the session ends,
            # so a 25-minute run prints nothing at all — and hook stderr doesn't
            # help, because Claude Code captures it into its own transcript rather
            # than passing it through. Streaming turns a silent box into a feed.
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        child_env = self._session_env()

        try:
            self.orch_proc = subprocess.Popen(
                cmd,
                start_new_session=True,
                stdout=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=child_env,
            )
        except FileNotFoundError:
            log("Error: 'claude' command not found. Install Claude Code and ensure it's on PATH.")
            return 127

        # A piped stdout MUST be drained or the child blocks once the pipe fills.
        # Daemon thread so a wedged reader can never hold up shutdown.
        reader = threading.Thread(
            target=self._stream_progress, args=(self.orch_proc.stdout,), daemon=True
        )
        reader.start()

        try:
            while True:
                try:
                    return self.orch_proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    if self.shutdown:
                        log("Shutdown requested — terminating orchestrator")
                        self._kill_orch()
                        return -2
                    if time.time() > deadline:
                        log(f"Session timeout ({self.session_timeout}s total) — terminating orchestrator")
                        self._kill_orch()
                        return -1
        finally:
            # The process exiting does not mean its output has been parsed. Without
            # this join the continuation loop can read last_result_subtype before
            # the reader has seen the terminal `result` event, and a budget death
            # looks like a clean finish. Bounded so a wedged reader can't hang the
            # plane — the progress feed is never allowed to block the run.
            reader.join(timeout=10)
            self.orch_proc = None

    # ----- main loop -----

    def _interruptible_sleep(self, seconds: float) -> None:
        deadline = time.time() + seconds
        while not self.shutdown and time.time() < deadline:
            remaining = deadline - time.time()
            time.sleep(min(0.5, max(0.0, remaining)))

    def poll_once(self, token: str) -> list[dict]:
        result = fetch_events(self.repo, self.etag, token)
        if result.not_modified:
            return []
        if result.etag:
            self.etag = result.etag
        events = result.events
        if not events:
            return []
        # If the previous high-water mark is set but isn't on this page, more
        # than 100 events arrived since the last poll and older ones may have
        # been pushed to page 2+. We don't paginate (ETag invariant), but warn.
        if (
            self.last_event_id is not None
            and not any(e.get("id") == self.last_event_id for e in events)
        ):
            log(
                f"Warning: previous high-water event id={self.last_event_id} "
                "not found on returned page; some events may have been missed. "
                "Consider lowering --poll-interval."
            )
        new_events = filter_relevant_events(events, self.last_event_id)
        # Always advance high-water to newest event seen, even if filtered out
        self.last_event_id = events[0].get("id")
        return new_events

    def _prime_high_water_if_needed(self, token: str) -> None:
        """Record the current newest event id as the high-water mark.

        Avoids replaying every relevant historical event on the events page
        after the initial orchestrator run. No-op if state was loaded from a
        prior session.
        """
        if self.last_event_id is not None:
            return
        try:
            result = fetch_events(self.repo, etag=None, token=token)
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            log(f"Failed to prime high-water mark ({e}); proceeding without")
            return
        if result.events:
            self.last_event_id = result.events[0].get("id")
            log(f"Primed high-water mark at event id={self.last_event_id}")
        if result.etag:
            self.etag = result.etag
        self.save_state()

    def serve(self) -> int:
        log(f"Genesis local control plane starting (repo: {self.repo})")
        log(
            f"  poll_interval={self.poll_interval}s session_timeout={self.session_timeout}s"
        )

        if not self.acquire_lock():
            log("Another local control plane instance is running. Exiting.")
            return 1

        # Verify claude is on PATH before disabling workflows. Otherwise we'd
        # leave GHA off with no working orchestrator running.
        if shutil.which("claude") is None:
            log("Error: 'claude' command not found. Install Claude Code and ensure it's on PATH.")
            self.release_lock()
            return 127

        # Self-heal: if a prior serve session exited non-gracefully (SIGKILL,
        # crash, supervisor restart), `.disabled-by-genesis` is on disk and
        # workflows are still disabled. Re-enable them first so this session
        # starts from a known clean state; the subsequent disable_workflows
        # below will disable them again under fresh tracking.
        if DISABLED_LIST_PATH.exists():
            try:
                already_off = tracked_all_disabled(repo=self.repo)
            except subprocess.CalledProcessError as e:
                log(f"Could not read workflow state ({e}); assuming reconcile is needed")
                already_off = False

            if already_off:
                # Nothing to heal: the end state we want is the state we're in.
                # Enabling here only to disable again seconds later would re-arm
                # GHA long enough for a queued event or cron tick to start the
                # duplicate run this whole mechanism exists to prevent.
                log(
                    f"Found {DISABLED_LIST_PATH} from a prior session; tracked "
                    "workflows are still disabled — keeping them off"
                )
            else:
                log(
                    f"Found stale {DISABLED_LIST_PATH} from a prior session with "
                    "workflows re-enabled — reconciling before disabling again"
                )
                try:
                    enable_workflows(repo=self.repo)
                except subprocess.CalledProcessError as e:
                    log(
                        f"Self-heal failed ({e}). Run `genesis workflows enable` "
                        "manually, then re-run `genesis serve`."
                    )
                    self.release_lock()
                    return 1

        try:
            disable_workflows(repo=self.repo, genesis_only=not self.all_workflows)
        except subprocess.CalledProcessError as e:
            log(f"Failed to disable workflows: {e}")
            # disable_workflows persists incrementally, so any partial-disable
            # state is on disk and can be recovered with `genesis workflows enable`.
            self.release_lock()
            return 1

        self.load_state()

        try:
            token = _gh_token()
        except subprocess.CalledProcessError:
            log("Failed to read gh auth token. Run `gh auth login` first.")
            self._reenable_workflows_safe()
            self.release_lock()
            return 1

        # Prime high-water mark on first run so the post-initial poll doesn't
        # replay every relevant historical event on the events page.
        self._prime_high_water_if_needed(token)

        # Initial run. If it fails because claude is broken (rc=127), abort
        # rather than entering the poll loop with workflows off.
        rc = self.run_orchestrator(None)
        self.save_state()
        if rc == 127:
            log("Initial orchestrator run failed (claude not callable). Aborting.")
            return self._shutdown(token_ok=True)
        if self.shutdown:
            return self._shutdown(token_ok=True)

        log(f"Polling {self.repo} for events...")
        while not self.shutdown:
            self._interruptible_sleep(self.poll_interval)
            if self.shutdown:
                break
            try:
                new_events = self.poll_once(token)
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    log("GitHub auth failed (401). Re-run `gh auth login`.")
                    break
                log(f"GitHub API error: HTTP {e.code} {e.reason}")
                continue
            except urllib.error.URLError as e:
                log(f"Network error polling events: {e}")
                continue

            for event in new_events:
                if self.shutdown:
                    break
                self.run_orchestrator(event)
                self.save_state()

            # A run that stopped mid-task left work in the tree that no future
            # event references. Pick it up on the next tick rather than waiting
            # for unrelated repo activity to happen along.
            if self.pending_followup and not new_events and not self.shutdown:
                self.pending_followup = False
                log("Resuming unfinished work from the previous run")
                self.run_orchestrator(None)
                self.save_state()

        return self._shutdown(token_ok=True)

    def _shutdown(self, token_ok: bool) -> int:
        log("Shutting down — re-enabling GitHub Actions workflows")
        self._reenable_workflows_safe()
        self.release_lock()
        log("Goodbye.")
        return 0

    def _reenable_workflows_safe(self) -> None:
        try:
            enable_workflows(repo=self.repo)
        except subprocess.CalledProcessError as e:
            log(f"Failed to re-enable workflows: {e}. Run `genesis workflows enable` to retry.")


def _make_signal_handler(plane: LocalControlPlane):
    def handler(signum, frame):
        log(f"Received signal {signum} — initiating graceful shutdown")
        plane.shutdown = True
        # If orchestrator is running, kill it; the main loop will exit when wait() returns.
        if plane.orch_proc is not None:
            try:
                os.killpg(os.getpgid(plane.orch_proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

    return handler


def serve() -> int:
    """Run the local orchestrator server. Entry point for `genesis serve`."""
    poll_interval = int(os.environ.get("GENESIS_POLL_INTERVAL", "60"))
    session_timeout = int(os.environ.get("GENESIS_SESSION_TIMEOUT", "3600"))
    agent = os.environ.get("GENESIS_AGENT", DEFAULT_AGENT)
    all_workflows = os.environ.get("GENESIS_ALL_WORKFLOWS") == "1"
    claude_home = resolve_claude_home()

    # Fail before disabling any workflows: a missing agent definition means every
    # session would ask Claude to run a file that isn't there.
    if not Path(agent).is_file():
        log(f"Error: agent definition not found: {agent}")
        log("Pass --agent <path> or set GENESIS_AGENT to an existing definition.")
        return 1

    try:
        repo = _get_repo()
    except subprocess.CalledProcessError as e:
        log(f"Failed to detect repository: {e}")
        log("Set GENESIS_REPO=owner/repo, or run inside a git repo with a GitHub remote.")
        return 1

    plane = LocalControlPlane(
        repo=repo,
        poll_interval=poll_interval,
        session_timeout=session_timeout,
        agent=agent,
        claude_home=claude_home,
        all_workflows=all_workflows,
    )

    handler = _make_signal_handler(plane)
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    # SIGHUP too: closing the terminal window is the most common way this process
    # dies, and unhandled it skips cleanup entirely — leaving the repo's
    # workflows disabled and a stale tracking file behind. SIGKILL still can't be
    # caught; `genesis workflows enable` is the recovery hatch for that.
    signal.signal(signal.SIGHUP, handler)

    try:
        return plane.serve()
    except Exception as e:
        log(f"Unexpected error: {e}")
        plane._reenable_workflows_safe()
        plane.release_lock()
        return 1
