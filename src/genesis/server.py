"""Local orchestrator server (local control plane).

Runs the orchestrator agent locally, polling GitHub repo events and launching
fresh Claude sessions when relevant activity is detected. Disables GitHub
Actions workflows on start to prevent duplicate execution; re-enables them on
graceful shutdown.

Authentication uses the user's existing `gh` CLI auth (`gh auth token`).
The Anthropic API key must be available in the environment for `claude -p`.

Configuration (environment variables):
    GENESIS_POLL_INTERVAL    seconds between polls (default: 60)
    GENESIS_SESSION_TIMEOUT  max seconds per orchestrator session (default: 3600)
    GENESIS_REPO             owner/repo (default: detected from git remote)
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from genesis.workflows import DISABLED_LIST_PATH, disable_workflows, enable_workflows

LOCK_PATH = Path(".genesis/.orchestrator.lock")
ETAG_PATH = Path(".genesis/.poll-etag")
HIGHWATER_PATH = Path(".genesis/.poll-highwater")

RELEVANT_EVENT_TYPES = frozenset(
    {"IssuesEvent", "IssueCommentEvent", "PullRequestEvent"}
)


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


def _build_prompt(event: dict | None) -> str:
    if event is None:
        return (
            "Run the orchestrator agent defined in .claude/agents/orchestrator.md. "
            "This is a local control plane initial run — assess project state and advance work."
        )
    event_type = event.get("type", "UnknownEvent")
    action = event.get("payload", {}).get("action", "unknown")
    actor = event.get("actor", {}).get("login", "unknown")
    return (
        "Run the orchestrator agent defined in .claude/agents/orchestrator.md.\n\n"
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

    def run_orchestrator(self, event: dict | None) -> int:
        prompt = _build_prompt(event)
        if event is None:
            log("Launching orchestrator (initial run)")
        else:
            event_type = event.get("type", "?")
            action = event.get("payload", {}).get("action", "?")
            event_id = event.get("id", "?")
            log(f"Launching orchestrator for {event_type}/{action} (id={event_id})")

        cmd = [
            "claude",
            "-p",
            prompt,
            "--max-turns",
            "20",
            "--allowedTools",
            "Read,Edit,Bash,Glob,Grep,Agent",
        ]
        try:
            self.orch_proc = subprocess.Popen(cmd, start_new_session=True)
        except FileNotFoundError:
            log("Error: 'claude' command not found. Install Claude Code and ensure it's on PATH.")
            return 127

        deadline = time.time() + self.session_timeout
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
                        log(f"Session timeout ({self.session_timeout}s) — terminating orchestrator")
                        self._kill_orch()
                        return -1
        finally:
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
            log(
                f"Found stale {DISABLED_LIST_PATH} from a prior session — "
                "re-enabling workflows before disabling them again"
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
            disable_workflows(repo=self.repo)
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
    )

    handler = _make_signal_handler(plane)
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    try:
        return plane.serve()
    except Exception as e:
        log(f"Unexpected error: {e}")
        plane._reenable_workflows_safe()
        plane.release_lock()
        return 1
