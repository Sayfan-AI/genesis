"""Genesis's own pre-session net: start every served session on an up-to-date main.

The served evolver works in a clone that stays on whatever branch the previous
session left, and it reads its charter from the working tree. On the first
served run that branch belonged to a pull request that had already merged, so
the next session would have read a charter one fix out of date.
"""

import json
import subprocess
from pathlib import Path

REPO = Path(__file__).parents[2]
SCRIPT = REPO / ".genesis" / "scripts" / "pre-session.sh"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _clone_with_origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    (origin / "f").write_text("1\n")
    _git(origin, "add", "f")
    _git(origin, "commit", "-q", "-m", "one")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    (origin / "f").write_text("2\n")
    _git(origin, "commit", "-q", "-am", "two")
    return clone


def _run(cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT)], cwd=cwd, capture_output=True, text=True)


def test_a_clean_clone_on_a_stale_branch_moves_to_current_main(tmp_path) -> None:
    clone = _clone_with_origin(tmp_path)
    _git(clone, "switch", "-q", "-c", "merged-feature")
    proc = _run(clone)
    assert proc.returncode == 0
    assert _git(clone, "branch", "--show-current") == "main"
    assert _git(clone, "log", "-1", "--format=%s") == "two", "main was not fast-forwarded"


def test_uncommitted_work_is_never_moved(tmp_path) -> None:
    clone = _clone_with_origin(tmp_path)
    _git(clone, "switch", "-q", "-c", "wip")
    (clone / "f").write_text("edited\n")
    proc = _run(clone)
    assert proc.returncode == 0
    assert _git(clone, "branch", "--show-current") == "wip"
    assert (clone / "f").read_text() == "edited\n"


def test_it_is_declared_on_session_start_so_both_modes_run_it() -> None:
    settings = json.loads((REPO / ".claude" / "settings.json").read_text())
    commands = [
        h.get("command", "")
        for entry in settings.get("hooks", {}).get("SessionStart", [])
        for h in entry.get("hooks", [])
    ]
    assert "bash .genesis/scripts/pre-session.sh" in commands
