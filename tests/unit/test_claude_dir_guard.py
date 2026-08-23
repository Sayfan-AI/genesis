"""Behavior tests for templates/scripts/claude-dir-guard.sh.

The gate adds no restriction — the harness already refuses these writes. What it
adds is an instruction, delivered at the moment the agent would otherwise stall on
"Claude requested permissions to write to <path>, but you haven't granted it yet"
with nobody in the loop to ask (genesis issue #49).

So the tests come in pairs, and the two directions do not cost the same:

- A **missed write** leaves the original failure intact: the agent gets the bare
  permission string, has no idea what to do with it, and the task dies holding a
  change it could have described in a comment.
- A **false positive** is worse than a nuisance here, because the obvious over-broad
  rule catches *reads*. The evolver's whole job is to change `.claude/`, and it
  starts by reading what's there. A gate that refuses `cat .claude/settings.json`
  blocks the diagnosis as well as the cure, and teaches the agent to route around
  gates — the failure host-guard.sh's own notes warn about.
"""

import json
import subprocess
from pathlib import Path

import pytest

GUARD = Path(__file__).parents[2] / "templates" / "scripts" / "claude-dir-guard.sh"

BLOCKED = 2  # PreToolUse treats exit 2 as "refuse this call"
ALLOWED = 0


def run_guard(tool_name, tool_input):
    payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input})
    proc = subprocess.run(
        ["bash", str(GUARD)], input=payload, capture_output=True, text=True
    )
    return proc.returncode, proc.stderr


# ---------- the writes that stall, in the shapes an agent actually emits ----------


@pytest.mark.parametrize(
    "path",
    [
        ".claude/settings.json",
        ".claude/agents/orchestrator.md",
        "./.claude/agents/worker.md",
        "/home/runner/work/proj/proj/.claude/settings.json",
        ".claude/skills/deploy/SKILL.md",
    ],
)
def test_a_write_under_claude_is_gated(path) -> None:
    for tool in ("Write", "Edit", "MultiEdit"):
        code, err = run_guard(tool, {"file_path": path})
        assert code == BLOCKED, f"{tool} {path} was not gated"
        assert "claude-dir-guard.sh" in err


def test_the_bash_redirect_route_is_gated_too() -> None:
    """Measured: `printf ... >> .claude/agents/worker.md` is refused by the
    harness exactly like the Edit tool is, so the protection is path-based rather
    than tool-based. An agent that reaches for Bash after Edit fails needs the
    same instruction, not a second dead end."""
    for command in (
        "printf 'x\\n' >> .claude/agents/worker.md",
        "echo '{}' > .claude/settings.json",
        "cat foo.json > ./.claude/settings.json",
        "tee .claude/agents/new.md < /tmp/body",
        "mkdir -p .claude/skills/deploy",
    ):
        code, err = run_guard("Bash", {"command": command})
        assert code == BLOCKED, f"not gated: {command}"
        assert "claude-dir-guard.sh" in err


# ---------- the reads and the neighbours, which must stay out of the way ----------


@pytest.mark.parametrize(
    "command",
    [
        "cat .claude/settings.json",
        "grep -rn host-guard .claude/",
        "ls -la .claude/agents",
        "git diff .claude/settings.json",
        "git add .claude/settings.json",
    ],
)
def test_reading_claude_is_never_gated(command) -> None:
    """The evolver's charter is to change `.claude/`, and it reads before it
    writes. Refusing the read blocks the diagnosis as well as the cure."""
    code, _ = run_guard("Bash", {"command": command})
    assert code == ALLOWED, f"a read was gated: {command}"


def test_the_read_tool_is_never_gated() -> None:
    code, _ = run_guard("Read", {"file_path": ".claude/agents/orchestrator.md"})
    assert code == ALLOWED


@pytest.mark.parametrize(
    "path",
    [
        "CLAUDE.md",
        "src/app/claude.py",
        "docs/dot-claude-layout.md",
        ".github/workflows/genesis-orchestrator.yml",
        ".genesis/scripts/log.sh",
        "myclaude/settings.json",
    ],
)
def test_writes_outside_claude_are_never_gated(path) -> None:
    """`.claude` as a path *segment*, not as a substring.

    `myclaude/` and `src/app/claude.py` contain the letters and are ordinary
    project files. A gate that matched the substring would refuse them, and
    `CLAUDE.md` in particular is the alternative home the gate's own message
    tells the agent to use — refusing it would make the advice impossible to
    follow.
    """
    code, _ = run_guard("Write", {"file_path": path})
    assert code == ALLOWED, f"an unrelated write was gated: {path}"


# ---------- the message is the whole point ----------


def test_the_message_names_the_next_action_not_just_the_refusal() -> None:
    """A gate that says "no" reproduces the stall it was built to remove.

    The original failure wasn't that the write was refused - it's that the refusal
    carried no next step, so a milestone task sat waiting on a human to paste two
    lines of JSON. Every element below is load-bearing: where to put the edit, in
    what form, and how to flag it.
    """
    _, err = run_guard("Write", {"file_path": ".claude/settings.json"})
    assert "comment on the task issue" in err.lower()
    assert "diff" in err.lower()
    assert "needs:human" in err
    assert "CLAUDE.md" in err, "prose has an alternative home and the message must say so"
    assert "will not work" in err, "the agent must be told not to retry in another shape"


# ---------- failing open ----------


def test_a_payload_it_cannot_parse_does_not_wedge_the_loop() -> None:
    """Same trade as host-guard.sh: a bug in a hook that runs on every tool call
    must not be able to stop the run."""
    for payload in ("", "not json", "[]", '{"tool_name": "Write"}'):
        proc = subprocess.run(
            ["bash", str(GUARD)], input=payload, capture_output=True, text=True
        )
        assert proc.returncode == ALLOWED, f"wedged on payload: {payload!r}"
