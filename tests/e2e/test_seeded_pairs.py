"""Anything genesis seeds and also uses itself is a pair, and pairs drift.

This is the class behind more bugs in this repo than any other single cause.
Issues #4, #11, #14, #15 and #22 were one failure wearing five hats: a fix landed
in `.github/workflows/` and never reached `templates/workflows/`. Issue #72 was
the same failure running the other way, in `.gitignore`. Issues #74 and #76 were
the same failure again, one directory over, in the `PreToolUse` guards.

Each of those was found by hand, filed separately, and fixed separately. The
pattern only became visible once genesis started running its own control plane
against its own repo, because that is what turns "a template genesis ships" into
"a file genesis depends on."

Why nothing reported any of them: a template is never executed in the repo that
stores it, and a fresh checkout never exercises the seeded behaviour. CI cannot
see the difference, so `main` stays green across the whole drift. The guard has
to be a test that names both halves and compares them.

`tests/e2e/test_workflows.py` holds the workflow half of this and
`tests/e2e/test_gitignore.py` the ignore-rule half. This file holds the seeded
`PreToolUse` guards, and it is parameterized rather than written per guard, so
the next one genesis seeds is covered the day it is added to `SEEDED_GUARDS`
rather than after it has drifted for a month.
"""

import json
import subprocess
from pathlib import Path

import pytest

GENESIS_REPO = Path(__file__).parents[2]

# Every guard genesis seeds into a dev repo AND relies on in its own tree. A new
# guard belongs here the moment it is added to SEED_SCRIPTS and declared in
# templates/settings.json - that is the whole point of the list.
SEEDED_GUARDS = ["host-guard.sh", "claude-dir-guard.sh"]

TEMPLATE_SETTINGS = GENESIS_REPO / "templates" / "settings.json"
OWN_SETTINGS = GENESIS_REPO / ".claude" / "settings.json"


def _template_copy(script: str) -> Path:
    return GENESIS_REPO / "templates" / "scripts" / script


def _own_copy(script: str) -> Path:
    """Genesis's own copy, at the same repo-relative path a scaffolded repo uses.

    Not cosmetic. The identical path makes the hook command string identical on
    both sides, so the declaration can be compared as text, and it lets the two
    copies of a script be byte-identical even though a script names its own path
    in the message it prints.
    """
    return GENESIS_REPO / ".genesis" / "scripts" / script


def _pre_tool_use_commands(settings: Path) -> list[str]:
    """Every command declared on `PreToolUse`, flattened.

    Flattened across the `matcher` + `hooks` nesting Claude Code requires, because
    a guard declared in the second entry is as armed as one in the first, and a
    test that reads `[0]` fails on ordering rather than on behaviour.
    """
    hooks = (json.loads(settings.read_text()).get("hooks") or {}).get("PreToolUse") or []
    return [h["command"] for entry in hooks for h in (entry.get("hooks") or [])]


def _tracked(path: Path) -> bool:
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", str(path.relative_to(GENESIS_REPO))],
        cwd=GENESIS_REPO,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


@pytest.mark.parametrize("script", SEEDED_GUARDS)
def test_genesis_carries_the_guard_it_seeds(script) -> None:
    """Genesis runs the same control plane it ships, against its own repo."""
    assert _own_copy(script).is_file(), (
        f"genesis seeds {script} into every repo it creates and does not carry it "
        "itself, while `genesis serve` runs sessions here under the same agent "
        "definitions"
    )


@pytest.mark.parametrize("script", SEEDED_GUARDS)
def test_the_guard_is_tracked_by_git(script) -> None:
    """Existing on this machine is not the same as existing in the repo.

    `gcm` is `git commit -a`, which stages modified tracked files and silently
    skips a brand-new one, so a manifest entry, a hook declaration and a passing
    test can all land in a commit that does not contain the script. It passes
    locally forever and fails on any fresh clone.
    """
    assert _tracked(_own_copy(script)), (
        f".genesis/scripts/{script} exists here but git does not track it, so a "
        f"fresh clone gets a hook pointing at a missing file. `git add` it"
    )


@pytest.mark.parametrize("script", SEEDED_GUARDS)
def test_the_two_copies_are_identical(script) -> None:
    """Byte-identical, which is what makes the template's test coverage transitive.

    The behaviour of each guard is tested once, against the template copy, in
    `tests/unit/`. That coverage only says anything about the copy genesis runs if
    the two files cannot differ.
    """
    own, template = _own_copy(script), _template_copy(script)
    assert own.read_bytes() == template.read_bytes(), (
        f"{script} has diverged between templates/scripts/ and .genesis/scripts/. "
        "The unit tests cover the template copy, so the copy genesis actually runs "
        "is now untested"
    )


@pytest.mark.parametrize("script", SEEDED_GUARDS)
def test_both_halves_arm_the_guard(script) -> None:
    """A guard script is an inert file until something declares it.

    That is not hypothetical here: `host-guard.sh` shipped seeded and undeclared
    once already, and `claude-dir-guard.sh` was manifested but untracked. Both
    are the same failure - the file exists, the test that looks for the file
    passes, and nothing ever calls it.
    """
    command = f"bash .genesis/scripts/{script}"
    for label, settings in ((".claude/settings.json", OWN_SETTINGS),
                            ("templates/settings.json", TEMPLATE_SETTINGS)):
        commands = _pre_tool_use_commands(settings)
        assert command in commands, (
            f"{label} does not run {script} on PreToolUse, so the guard is a file "
            f"doing nothing. Declared PreToolUse commands: {commands}"
        )


def test_the_guards_run_in_the_same_order_on_both_sides() -> None:
    """Order is behaviour, not style.

    A guard that exits 2 blocks the call, so whichever runs first decides which
    refusal message the agent sees. Genesis reading its own commands in a
    different order from the repos it seeds means the two behave differently on a
    command both would refuse, which is the hardest kind of difference to notice.

    Only the seeded guards are compared. A scaffolded repo also logs on
    `PreToolUse` and genesis does not, so the lists are filtered to the guards
    rather than required to be equal.
    """
    def guard_order(settings: Path) -> list[str]:
        return [
            script
            for command in _pre_tool_use_commands(settings)
            for script in SEEDED_GUARDS
            if script in command
        ]

    assert guard_order(OWN_SETTINGS) == guard_order(TEMPLATE_SETTINGS), (
        "genesis runs its PreToolUse guards in a different order from the repos it "
        f"seeds: {guard_order(OWN_SETTINGS)} here against "
        f"{guard_order(TEMPLATE_SETTINGS)} in the template"
    )


def test_every_seeded_guard_is_in_the_pair_list() -> None:
    """The list above is the thing that rots, so it gets its own guard.

    Adding a guard to `templates/settings.json` and forgetting `SEEDED_GUARDS`
    would leave the new one unpaired and this whole file silently narrower than it
    reads. Derived from the template's own declarations rather than from
    `SEED_SCRIPTS`, because a script can be seeded without being a PreToolUse
    guard - `issues.sh` and `log.sh` are seeded and are not guards.
    """
    # The `.sh` token, not the last one: `log.sh` is declared with an argument
    # (`bash .genesis/scripts/log.sh pre-tool-use`), so the trailing word is the
    # hook event rather than the script.
    declared = {
        Path(token).name
        for command in _pre_tool_use_commands(TEMPLATE_SETTINGS)
        for token in command.split()
        if token.endswith(".sh")
    }
    # log.sh is declared on PreToolUse to record the call, not to gate it, and
    # genesis has no logging pipeline of its own to point it at.
    guards = {name for name in declared if name != "log.sh"}
    assert guards == set(SEEDED_GUARDS), (
        f"templates/settings.json declares {sorted(guards)} on PreToolUse but "
        f"SEEDED_GUARDS lists {sorted(SEEDED_GUARDS)}. Every guard genesis seeds "
        "and relies on itself has to be in the list, or it is not checked as a pair"
    )
