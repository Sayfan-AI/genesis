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

What this file does NOT cover any more: that genesis carries the script, tracks
it, keeps it byte-identical to the template, and declares it on `PreToolUse`.
Those are properties every seeded guard shares, so they moved to
`tests/e2e/test_seeded_pairs.py`, which checks them parameterized over the whole
set. What stays here is the part unique to this guard - the evolver charter, which
is the only agent definition that ever claimed the capability the harness refuses.
"""

import re
from pathlib import Path

GENESIS_REPO = Path(__file__).parents[2]

OWN_EVOLVER = GENESIS_REPO / ".claude" / "agents" / "evolver.md"

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
