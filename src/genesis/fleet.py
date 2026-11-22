"""macOS `launchd` supervisor management for a fleet of local `genesis serve` planes.

Why this exists: `genesis serve` turns a terminal tab into a control plane, and
terminal tabs die silently — on reboot, on window close, on transient crashes,
or legitimately when `RUN_COST_BUDGET_USD` is reached. Unsupervised, all four
look identical afterwards: no process, disabled workflows (or workflows
re-enabled with nobody watching local state), and a silent backlog.

`genesis fleet` reads `~/.config/genesis/fleet.toml` and reconciles one
`LaunchAgent` per enabled repo (`ai.sayfan.genesis.serve.<name>`) plus one
periodic watchdog (`ai.sayfan.genesis.watch`):

- Crashes, reboots, and `SIGTERM` restarts do NOT write `.genesis/.stopped`, so
  `launchd` (`KeepAlive.PathState` on `.genesis/.stopped == false`) brings the
  plane straight back.
- Budget exhaustion (`run_budget`), broken GitHub auth (`gh_auth_401`), missing
  engine CLI (`missing_binary`), and `genesis fleet stop` DO write
  `.genesis/.stopped`, so `launchd` leaves the plane down until an operator runs
  `genesis fleet resume <name>`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import plistlib
import re
import shlex
import signal
import subprocess
import time
import tomllib
from zoneinfo import ZoneInfo

from genesis.plane_state import (
    STDOUT_REL,
    STOPPED_REL,
    clear_stopped,
    pid_alive,
    read_lock_pid,
    read_plane_state,
    read_stopped,
    write_stopped,
)

SERVE_LABEL_PREFIX = "ai.sayfan.genesis.serve."
WATCH_LABEL = "ai.sayfan.genesis.watch"
PACIFIC = ZoneInfo("America/Los_Angeles")
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class FleetRepo:
    name: str
    path: Path
    repo: str | None = None
    agent: str | None = None
    engine: str | None = None
    poll_interval: int | None = None
    session_timeout: int | None = None
    all_workflows: bool = False
    enabled: bool = True

    @property
    def label(self) -> str:
        return f"{SERVE_LABEL_PREFIX}{self.name}"


@dataclass(frozen=True)
class FleetConfig:
    config_path: Path
    plane_dir: Path
    watch_interval: int
    watch_enabled: bool
    repos: list[FleetRepo]


def fleet_config_path(override: str | Path | None = None) -> Path:
    """Machine-wide fleet manifest (`~/.config/genesis/fleet.toml` by default).

    Deliberately ignores `GENESIS_CONFIG_DIR`: per-org directories set that
    variable in `.envrc` to pick a GitHub App, whereas `fleet.toml` spans every
    org on the machine. Tests and custom setups override via `--config` or
    `GENESIS_FLEET_CONFIG`.
    """
    if override:
        return Path(override).expanduser()
    env_override = os.environ.get("GENESIS_FLEET_CONFIG", "").strip()
    if env_override:
        return Path(env_override).expanduser()
    return Path.home() / ".config" / "genesis" / "fleet.toml"


def launch_agents_dir() -> Path:
    override = os.environ.get("GENESIS_LAUNCH_AGENTS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "LaunchAgents"


def default_daemon_path(home: Path | None = None) -> str:
    h = home or Path.home()
    return f"{h / '.local' / 'bin'}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def load_fleet_config(path: str | Path | None = None) -> FleetConfig:
    cfg_path = fleet_config_path(path)
    if not cfg_path.is_file():
        raise FileNotFoundError(
            f"Fleet config not found at {cfg_path}. Create it with [[repos]] entries first."
        )
    with cfg_path.open("rb") as f:
        raw = tomllib.load(f)

    runtime = raw.get("runtime") or {}
    if not isinstance(runtime, dict):
        raise ValueError("[runtime] in fleet.toml must be a table")
    plane_dir = Path(str(runtime.get("plane_dir") or "~/git/genesis-plane")).expanduser()
    watch_interval = int(runtime.get("watch_interval", 180))
    if watch_interval < 30:
        raise ValueError("runtime.watch_interval must be at least 30 seconds")
    watch_enabled = bool(runtime.get("watch_enabled", True))

    raw_repos = raw.get("repos") or []
    if not isinstance(raw_repos, list):
        raise ValueError("[[repos]] in fleet.toml must be an array of tables")

    seen: set[str] = set()
    repos: list[FleetRepo] = []
    for item in raw_repos:
        if not isinstance(item, dict):
            raise ValueError("Each [[repos]] entry must be a table")
        name = str(item.get("name") or "").strip()
        if not name or not SAFE_NAME_RE.match(name):
            raise ValueError(f"Invalid repo name in fleet.toml: {name!r}")
        if name in seen:
            raise ValueError(f"Duplicate repo name in fleet.toml: {name!r}")
        seen.add(name)
        raw_path = str(item.get("path") or "").strip()
        if not raw_path:
            raise ValueError(f"Missing path for repo {name!r} in fleet.toml")
        repos.append(
            FleetRepo(
                name=name,
                path=Path(raw_path).expanduser(),
                repo=str(item["repo"]).strip() if item.get("repo") else None,
                agent=str(item["agent"]).strip() if item.get("agent") else None,
                engine=str(item["engine"]).strip() if item.get("engine") else None,
                poll_interval=int(item["poll_interval"]) if item.get("poll_interval") is not None else None,
                session_timeout=int(item["session_timeout"]) if item.get("session_timeout") is not None else None,
                all_workflows=bool(item.get("all_workflows", False)),
                enabled=bool(item.get("enabled", True)),
            )
        )

    return FleetConfig(
        config_path=cfg_path,
        plane_dir=plane_dir,
        watch_interval=watch_interval,
        watch_enabled=watch_enabled,
        repos=repos,
    )


def serve_command_args(entry: FleetRepo, plane_dir: Path) -> list[str]:
    genesis_bin = str(plane_dir / ".venv" / "bin" / "genesis")
    cmd = [genesis_bin, "serve", "--supervised"]
    if entry.repo:
        cmd.extend(["--repo", entry.repo])
    if entry.agent:
        cmd.extend(["--agent", entry.agent])
    if entry.engine:
        cmd.extend(["--engine", entry.engine])
    if entry.poll_interval is not None:
        cmd.extend(["--poll-interval", str(entry.poll_interval)])
    if entry.session_timeout is not None:
        cmd.extend(["--session-timeout", str(entry.session_timeout)])
    if entry.all_workflows:
        cmd.append("--all-workflows")
    return cmd


def build_serve_plist(entry: FleetRepo, plane_dir: Path, *, home: Path | None = None) -> bytes:
    """Render the `LaunchAgent` plist for one `genesis serve` repo."""
    h = home or Path.home()
    argv = serve_command_args(entry, plane_dir)
    quoted = " ".join(shlex.quote(part) for part in argv)
    # `exec direnv exec .` replaces both `/bin/zsh` and `direnv` via `execve`, so
    # the PID `launchd` supervises is the Python plane itself while still loading
    # parent `.envrc` files (per-org `GENESIS_CONFIG_DIR` and `GH_TOKEN` routing).
    shell_cmd = (
        f"if command -v direnv >/dev/null 2>&1; then exec direnv exec . {quoted}; "
        f"else exec {quoted}; fi"
    )
    stopped_path = str(entry.path / STOPPED_REL)
    stdout_path = str(entry.path / STDOUT_REL)
    payload = {
        "Label": entry.label,
        "WorkingDirectory": str(entry.path),
        "ProgramArguments": ["/bin/zsh", "-lc", shell_cmd],
        "EnvironmentVariables": {
            "HOME": str(h),
            "PATH": default_daemon_path(h),
        },
        "KeepAlive": {
            "PathState": {
                stopped_path: False,
            }
        },
        "ThrottleInterval": 30,
        "StandardOutPath": stdout_path,
        "StandardErrorPath": stdout_path,
    }
    return plistlib.dumps(payload, sort_keys=True)


def build_watch_plist(cfg: FleetConfig, *, home: Path | None = None) -> bytes:
    """Render the periodic `ai.sayfan.genesis.watch` `LaunchAgent` plist."""
    h = home or Path.home()
    genesis_bin = str(cfg.plane_dir / ".venv" / "bin" / "genesis")
    log_path = str(h / "Library" / "Logs" / "genesis-watch.log")
    payload = {
        "Label": WATCH_LABEL,
        "WorkingDirectory": str(h),
        "ProgramArguments": [genesis_bin, "watch", "--config", str(cfg.config_path)],
        "EnvironmentVariables": {
            "HOME": str(h),
            "PATH": default_daemon_path(h),
        },
        "StartInterval": cfg.watch_interval,
        "RunAtLoad": True,
        "LowPriorityIO": True,
        "Nice": 5,
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }
    return plistlib.dumps(payload, sort_keys=True)


def _gui_domain() -> str:
    return f"gui/{os.getuid()}"


def _run_launchctl(args: list[str], *, run=subprocess.run) -> subprocess.CompletedProcess:
    return run(["/bin/launchctl", *args], capture_output=True, text=True, check=False)


def _is_loaded(label: str, *, run=subprocess.run) -> bool:
    res = _run_launchctl(["print", f"{_gui_domain()}/{label}"], run=run)
    return res.returncode == 0


def _format_stamp(ts: object) -> str:
    if not isinstance(ts, (int, float)) or ts <= 0:
        return "-"
    dt = datetime.fromtimestamp(float(ts), tz=PACIFIC)
    return dt.strftime("%Y-%m-%d %H:%M %Z")


def _format_spend(state: dict[str, object] | None, stopped: dict[str, object] | None) -> str:
    src = state or stopped or {}
    spent = src.get("run_spent")
    budget = src.get("run_budget")
    unit = str(src.get("cost_unit") or "usd")
    if not isinstance(spent, (int, float)):
        return "-"
    if unit == "tokens":
        if isinstance(budget, (int, float)) and budget > 0:
            return f"{int(spent):,}/{int(budget):,} tok"
        return f"{int(spent):,} tok"
    if isinstance(budget, (int, float)) and budget > 0:
        return f"${spent:.2f}/${budget:.0f}"
    return f"${spent:.2f}"


def _configured_engine(repo_dir: Path) -> str:
    cfg_file = repo_dir / ".genesis" / "config.toml"
    try:
        with cfg_file.open("rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return "claude"
    eng = raw.get("engine")
    if isinstance(eng, dict) and isinstance(eng.get("name"), str) and eng["name"].strip():
        return eng["name"].strip()
    return "claude"


def inspect_repo_status(entry: FleetRepo) -> dict[str, object]:
    """Summarize one repo's local plane status without making network calls."""
    live_pid = read_lock_pid(entry.path)
    state = read_plane_state(entry.path)
    stopped = read_stopped(entry.path)
    disabled_marker = (entry.path / ".genesis" / ".disabled-by-genesis").exists()

    engine = str((state or {}).get("engine") or entry.engine or _configured_engine(entry.path))
    if not entry.enabled and live_pid is None:
        status = "disabled"
    elif live_pid is not None:
        raw_state = str((state or {}).get("state") or "running")
        child_pid = (state or {}).get("child_pid")
        if raw_state == "running_session" and pid_alive(child_pid):
            turns = (state or {}).get("turns") or 0
            status = f"running_session (t={turns})"
        elif (state or {}).get("pending_followup"):
            status = "idle (followup queued)"
        else:
            status = "idle" if raw_state in ("idle", "starting", "running") else raw_state
    elif stopped is not None:
        reason = str(stopped.get("reason") or "manual_stop")
        status = f"stopped ({reason})"
    elif disabled_marker:
        status = "down (workflows off)"
    else:
        status = "down"

    activity_ts = None
    if state:
        activity_ts = (
            state.get("last_turn_at")
            or state.get("last_poll_at")
            or state.get("updated_at")
        )
    elif stopped:
        activity_ts = stopped.get("stopped_at")

    return {
        "name": entry.name,
        "enabled": entry.enabled,
        "status": status,
        "pid": live_pid,
        "engine": engine,
        "spend": _format_spend(state, stopped),
        "activity": _format_stamp(activity_ts),
        "path": str(entry.path),
    }


def fleet_status(cfg: FleetConfig) -> int:
    rows = [inspect_repo_status(r) for r in cfg.repos]
    headers = ("NAME", "STATUS", "PID", "ENGINE", "SPEND", "LAST ACTIVITY", "PATH")
    table_rows = [
        (
            str(r["name"]),
            str(r["status"]),
            str(r["pid"] or "-"),
            str(r["engine"]),
            str(r["spend"]),
            str(r["activity"]),
            str(r["path"]),
        )
        for r in rows
    ]
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in table_rows)) if table_rows else len(headers[i])
        for i in range(len(headers))
    ]
    fmt = "  ".join(f"{{:{w}}}" for w in widths[:-1]) + "  {}"
    print(fmt.format(*headers))
    print("  ".join("-" * w for w in widths))
    for row in table_rows:
        print(fmt.format(*row))
    return 0


def fleet_sync(cfg: FleetConfig, *, run=subprocess.run) -> int:
    agents_dir = launch_agents_dir()
    agents_dir.mkdir(parents=True, exist_ok=True)
    (Path.home() / "Library" / "Logs").mkdir(parents=True, exist_ok=True)
    domain = _gui_domain()

    genesis_bin = cfg.plane_dir / ".venv" / "bin" / "genesis"
    if not genesis_bin.is_file():
        print(f"Error: genesis runtime binary not found at {genesis_bin}")
        print("Run `scripts/update-plane.sh` first.")
        return 1

    desired_plists: set[Path] = set()
    for entry in cfg.repos:
        plist_path = agents_dir / f"{entry.label}.plist"
        if not entry.enabled:
            if plist_path.exists():
                _run_launchctl(["bootout", domain, str(plist_path)], run=run)
                plist_path.unlink(missing_ok=True)
                print(f"unloaded disabled {entry.name} ({plist_path.name})")
            continue
        if not entry.path.is_dir():
            print(f"Warning: skipping {entry.name} (directory missing: {entry.path})")
            continue
        (entry.path / ".genesis").mkdir(parents=True, exist_ok=True)
        desired_plists.add(plist_path)
        rendered = build_serve_plist(entry, cfg.plane_dir)
        changed = not plist_path.exists() or plist_path.read_bytes() != rendered
        if changed:
            plist_path.write_bytes(rendered)
        loaded = _is_loaded(entry.label, run=run)
        live_pid = read_lock_pid(entry.path)
        if changed or not loaded:
            # Avoid killing an in-flight session if a manual plane is already
            # holding `.orchestrator.lock`; when the plist is newly loaded,
            # `acquire_lock()` keeps the existing live plane untouched until it
            # exits, then `launchd` takes over seamlessly.
            if loaded:
                _run_launchctl(["bootout", domain, str(plist_path)], run=run)
            res = _run_launchctl(["bootstrap", domain, str(plist_path)], run=run)
            if res.returncode != 0:
                print(f"Error loading {entry.label}: {(res.stderr or res.stdout).strip()}")
                return 1
            action = "updated" if loaded else "loaded"
            suffix = f" (existing pid {live_pid} kept)" if live_pid else ""
            print(f"{action} {entry.label}{suffix}")
        else:
            print(f"up-to-date {entry.label}")

    # Remove stale `ai.sayfan.genesis.serve.*.plist` files removed from fleet.toml.
    for existing in sorted(agents_dir.glob(f"{SERVE_LABEL_PREFIX}*.plist")):
        if existing not in desired_plists:
            _run_launchctl(["bootout", domain, str(existing)], run=run)
            existing.unlink(missing_ok=True)
            print(f"removed stale {existing.name}")

    watch_plist = agents_dir / f"{WATCH_LABEL}.plist"
    if cfg.watch_enabled:
        rendered_watch = build_watch_plist(cfg)
        watch_changed = not watch_plist.exists() or watch_plist.read_bytes() != rendered_watch
        if watch_changed:
            watch_plist.write_bytes(rendered_watch)
        watch_loaded = _is_loaded(WATCH_LABEL, run=run)
        if watch_changed or not watch_loaded:
            if watch_loaded:
                _run_launchctl(["bootout", domain, str(watch_plist)], run=run)
            res = _run_launchctl(["bootstrap", domain, str(watch_plist)], run=run)
            if res.returncode != 0:
                print(f"Error loading {WATCH_LABEL}: {(res.stderr or res.stdout).strip()}")
                return 1
            print(f"{'updated' if watch_loaded else 'loaded'} {WATCH_LABEL}")
        else:
            print(f"up-to-date {WATCH_LABEL}")
    elif watch_plist.exists():
        _run_launchctl(["bootout", domain, str(watch_plist)], run=run)
        watch_plist.unlink(missing_ok=True)
        print(f"unloaded {WATCH_LABEL}")

    return 0


def _select_targets(cfg: FleetConfig, target: str) -> list[FleetRepo]:
    if target == "all":
        return [r for r in cfg.repos if r.enabled]
    matches = [r for r in cfg.repos if r.name == target]
    if not matches:
        known = ", ".join(r.name for r in cfg.repos) or "(none)"
        raise ValueError(f"Unknown fleet repo {target!r} (known: {known})")
    return matches


def fleet_resume(cfg: FleetConfig, target: str = "all", *, run=subprocess.run) -> int:
    domain = _gui_domain()
    agents_dir = launch_agents_dir()
    for entry in _select_targets(cfg, target):
        cleared = clear_stopped(entry.path)
        plist_path = agents_dir / f"{entry.label}.plist"
        if plist_path.exists() and not _is_loaded(entry.label, run=run):
            _run_launchctl(["bootstrap", domain, str(plist_path)], run=run)
        if _is_loaded(entry.label, run=run) and read_lock_pid(entry.path) is None:
            _run_launchctl(["kickstart", f"{domain}/{entry.label}"], run=run)
        note = "cleared .stopped and " if cleared else ""
        print(f"{entry.name}: {note}resumed")
    return 0


def fleet_stop(cfg: FleetConfig, target: str = "all", *, run=subprocess.run) -> int:
    domain = _gui_domain()
    for entry in _select_targets(cfg, target):
        write_stopped("manual_stop", detail="stopped via genesis fleet stop", base_dir=entry.path)
        if _is_loaded(entry.label, run=run):
            _run_launchctl(["kill", "SIGTERM", f"{domain}/{entry.label}"], run=run)
        pid = read_lock_pid(entry.path)
        if pid is not None:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        print(f"{entry.name}: marked stopped and sent SIGTERM")
    return 0


def fleet_restart(cfg: FleetConfig, target: str = "all", *, run=subprocess.run) -> int:
    domain = _gui_domain()
    for entry in _select_targets(cfg, target):
        clear_stopped(entry.path)
        if _is_loaded(entry.label, run=run):
            res = _run_launchctl(["kickstart", "-k", f"{domain}/{entry.label}"], run=run)
            if res.returncode != 0:
                print(f"{entry.name}: kickstart failed: {(res.stderr or res.stdout).strip()}")
                return 1
            print(f"{entry.name}: restarted")
        else:
            print(f"{entry.name}: LaunchAgent not loaded; run `genesis fleet sync` first")
            return 1
    return 0


def fleet_logs(cfg: FleetConfig, name: str, lines: int = 50) -> int:
    entry = _select_targets(cfg, name)[0]
    log_path = entry.path / STDOUT_REL
    if not log_path.is_file():
        print(f"No log file at {log_path}")
        return 0
    tail = log_path.read_text(errors="replace").splitlines()[-max(1, lines) :]
    for line in tail:
        print(line)
    return 0
