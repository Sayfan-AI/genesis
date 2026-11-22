"""The scheduled run's human gate, in both of its homes (issue #143).

`genesis-orchestrator.yml`'s "Skip the run if a human gate is open" step and the
plane's `_gated` are one rule in two languages: bash and jq on a runner, Python
under `genesis serve`. They're checked here by running both over the same
boards through the same stub `gh`, rather than by reading either, the shape
`test_automerge.py` uses for the protected-path gate.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from genesis import server
from genesis.scaffold import TEMPLATES_DIR

ORCHESTRATOR_WORKFLOW = TEMPLATES_DIR / "workflows" / "genesis-orchestrator.yml"
JQ = shutil.which("jq")

pytestmark = pytest.mark.skipif(JQ is None, reason="the workflow step needs jq")

# A `gh issue list` stand-in reading the board from $BOARD. It applies
# `--label`, `--state` and `--jq` the way gh does, which is all either
# implementation asks of it.
STUB_GH = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
board = json.loads(os.environ["BOARD"])
a = sys.argv[1:]
if a[:2] != ["issue", "list"]:
    sys.exit(0)
def opt(name):
    return a[a.index(name) + 1] if name in a else None
state = opt("--state") or "open"
if state in board.get("fail", []):
    print("HTTP 502", file=sys.stderr); sys.exit(1)
rows = board["open"] if state == "open" else [
    {"number": 1000 + n, "title": t, "labels": []} for n, t in enumerate(board["closed"])
]
label = opt("--label")
out = []
for r in rows:
    names = r.get("labels", [])
    if label and label not in names:
        continue
    out.append({
        "number": r["number"], "title": r["title"], "createdAt": "2026-11-01T00:00:00Z",
        "labels": [{"name": n} for n in names],
    })
text = json.dumps(out)
jq = opt("--jq")
if jq:
    text = subprocess.run(["jq", "-c", jq], input=text, capture_output=True, text=True, check=True).stdout
print(text)
'''


def _issue(number: int, title: str, *labels: str) -> dict:
    return {"number": number, "title": title, "labels": list(labels)}


SECRETS_GATE = _issue(73, "Add the VAPID repo secrets", "needs:human", "milestone:2")
BLOCKED_TASK = _issue(62, "Web push", "milestone:2", "blocked")
READY_BUG = _issue(84, "Flaky journey test", "milestone:2", "bug")
M1_DONE = ["Milestone 1 complete"]

BOARDS = {
    # Sweatless as issue #143 measured it: a narrow gate, the two tasks it
    # blocks, and a ready bug fix nobody ran.
    "narrow gate, ready work": ({"open": [SECRETS_GATE, BLOCKED_TASK, READY_BUG], "closed": M1_DONE}, False),
    "narrow gate, everything else blocked": ({"open": [SECRETS_GATE, BLOCKED_TASK], "closed": M1_DONE}, True),
    "narrow gate, other work claimed or escalated": ({"open": [
        SECRETS_GATE,
        _issue(85, "Being worked", "milestone:2", "in-progress"),
        _issue(86, "Framework bug", "milestone:2", "needs:evolver"),
    ], "closed": M1_DONE}, True),
    # Milestone 3's issues aren't `next --milestone 2`'s to hand out.
    "narrow gate, ready work only on a later milestone": (
        {"open": [SECRETS_GATE, _issue(90, "Later", "milestone:3")], "closed": M1_DONE}, True,
    ),
    # Before the milestone 1 sign-off, milestone 1 is the active one.
    "narrow gate, ready work on milestone 1 before its sign-off": (
        {"open": [SECRETS_GATE, _issue(9, "Task", "milestone:1")], "closed": []}, False,
    ),
    # The gates the orchestrator's rules say to wait behind stop the run even
    # with selectable work on the board.
    "plan gate, ready work": ({"open": [
        _issue(80, "Milestone 2 plan", "needs:human"), READY_BUG,
    ], "closed": M1_DONE}, True),
    "completion gate, ready work": ({"open": [
        _issue(81, "Milestone 2 complete", "needs:human"), READY_BUG,
    ], "closed": M1_DONE}, True),
    "onboarding, ready work": ({"open": [
        _issue(1, "Build a thing", "needs:human", "genesis:onboarding"), READY_BUG,
    ], "closed": []}, True),
    # A dead run's escalation isn't a gate at all.
    "only an automation failure": ({"open": [
        _issue(70, "Run died", "needs:human", "automation:failure"),
    ], "closed": M1_DONE}, False),
    "no gate": ({"open": [BLOCKED_TASK], "closed": M1_DONE}, False),
    # Fails open: one idle session beats a cron silenced by a gh hiccup.
    "narrow gate, closed issues unreadable": (
        {"open": [SECRETS_GATE, BLOCKED_TASK], "closed": M1_DONE, "fail": ["closed"]}, False,
    ),
}


def _bin(tmp_path: Path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "gh"
    stub.write_text(STUB_GH)
    stub.chmod(0o755)
    return bindir


def _gate_step_script() -> str:
    text = ORCHESTRATOR_WORKFLOW.read_text()
    match = re.search(
        r"- name: Skip the run if a human gate is open\n.*?\n        run: \|\n(.*?)\n\n      #",
        text, re.S,
    )
    assert match, "genesis-orchestrator.yml lost its human-gate step"
    return "\n".join(line[10:] for line in match.group(1).splitlines())


def _workflow_skips(board: dict, tmp_path: Path) -> bool:
    out = tmp_path / "out"
    out.write_text("")
    env = {
        "PATH": f"{_bin(tmp_path)}:{Path(JQ).parent}:/usr/bin:/bin",
        "BOARD": json.dumps(board),
        "GITHUB_OUTPUT": str(out),
        "GH_REPO": "o/r",
    }
    # `-e`, as a runner's default shell runs it.
    subprocess.run(["bash", "-e", "-c", _gate_step_script()], env=env, check=True, capture_output=True)
    line = [l for l in out.read_text().splitlines() if l.startswith("skip=")][-1]
    return line == "skip=true"


def _plane_skips(board: dict, tmp_path: Path, monkeypatch) -> bool:
    monkeypatch.setenv("PATH", f"{_bin(tmp_path)}:{Path(JQ).parent}:/usr/bin:/bin")
    monkeypatch.setenv("BOARD", json.dumps(board))
    plane = server.LocalControlPlane(repo="o/r", poll_interval=1, session_timeout=5)
    return plane._gated("scheduled run", "tok")


@pytest.mark.parametrize("name", list(BOARDS))
def test_the_workflow_and_the_plane_gate_the_same_boards(name, tmp_path, monkeypatch) -> None:
    board, skips = BOARDS[name]
    assert _workflow_skips(board, tmp_path) is skips, f"workflow: {name}"
    assert _plane_skips(board, tmp_path, monkeypatch) is skips, f"plane: {name}"
