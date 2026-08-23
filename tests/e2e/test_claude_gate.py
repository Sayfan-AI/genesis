"""The `.claude/` write gate is a pair, and genesis holds only one half of it.

Genesis issue #74, and the same class as #72 in the same direction: a fix landed
in `templates/` and never crossed back into the tree genesis runs itself from.

Issue #49 measured the mechanism — writes anywhere under `.claude/` are refused
for every tool, and nothing relaxes it short of `bypassPermissions`, so the answer
is a *gate* rather than a grant. `templates/scripts/claude-dir-guard.sh` intercepts
the write and tells the agent to post the exact edit on the task issue under
`needs:human`. Every repo genesis scaffolds gets the script and the `PreToolUse`
declaration that arms it. Genesis had neither, while `.claude/agents/evolver.md`
told its own evolver it could modify `.claude/agents/`.

Why nothing reported it, which is the property these tests exist to change: a
fresh checkout never attempts a `.claude/` write, so CI cannot see the difference
and `main` stays green either way. Same blind spot that hid the ignore rule. The
guard has to be a test that names both halves.

The behaviour of the script is not retested here — `tests/unit/test_claude_dir_guard.py`
covers that against the template copy, and `test_the_two_copies_are_identical`
below is what makes that coverage transitive.
"""

import json
import re
import subprocess
from pathlib import Path

GENESIS_REPO = Path(__file__).parents[2]

GUARD_SCRIPT = "claude-dir-guard.sh"

# The seeded half: what genesis writes into a dev repo.
TEMPLATE_GUARD = GENESIS_REPO / "templates" / "scripts" / GUARD_SCRIPT
TEMPLATE_SETTINGS = GENESIS_REPO / "templates" / "settings.json"

# Genesis's own half. The script sits at the *same repo-relative path* a
# scaffolded repo uses, which is not cosmetic: it makes the hook command string
# identical on both sides, so the declaration can be compared as text, and it
# lets the two copies of the script be byte-identical even though the script
# names its own path in the message it prints.
OWN_GUARD = GENESIS_REPO / ".genesis" / "scripts" / GUARD_SCRIPT
OWN_SETTINGS = GENESIS_REPO / ".claude" / "settings.json"
OWN_EVOLVER = GENESIS_REPO / ".claude" / "agents" / "evolver.md"

HOOK_COMMAND = f"bash .genesis/scripts/{GUARD_SCRIPT}"


def _pre_tool_use_commands(settings: Path) -> list[str]:
    """Every command declared on `PreToolUse` in a settings file.

    Flattened across the `matcher` + `hooks` nesting that Claude Code requires,
    because a gate declared in the second entry is as armed as one in the first
    and a test that reads `[0]` fails on ordering rather than on behaviour.
    """
    hooks = (json.loads(settings.read_text()).get("hooks") or {}).get("PreToolUse") or []
    return [h["command"] for entry in hooks for h in (entry.get("hooks") or [])]


def _tracked(path: Path) -> bool:
    """True when git tracks `path` in genesis's own repo."""
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", str(path.relative_to(GENESIS_REPO))],
        cwd=GENESIS_REPO,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def test_genesis_carries_the_guard_script() -> None:
    """The script has to exist in the tree genesis's own evolver runs in."""
    assert OWN_GUARD.is_file(), (
        f"{OWN_GUARD.relative_to(GENESIS_REPO)} is missing. `genesis-evolver.yml` "
        "and `genesis serve` both run agents against this repo, whose .claude/ "
        "holds the evolver's own definition — the one directory its charter points "
        "at and the one the harness refuses to write"
    )


def test_the_guard_script_is_tracked() -> None:
    """An untracked script is not there on a runner.

    `genesis-evolver.yml` starts from `actions/checkout`, so a guard that exists
    only on one developer's disk is armed in exactly the mode that has nobody to
    ask. `.genesis/` in this repo is otherwise per-machine runtime state, which is
    what makes this worth asserting rather than assuming.
    """
    assert _tracked(OWN_GUARD), (
        f"{OWN_GUARD.relative_to(GENESIS_REPO)} is not tracked by git, so it does "
        "not exist in an Actions checkout — the mode where a stalled write has no "
        "human to fall back on"
    )


def test_the_two_copies_are_identical() -> None:
    """The pairing itself, and the thing that drifts.

    Byte-identical rather than merely equivalent, because equivalence needs a
    judgement call and this is the check that has to survive someone improving one
    copy in a hurry. It is also what makes the template copy's unit tests cover
    genesis's copy.
    """
    assert OWN_GUARD.read_bytes() == TEMPLATE_GUARD.read_bytes(), (
        "the two copies of the .claude/ gate have diverged. Whichever side was "
        "improved, port it to the other: an improvement that reaches only the "
        "seeded copy leaves genesis's own evolver on the old behaviour, and one "
        "that reaches only genesis never ships to a dev system at all"
    )


def test_both_halves_declare_the_gate_on_pre_tool_use() -> None:
    """A script on disk that nothing declares does nothing.

    That is not a hypothetical: it is the precise failure this gate exists to
    report (#49), and it is the state genesis was in — the template's declaration
    was the only one in the repo.
    """
    for label, settings in (
        ("templates/settings.json", TEMPLATE_SETTINGS),
        (".claude/settings.json", OWN_SETTINGS),
    ):
        commands = _pre_tool_use_commands(settings)
        assert any(GUARD_SCRIPT in c for c in commands), (
            f"{label} does not run {GUARD_SCRIPT} on PreToolUse, so the gate is a "
            f"file doing nothing. Declared PreToolUse commands: {commands}"
        )


def test_both_halves_declare_the_path_the_script_is_at() -> None:
    """A declaration pointing at a path with no script is inert too, and quietly:
    `bash` on a missing file exits non-zero, PreToolUse treats only 2 as a block,
    so the call sails through and the gate reports nothing."""
    for label, settings in (
        ("templates/settings.json", TEMPLATE_SETTINGS),
        (".claude/settings.json", OWN_SETTINGS),
    ):
        commands = _pre_tool_use_commands(settings)
        assert HOOK_COMMAND in commands, (
            f"{label} declares the gate at a path that is not {HOOK_COMMAND!r}. "
            "Both halves keep the script at the same repo-relative path on "
            f"purpose. Declared PreToolUse commands: {commands}"
        )


def test_the_evolver_charter_does_not_promise_a_write_it_cannot_make() -> None:
    """`What You Can Modify` listed `.claude/agents/`, which the harness refuses.

    An agent told it holds a capability it doesn't spends turns discovering
    otherwise and leaves no diagnosis — `error_max_turns`, which this repo's
    CLAUDE.md calls the worst failure shape in the system, and which this workflow
    has already hit twice.

    Only that section is read. The rest of the file should absolutely mention
    `.claude/`; saying what to do instead is the other half of the fix.
    """
    body = OWN_EVOLVER.read_text()
    match = re.search(r"^## What You Can Modify$(.*?)(?=^## |\Z)", body, re.M | re.S)
    assert match, (
        "`.claude/agents/evolver.md` no longer has a `## What You Can Modify` "
        "section. If it was renamed, retarget this guard rather than dropping it"
    )
    offenders = [
        line.strip()
        for line in match.group(1).splitlines()
        if ".claude/" in line or ".claude`" in line
    ]
    assert not offenders, (
        "the evolver charter lists a path under .claude/ as modifiable, and the "
        f"harness refuses every write there: {offenders}. Genesis's evolver cannot "
        "edit its own definition; it proposes the edit on the task issue under "
        "`needs:human` instead"
    )


def test_the_evolver_charter_says_what_to_do_instead() -> None:
    """Deleting the false promise without replacing it just moves the dead end.

    The agent still needs the edit somewhere it *can* land, and `needs:human` is
    the protocol the gate's own message names — so the charter and the gate have
    to tell the same story.
    """
    body = OWN_EVOLVER.read_text()
    assert "needs:human" in body, (
        "`.claude/agents/evolver.md` drops the write it cannot make but never says "
        "where the edit goes. The gate tells a blocked agent to post the exact "
        "edit on the task issue and label it `needs:human`; the charter has to "
        "agree, or the agent's own definition contradicts the hook that stops it"
    )
