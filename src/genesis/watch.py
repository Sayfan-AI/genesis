"""Board-vs-execution stall detector and single-aggregate fleet alerting (`genesis watch`).

`launchd` keeps `genesis serve` processes running; `genesis watch` checks the
gap a process supervisor can't see — when the GitHub board and the local
execution state disagree:

1. `daemon_down` / `stopped:<reason>`: an enabled fleet repo has no live
   `genesis serve` PID (either unexpectedly down or halted at `run_budget`,
   `gh_auth_401`, or `missing_binary`).
2. `phantom_in_progress`: an open issue carries `in-progress` past the grace
   window while no local session is running, no GitHub Actions workflow is
   queued/running, and no open pull request backs the issue (the failure class
   from `sweatless` issue #103). Auto-remediated by releasing the claim and
   writing `.genesis/.nudge`.
3. `hung_session`: a local session's child process is alive in `running_session`
   with zero tool-call progress for `--hung-minutes` (default 20).
4. `starving_work`: an idle plane with no open `needs:human` gate has selectable
   work or a green bot pull request sitting untouched. First occurrence writes
   `.genesis/.nudge` silently; if work remains starved past a second window, it
   joins the fleet alert.

Alerts are consolidated into **one aggregate notification per fleet check**
(throttled via `~/.config/genesis/watch-state.json`) rather than one alert per
repository.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from genesis.appauth import (
    _ENV_LINE,
    load_app_credentials,
    local_env_path,
    mint_installation_token,
)
from genesis.automerge import _gh, is_bot_author
from genesis.claims import CLAIM_MARKER_RE, _last_claim_session, _parse_iso, _pr_backs_issue
from genesis.fleet import FleetRepo, load_fleet_config
from genesis.plane_state import (
    pid_alive,
    read_lock_pid,
    read_plane_state,
    read_stopped,
    write_nudge,
)
from genesis import triggers

LOKI_KEYS = ("GENESIS_LOKI_URL", "GENESIS_LOKI_USER", "GENESIS_LOKI_TOKEN")
REMOTE_SLUG_RE = re.compile(r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$")
ACTIVE_RUN_STATUSES = frozenset({"in_progress", "queued", "waiting", "requested", "pending"})


@dataclass(frozen=True)
class Finding:
    repo: str
    kind: str
    summary: str
    remediated: bool = False


def watch_state_path(override: str | Path | None = None) -> Path:
    if override:
        return Path(override).expanduser()
    env_override = os.environ.get("GENESIS_WATCH_STATE", "").strip()
    if env_override:
        return Path(env_override).expanduser()
    return Path.home() / ".config" / "genesis" / "watch-state.json"


def _load_watch_state(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_watch_state(path: Path, state: dict[str, object]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)
    except OSError:
        return


def load_loki_env(environ: dict[str, str] | None = None, path: Path | None = None) -> dict[str, str]:
    """Read `GENESIS_LOKI_*` from `~/.config/genesis/.env` without touching `ANTHROPIC_API_KEY`."""
    base = dict(os.environ if environ is None else environ)
    wanted = [k for k in LOKI_KEYS if not (base.get(k) or "").strip()]
    if not wanted:
        return base
    candidates = [path] if path else [local_env_path(), Path.home() / ".config" / "genesis" / ".env"]
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            text = candidate.read_text()
        except OSError:
            continue
        for m in _ENV_LINE.finditer(text):
            key = m.group(1)
            if key not in wanted or (base.get(key) or "").strip():
                continue
            val = (
                m.group(2)
                if m.group(2) is not None
                else m.group(3)
                if m.group(3) is not None
                else m.group(4).split(" #", 1)[0].strip()
            )
            if val:
                base[key] = val
    return base


def _push_watch_loki(findings: list[Finding], notified: bool, env: dict[str, str]) -> bool:
    """Best-effort Loki `fleet-watch` event push."""
    url = (env.get("GENESIS_LOKI_URL") or "").strip()
    if not url:
        return False
    import base64
    import urllib.request

    ns = time.time_ns()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ns // 1_000_000_000))
    repos = ",".join(sorted({f.repo for f in findings}))
    kinds = ",".join(sorted({f"{f.repo}:{f.kind}" for f in findings}))
    msg = "; ".join(f"{f.repo}: {f.summary}" for f in findings)
    fields = {
        "ts": f"{stamp}.{ns % 1_000_000_000 // 1_000_000:03d}Z",
        "event": "fleet-watch",
        "level": "warn",
        "count": len(findings),
        "notified": str(notified).lower(),
        "repos": repos,
        "kinds": kinds,
        "summary": msg,
    }

    def _fmt(v: object) -> str:
        t = str(v)
        return json.dumps(t) if any(c in t for c in ' "=\\') else t

    line = " ".join(f"{k}={_fmt(v)}" for k, v in fields.items())
    payload = json.dumps(
        {
            "streams": [
                {
                    "stream": {
                        "project": "genesis-watch",
                        "hook_event": "fleet-watch",
                        "service_name": "genesis-watch",
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
    user = env.get("GENESIS_LOKI_USER", "")
    tok = env.get("GENESIS_LOKI_TOKEN", "")
    if user and tok:
        cred = base64.b64encode(f"{user}:{tok}".encode()).decode()
        req.add_header("Authorization", f"Basic {cred}")
    try:
        with urllib.request.urlopen(req, timeout=5):
            return True
    except Exception:  # noqa: BLE001
        return False


def _repo_env(repo_dir: Path) -> dict[str, str]:
    """Build an isolated env for `repo_dir`, honoring any `.envrc` org routing."""
    env = dict(os.environ)
    # Strip inherited `GH_TOKEN` before consulting direnv or keyring so an
    # ambient work token never bleeds into personal/org repos.
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    direnv_bin = shutil.which("direnv", path=env.get("PATH"))
    if direnv_bin and repo_dir.is_dir():
        try:
            out = subprocess.run(
                [direnv_bin, "export", "json"],
                cwd=repo_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if out.returncode == 0 and out.stdout.strip():
                exported = json.loads(out.stdout)
                if isinstance(exported, dict):
                    for k, v in exported.items():
                        if v is None:
                            env.pop(k, None)
                        elif isinstance(v, str):
                            env[k] = v
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    cfg_dir = env.get("GENESIS_CONFIG_DIR", "").strip()
    env_file = (Path(cfg_dir) / ".env") if cfg_dir else local_env_path()
    load_app_credentials(env, env_file)
    return env


def detect_repo_slug(entry: FleetRepo, state: dict[str, object] | None = None) -> str | None:
    if entry.repo:
        return entry.repo
    if state and isinstance(state.get("repo"), str) and state["repo"].strip():
        return str(state["repo"]).strip()
    try:
        out = subprocess.run(
            ["git", "-C", str(entry.path), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    m = REMOTE_SLUG_RE.search(out.stdout.strip())
    return m.group(1) if m else None


def _has_child_process(parent_pid: int) -> bool:
    """Fallback child check for a legacy `genesis serve` with no `.plane-state.json`."""
    try:
        out = subprocess.run(
            ["pgrep", "-P", str(parent_pid)],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return out.returncode == 0 and bool(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def _last_claim_timestamp(issue: dict) -> float | None:
    stamp: float | None = None
    for comment in issue.get("comments") or []:
        if not isinstance(comment, dict):
            continue
        if CLAIM_MARKER_RE.search(str(comment.get("body") or "")):
            parsed = _parse_iso(comment.get("createdAt"))
            if parsed is not None:
                stamp = parsed
    if stamp is not None:
        return stamp
    return _parse_iso(issue.get("updatedAt"))


def _gh_json(args: list[str], token: str | None, *, gh=_gh) -> object | None:
    code, out = gh(args, token)
    if code != 0:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def _has_active_actions_run(slug: str, token: str | None, *, gh=_gh) -> bool | None:
    runs = _gh_json(
        ["run", "list", "--repo", slug, "--limit", "20", "--json", "status"],
        token,
        gh=gh,
    )
    if not isinstance(runs, list):
        return None
    return any(
        isinstance(r, dict) and str(r.get("status") or "") in ACTIVE_RUN_STATUSES
        for r in runs
    )


def _checks_state(rollup: list) -> str:
    """Return 'pending', 'green', or 'other' for a PR's statusCheckRollup."""
    if not rollup:
        return "other"
    for item in rollup:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").upper()
        state = str(item.get("state") or "").upper()
        conclusion = str(item.get("conclusion") or "").upper()
        if status and status != "COMPLETED":
            return "pending"
        if state in ("PENDING", "EXPECTED"):
            return "pending"
        if conclusion not in ("SUCCESS", "NEUTRAL", "SKIPPED") and state != "SUCCESS":
            return "other"
    return "green"


def _release_phantom_claim(
    entry: FleetRepo,
    slug: str,
    issue_number: int,
    claim_session: str | None,
    env: dict[str, str],
    app_token: str | None,
    *,
    gh=_gh,
    run=subprocess.run,
) -> bool:
    reason = (
        f"genesis watch: issue #{issue_number} was marked in-progress with no running "
        "local session, active workflow run, or open pull request"
    )
    issues_sh = entry.path / ".genesis" / "scripts" / "issues.sh"
    child_env = dict(env)
    for secret in ("GENESIS_GITHUB_APP_SECRET", "GENESIS_GITHUB_APP_ID"):
        child_env.pop(secret, None)
    if app_token:
        child_env["GH_TOKEN"] = app_token
        child_env["GITHUB_TOKEN"] = app_token

    if claim_session and issues_sh.is_file():
        try:
            out = run(
                ["bash", str(issues_sh), "release", "--session", claim_session, "--reason", reason],
                cwd=entry.path,
                env=child_env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if out.returncode == 0 and f"released #{issue_number}" in (out.stdout or ""):
                return True
        except (OSError, subprocess.SubprocessError):
            pass

    # Unmarked `in-progress` label (no `genesis-claim` comment) or missing script:
    # remove the label directly and leave an audit comment.
    tok = app_token or env.get("GH_TOKEN")
    code, _ = gh(
        ["issue", "edit", str(issue_number), "--repo", slug, "--remove-label", "in-progress"],
        tok,
    )
    if code == 0:
        gh(["issue", "comment", str(issue_number), "--repo", slug, "--body", reason], tok)
        return True
    return False


def check_repo(
    entry: FleetRepo,
    *,
    now: float | None = None,
    phantom_minutes: float = 5.0,
    hung_minutes: float = 20.0,
    starve_minutes: float = 15.0,
    remediate: bool = True,
    watch_state: dict[str, object] | None = None,
    gh=_gh,
    run=subprocess.run,
    env_loader=_repo_env,
    token_minter=mint_installation_token,
) -> list[Finding]:
    """Run all four stall checks against `entry`."""
    ts_now = time.time() if now is None else now
    findings: list[Finding] = []

    live_pid = read_lock_pid(entry.path)
    state = read_plane_state(entry.path)
    stopped = read_stopped(entry.path)
    disabled_marker = (entry.path / ".genesis" / ".disabled-by-genesis").exists()

    manually_paused = False
    if live_pid is None:
        if stopped is not None:
            reason = str(stopped.get("reason") or "manual_stop")
            if reason == "manual_stop":
                manually_paused = True
            else:
                detail = str(stopped.get("detail") or "").strip()
                extra = f" ({detail})" if detail else ""
                findings.append(
                    Finding(
                        repo=entry.name,
                        kind=f"stopped:{reason}",
                        summary=f"plane stopped on {reason}{extra}",
                    )
                )
        elif entry.enabled:
            wf_note = " (workflows still disabled)" if disabled_marker else ""
            findings.append(
                Finding(
                    repo=entry.name,
                    kind="daemon_down",
                    summary=f"genesis serve isn't running{wf_note}",
                )
            )

    # Check 3: hung local session (pure local state, zero network calls).
    local_session_running = False
    if live_pid is not None:
        if state is not None:
            if state.get("state") == "running_session" and pid_alive(state.get("child_pid")):
                local_session_running = True
                last_progress = max(
                    float(state.get("last_turn_at") or 0.0),
                    float(state.get("session_started_at") or 0.0),
                )
                if last_progress > 0 and ts_now - last_progress >= hung_minutes * 60:
                    idle_min = int((ts_now - last_progress) // 60)
                    findings.append(
                        Finding(
                            repo=entry.name,
                            kind="hung_session",
                            summary=(
                                f"session pid {state.get('child_pid')} has produced "
                                f"no tool calls for {idle_min}m"
                            ),
                        )
                    )
            # `session_token` spans the whole continuation chain: the plane sets
            # it when a chain starts and clears it only after the ladder and its
            # own claim release finish. Between a session ending and its resume
            # (the judge runs here), the state still names the reaped child, so
            # the pid check above misses a chain that's very much alive. Its
            # claims are the ladder's to release, never the watchdog's (#173).
            if str(state.get("session_token") or ""):
                local_session_running = True
        elif _has_child_process(live_pid):
            local_session_running = True

    slug = detect_repo_slug(entry, state)
    if not slug:
        return findings

    env = env_loader(entry.path)
    app_token = token_minter(slug, env)
    read_token = app_token or env.get("GH_TOKEN")

    # Check 2: phantom `in-progress` issues on the board.
    in_prog = _gh_json(
        [
            "issue",
            "list",
            "--repo",
            slug,
            "--state",
            "open",
            "--label",
            "in-progress",
            "--json",
            "number,title,updatedAt,comments",
            "--limit",
            "100",
        ],
        read_token,
        gh=gh,
    )
    open_prs: list[dict] | None = None
    if isinstance(in_prog, list) and in_prog and not local_session_running:
        candidates: list[tuple[int, str | None, float]] = []
        for issue in in_prog:
            if not isinstance(issue, dict) or not isinstance(issue.get("number"), int):
                continue
            claim_ts = _last_claim_timestamp(issue)
            if claim_ts is not None and ts_now - claim_ts < phantom_minutes * 60:
                continue
            candidates.append(
                (int(issue["number"]), _last_claim_session(issue), claim_ts or 0.0)
            )

        if candidates:
            active_gha = _has_active_actions_run(slug, read_token, gh=gh)
            # Fail open on API errors (`active_gha is None`): never strip a claim
            # when we couldn't verify GitHub Actions is idle.
            if active_gha is False:
                prs_raw = _gh_json(
                    [
                        "pr",
                        "list",
                        "--repo",
                        slug,
                        "--state",
                        "open",
                        "--json",
                        "number,title,body,headRefName,isDraft,author,updatedAt,createdAt,statusCheckRollup",
                        "--limit",
                        "100",
                    ],
                    read_token,
                    gh=gh,
                )
                if isinstance(prs_raw, list):
                    open_prs = [p for p in prs_raw if isinstance(p, dict)]
                    for num, claim_sess, claim_ts in candidates:
                        if any(_pr_backs_issue(pr, num, claim_ts) for pr in open_prs):
                            continue
                        released = False
                        if remediate:
                            released = _release_phantom_claim(
                                entry,
                                slug,
                                num,
                                claim_sess,
                                env,
                                app_token,
                                gh=gh,
                                run=run,
                            )
                            if released and live_pid is not None:
                                write_nudge(
                                    entry.path,
                                    f"genesis watch released stranded in-progress claim on issue #{num}",
                                )
                        action = "auto-released" if released else "still labelled in-progress"
                        findings.append(
                            Finding(
                                repo=entry.name,
                                kind="phantom_in_progress",
                                summary=f"issue #{num} had no running session, workflow, or PR ({action})",
                                remediated=released,
                            )
                        )

    # Check 4: starving work / green bot PR on an idle running plane.
    if (
        live_pid is not None
        and not manually_paused
        and not local_session_running
        and state is not None
        and state.get("state") == "idle"
        and not state.get("pending_followup")
    ):
        last_work_ts = max(
            float(state.get("last_turn_at") or 0.0),
            float(state.get("started_at") or 0.0),
        )
        if last_work_ts <= 0 or ts_now - last_work_ts >= starve_minutes * 60:
            gates = triggers.open_human_gates(slug, read_token)
            if gates == []:
                if open_prs is None:
                    prs_raw = _gh_json(
                        [
                            "pr",
                            "list",
                            "--repo",
                            slug,
                            "--state",
                            "open",
                            "--json",
                            "number,title,body,headRefName,isDraft,author,updatedAt,createdAt,statusCheckRollup",
                            "--limit",
                            "100",
                        ],
                        read_token,
                        gh=gh,
                    )
                    open_prs = [p for p in prs_raw if isinstance(p, dict)] if isinstance(prs_raw, list) else None

                bot_prs_pending = False
                starving_reason: str | None = None
                if open_prs is not None:
                    for pr in open_prs:
                        if pr.get("isDraft") or not is_bot_author(pr.get("author")):
                            continue
                        cstate = _checks_state(pr.get("statusCheckRollup") or [])
                        if cstate == "pending":
                            bot_prs_pending = True
                            break
                        updated = _parse_iso(pr.get("updatedAt"))
                        if cstate == "green" and updated is not None and ts_now - updated >= starve_minutes * 60:
                            starving_reason = f"green bot PR #{pr.get('number')} unmerged for {int((ts_now - updated) // 60)}m"
                            break

                if not bot_prs_pending and starving_reason is None:
                    agent_name = str(state.get("agent") or entry.agent or "")
                    is_evolver = "evolver" in agent_name
                    issues_raw = _gh_json(
                        [
                            "issue",
                            "list",
                            "--repo",
                            slug,
                            "--state",
                            "open",
                            "--json",
                            "number,labels,updatedAt",
                            "--limit",
                            "100",
                        ],
                        read_token,
                        gh=gh,
                    )
                    if isinstance(issues_raw, list):
                        for iss in issues_raw:
                            if not isinstance(iss, dict):
                                continue
                            labels = {
                                str(lbl.get("name") or "")
                                for lbl in (iss.get("labels") or [])
                                if isinstance(lbl, dict)
                            }
                            if labels & {"in-progress", "blocked", "needs:human"}:
                                continue
                            if is_evolver and "needs:evolver" not in labels:
                                continue
                            if not is_evolver and "needs:evolver" in labels:
                                continue
                            updated = _parse_iso(iss.get("updatedAt"))
                            if updated is not None and ts_now - updated >= starve_minutes * 60:
                                starving_reason = (
                                    f"open issue #{iss.get('number')} waiting while plane sits idle"
                                )
                                break

                if starving_reason:
                    nudges = (
                        watch_state.setdefault("nudges", {})
                        if isinstance(watch_state, dict)
                        else {}
                    )
                    if not isinstance(nudges, dict):
                        nudges = {}
                        if isinstance(watch_state, dict):
                            watch_state["nudges"] = nudges
                    prev_nudge = float(nudges.get(entry.name) or 0.0)
                    if remediate and (prev_nudge <= 0 or ts_now - prev_nudge >= starve_minutes * 60):
                        write_nudge(entry.path, f"genesis watch: {starving_reason}")
                        nudges[entry.name] = ts_now
                    # Alert only if an earlier nudge already fired and the repo
                    # is STILL starving past another full starve window.
                    if prev_nudge > 0 and ts_now - prev_nudge >= starve_minutes * 60:
                        findings.append(
                            Finding(
                                repo=entry.name,
                                kind="starving_work",
                                summary=starving_reason,
                                remediated=remediate,
                            )
                        )
                elif isinstance(watch_state, dict) and isinstance(watch_state.get("nudges"), dict):
                    watch_state["nudges"].pop(entry.name, None)

    return findings


def send_desktop_notification(title: str, message: str, *, run=subprocess.run) -> None:
    tn = "/opt/homebrew/bin/terminal-notifier"
    if os.path.exists(tn):
        run(
            [
                tn,
                "-title",
                title,
                "-message",
                message,
                "-group",
                "genesis-watch",
                "-sound",
                "Basso",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return
    if os.path.exists("/usr/bin/osascript"):
        script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
        run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, check=False)


def dispatch_aggregate_alert(
    findings: list[Finding],
    state: dict[str, object],
    *,
    now: float | None = None,
    renotify_minutes: float = 30.0,
    notify_enabled: bool = True,
    notifier=send_desktop_notification,
    loki_env: dict[str, str] | None = None,
) -> bool:
    """Send at most ONE consolidated alert across all checked repos."""
    ts_now = time.time() if now is None else now
    if not findings:
        state["active_keys"] = []
        return False

    active_keys = sorted({f"{f.repo}:{f.kind}" for f in findings})
    prev_keys = set(state.get("active_keys") or []) if isinstance(state.get("active_keys"), list) else set()
    last_notified = float(state.get("last_notified_at") or 0.0)

    escalated = bool(set(active_keys) - prev_keys)
    due_renotify = (ts_now - last_notified) >= renotify_minutes * 60
    should_notify = notify_enabled and (escalated or due_renotify)

    state["active_keys"] = active_keys
    if should_notify:
        repos_affected = sorted({f.repo for f in findings})
        noun = "repo" if len(repos_affected) == 1 else "repos"
        title = f"Genesis Watch ({len(repos_affected)} {noun})"
        body = "; ".join(f"{f.repo}: {f.summary}" for f in findings)
        notifier(title, body)
        state["last_notified_at"] = ts_now

    if loki_env is not None:
        _push_watch_loki(findings, should_notify, loki_env)

    return should_notify


def run_watch(
    *,
    config_path: str | Path | None = None,
    repo_dir: str | Path | None = None,
    state_path: str | Path | None = None,
    phantom_minutes: float = 5.0,
    hung_minutes: float = 20.0,
    starve_minutes: float = 15.0,
    renotify_minutes: float = 30.0,
    notify: bool = True,
    remediate: bool = True,
    as_json: bool = False,
) -> int:
    if repo_dir is not None:
        p = Path(repo_dir).expanduser().resolve()
        repos = [FleetRepo(name=p.name, path=p, enabled=True)]
    else:
        cfg = load_fleet_config(config_path)
        repos = [r for r in cfg.repos if r.enabled]

    s_path = watch_state_path(state_path)
    w_state = _load_watch_state(s_path)
    now = time.time()

    all_findings: list[Finding] = []
    for entry in repos:
        all_findings.extend(
            check_repo(
                entry,
                now=now,
                phantom_minutes=phantom_minutes,
                hung_minutes=hung_minutes,
                starve_minutes=starve_minutes,
                remediate=remediate,
                watch_state=w_state,
            )
        )

    loki_env = load_loki_env() if notify else None
    notified = dispatch_aggregate_alert(
        all_findings,
        w_state,
        now=now,
        renotify_minutes=renotify_minutes,
        notify_enabled=notify,
        loki_env=loki_env,
    )
    _save_watch_state(s_path, w_state)

    if as_json:
        print(
            json.dumps(
                {
                    "ok": len(all_findings) == 0,
                    "notified": notified,
                    "findings": [asdict(f) for f in all_findings],
                },
                indent=2,
            )
        )
    elif not all_findings:
        print(f"genesis watch: ok ({len(repos)} repo{'s' if len(repos) != 1 else ''} healthy)")
    else:
        for f in all_findings:
            print(f"[{f.repo}] {f.kind}: {f.summary}")

    return 0 if not all_findings else 1
