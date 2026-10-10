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
import os
import subprocess
from pathlib import Path

import pytest

GUARD = Path(__file__).parents[2] / "templates" / "scripts" / "claude-dir-guard.sh"

BLOCKED = 2  # PreToolUse treats exit 2 as "refuse this call"
ALLOWED = 0


REPO = "/home/runner/work/proj/proj"
HOME = "/home/runner"


def run_guard(tool_name, tool_input, *, project_dir=None, cwd=None):
    """Run the guard the way the harness does.

    With neither `project_dir` nor `cwd`, the guard can't locate the repo and
    falls back to gating any `.claude/` segment. Most cases below run that way on
    purpose: it's the conservative branch, and a missing variable must never be
    what disarms the gate. `CLAUDE_PROJECT_DIR` is stripped from the inherited
    environment so a test run from inside a Claude Code session stays hermetic.
    """
    body = {"tool_name": tool_name, "tool_input": tool_input}
    if cwd is not None:
        body["cwd"] = cwd
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    env["HOME"] = HOME
    if project_dir is not None:
        env["CLAUDE_PROJECT_DIR"] = project_dir
    proc = subprocess.run(
        ["bash", str(GUARD)],
        input=json.dumps(body),
        capture_output=True,
        text=True,
        env=env,
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


# ---------- only the repo's own .claude/ (genesis issue #89) ----------


@pytest.mark.parametrize(
    "path",
    [
        f"{HOME}/.claude/projects/-home-runner-work-proj-proj/memory/fact.md",
        f"{HOME}/.claude/CLAUDE.md",
        f"{HOME}/.claude/skills/mine/SKILL.md",
        "/home/runner/work/other/other/.claude/settings.json",
        "../other/.claude/settings.json",
    ],
)
def test_a_claude_dir_outside_the_repo_is_not_gated(path) -> None:
    """The user's ~/.claude/ holds auto-memory, the personal CLAUDE.md and user
    skills, and the harness lets a session write all three. Gating them sent the
    agent to file a needs:human comment for a write that would have landed."""
    for tool in ("Write", "Edit"):
        code, _ = run_guard(tool, {"file_path": path}, project_dir=REPO, cwd=REPO)
        assert code == ALLOWED, f"{tool} {path} was gated"


@pytest.mark.parametrize(
    "path",
    [
        ".claude/settings.json",
        "./.claude/agents/worker.md",
        f"{REPO}/.claude/settings.json",
        f"{REPO}/src/../.claude/agents/x.md",
        "../.claude/settings.json",
    ],
)
def test_the_repo_claude_dir_is_still_gated_when_the_root_is_known(path) -> None:
    """`../.claude/` from a subdirectory climbs back into the repo, and a
    relative path is read against the session's cwd, not the root."""
    code, _ = run_guard(
        "Write", {"file_path": path}, project_dir=REPO, cwd=f"{REPO}/src"
        if path.startswith("../") else REPO
    )
    assert code == BLOCKED, f"{path} was not gated"


def test_the_payload_cwd_locates_the_repo_when_the_env_var_is_absent() -> None:
    code, _ = run_guard("Write", {"file_path": f"{HOME}/.claude/CLAUDE.md"}, cwd=REPO)
    assert code == ALLOWED
    code, _ = run_guard("Write", {"file_path": f"{REPO}/.claude/settings.json"}, cwd=REPO)
    assert code == BLOCKED


@pytest.mark.parametrize(
    "command",
    [
        "echo '- fact' >> ~/.claude/projects/x/memory/MEMORY.md",
        "printf x > $HOME/.claude/CLAUDE.md",
        f"cp notes.md {HOME}/.claude/skills/mine/SKILL.md",
    ],
)
def test_bash_writes_to_the_user_claude_dir_are_not_gated(command) -> None:
    code, _ = run_guard("Bash", {"command": command}, project_dir=REPO, cwd=REPO)
    assert code == ALLOWED, f"gated: {command}"


@pytest.mark.parametrize(
    "command",
    [
        "printf x >> .claude/agents/worker.md",
        f"echo '{{}}' > {REPO}/.claude/settings.json",
        "cat > .claude/agents/new.md <<'EOF'\nbody\nEOF",
    ],
)
def test_bash_writes_to_the_repo_claude_dir_are_still_gated(command) -> None:
    code, _ = run_guard("Bash", {"command": command}, project_dir=REPO, cwd=REPO)
    assert code == BLOCKED, f"not gated: {command}"


def test_a_heredoc_body_that_quotes_a_redirect_is_not_a_write() -> None:
    """Measured on genesis issue #89: an issue body describing the gate quoted a
    redirect onto a .claude/ path, and the whole `gh issue create` was refused.
    A heredoc body is data. The redirect on the header line is still a write."""
    command = (
        'gh issue create --title t --body "$(cat <<\'EOF\'\n'
        "the agent ran printf x >> .claude/agents/worker.md and was refused\n"
        'EOF\n)"'
    )
    code, _ = run_guard("Bash", {"command": command}, project_dir=REPO, cwd=REPO)
    assert code == ALLOWED
    code, _ = run_guard("Bash", {"command": command})
    assert code == ALLOWED, "heredoc bodies are data even when the root is unknown"


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
