"""Runtime heartbeat, stop sentinel, and external nudge files for `genesis serve`.

All three live as dotfiles directly under `.genesis/`, where `.gitignore`
(`.genesis/.*`) already marks per-machine state untracked across genesis itself
and every scaffolded dev system:

- `.genesis/.plane-state.json`: live snapshot written atomically at plane
  lifecycle transitions (`starting`, `running_session`, `idle`, `stopped`) so
  `genesis fleet status` and `genesis watch` can distinguish a busy session, a
  hung session, an idle poller, and a dead process without guessing from stdout.
- `.genesis/.stopped`: written ONLY on non-restartable terminal stops
  (`run_budget`, `gh_auth_401`, `missing_binary`, or an explicit operator
  `manual_stop`). `launchd`'s `KeepAlive.PathState` watches this path (`false`)
  and `genesis serve --supervised` checks it on startup, so crashes, reboots,
  and `SIGTERM` restarts come straight back while budget/auth stops stay put
  until `genesis fleet resume` clears the sentinel.
- `.genesis/.nudge`: one-shot wake marker written by `genesis watch` when it
  releases a stranded `in-progress` claim or spots starving work on an idle
  plane. The next poll tick consumes the file and queues a follow-up pass.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

PLANE_STATE_REL = Path(".genesis/.plane-state.json")
STOPPED_REL = Path(".genesis/.stopped")
NUDGE_REL = Path(".genesis/.nudge")
LOCK_REL = Path(".genesis/.orchestrator.lock")
STDOUT_REL = Path(".genesis/.serve.stdout")


def _resolve(rel: Path, base_dir: Path | None) -> Path:
    return (base_dir / rel) if base_dir is not None else rel


def pid_alive(pid: object) -> bool:
    """True when `pid` names an existing OS process."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def read_lock_pid(repo_dir: Path | None = None) -> int | None:
    """Return the PID in `.genesis/.orchestrator.lock` if that process is alive."""
    path = _resolve(LOCK_REL, repo_dir)
    try:
        raw = path.read_text().strip()
        pid = int(raw)
    except (OSError, ValueError):
        return None
    return pid if pid_alive(pid) else None


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)
    except OSError:
        # Telemetry and supervision state must never crash the control plane.
        return


def _read_json_dict(path: Path) -> dict[str, object] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_plane_state(
    *,
    repo: str,
    pid: int | None,
    engine: str,
    agent: str,
    state: str,
    started_at: float | None = None,
    child_pid: int | None = None,
    session_token: str = "",
    session_started_at: float | None = None,
    last_turn_at: float | None = None,
    last_poll_at: float | None = None,
    turns: int = 0,
    run_spent: float = 0.0,
    run_budget: float = 0.0,
    cost_unit: str = "usd",
    pending_followup: bool = False,
    stopped_reason: str | None = None,
    base_dir: Path | None = None,
) -> None:
    """Persist the plane's current state atomically."""
    now = time.time()
    payload: dict[str, object] = {
        "repo": repo,
        "pid": pid,
        "engine": engine,
        "agent": agent,
        "state": state,
        "started_at": started_at,
        "child_pid": child_pid,
        "session_token": session_token,
        "session_started_at": session_started_at,
        "last_turn_at": last_turn_at,
        "last_poll_at": last_poll_at,
        "turns": turns,
        "run_spent": round(run_spent, 4),
        "run_budget": round(run_budget, 4),
        "cost_unit": cost_unit,
        "pending_followup": pending_followup,
        "stopped_reason": stopped_reason,
        "updated_at": now,
    }
    _atomic_write_json(_resolve(PLANE_STATE_REL, base_dir), payload)


def read_plane_state(repo_dir: Path | None = None) -> dict[str, object] | None:
    """Read `.genesis/.plane-state.json`, or `None` if absent/corrupt."""
    return _read_json_dict(_resolve(PLANE_STATE_REL, repo_dir))


def write_stopped(
    reason: str,
    *,
    detail: str = "",
    run_spent: float = 0.0,
    run_budget: float = 0.0,
    cost_unit: str = "usd",
    base_dir: Path | None = None,
) -> None:
    """Record a terminal stop so `launchd` doesn't respawn in a tight loop."""
    payload: dict[str, object] = {
        "reason": reason,
        "detail": detail,
        "run_spent": round(run_spent, 4),
        "run_budget": round(run_budget, 4),
        "cost_unit": cost_unit,
        "stopped_at": time.time(),
    }
    _atomic_write_json(_resolve(STOPPED_REL, base_dir), payload)


def read_stopped(repo_dir: Path | None = None) -> dict[str, object] | None:
    """Read `.genesis/.stopped`, or `None` if the plane isn't marked stopped."""
    path = _resolve(STOPPED_REL, repo_dir)
    if not path.exists():
        return None
    data = _read_json_dict(path)
    if data is not None:
        return data
    # Even an empty/hand-touched `.stopped` sentinel still counts as stopped.
    return {"reason": "manual_stop", "detail": ""}


def clear_stopped(repo_dir: Path | None = None) -> bool:
    """Remove `.genesis/.stopped`. Returns True if a sentinel was cleared."""
    path = _resolve(STOPPED_REL, repo_dir)
    if not path.exists():
        return False
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def write_nudge(repo_dir: Path, reason: str) -> None:
    """Ask a running `genesis serve` to queue a follow-up pass on its next tick."""
    _atomic_write_json(
        _resolve(NUDGE_REL, repo_dir),
        {"reason": reason, "created_at": time.time()},
    )


def consume_nudge(base_dir: Path | None = None) -> str | None:
    """Read and remove `.genesis/.nudge`, returning its reason if one was pending."""
    path = _resolve(NUDGE_REL, base_dir)
    if not path.exists():
        return None
    data = _read_json_dict(path)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    if isinstance(data, dict):
        reason = str(data.get("reason") or "").strip()
        if reason:
            return reason
    return "external nudge from genesis watch"
