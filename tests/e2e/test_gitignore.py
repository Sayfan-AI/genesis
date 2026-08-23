"""The scaffolded `.gitignore` must match the CLASS of `genesis serve` runtime
state, not a list of today's filenames.

Why this exists (genesis issue #40, found in Sayfan-AI/MaKlaude and fixed there in
MaKlaude PR #153). The section listing local control-plane state was an
enumeration of four names. `genesis serve` then began writing a fifth,
`.genesis/.trigger-state`, and nothing ignored it, so every local-mode run left
it untracked. Nothing failed: CI can't see it (a fresh checkout has no runtime
state), `main` stayed green, and the only symptom was a `??` line that every
agent reading `git status` had to look past — until some `git add -A` path swept
per-machine state into the repo. Appending the fifth name would leave the shape
that produced the miss, so the fix is a pattern and these tests pin the pattern
rather than the names.

Everything below asks *git* whether a path is ignored. Reparsing `.gitignore` in
Python would test a reimplementation of the matcher instead of the matcher.

`--no-index` is load-bearing, and it is the trap this issue warned about: by
default `git check-ignore` consults the index and reports every **tracked** path
as un-ignored, because gitignore genuinely has no effect on tracked files. True,
and useless for the negative control below — the scaffold's own initial commit
tracks `.genesis/config.toml` and every seeded script, so without the flag the
control answers "not ignored" however over-broad the pattern gets.

Measured, not assumed. Scaffold a repo, then broaden the rule to `.genesis/*` the
way a careless fix would: with `--no-index` the control fails on all three
tracked paths, without it the control passes on all three. `--no-index` is also
the question that matters in practice — the next new file under
`.genesis/scripts/` isn't tracked yet, and an over-broad rule swallows it
silently.
"""

import re
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from genesis.scaffold import (
    GITIGNORE_PATTERN,
    scaffold_existing_repo,
    scaffold_external_dev_repo,
    scaffold_new_repo,
)


GOAL = "Keep the working tree clean across local runs"
PROJECT = "ignoretest"

SOURCE_DIR = Path(__file__).parents[2] / "src" / "genesis"

# `.genesis/` paths genesis's own source names that are tracked repo content
# rather than per-machine state. Anything else genesis references under
# `.genesis/` and does NOT name as a dotfile breaks the convention the ignore
# pattern encodes, and `test_genesis_writes_runtime_state_as_dotfiles` says so.
TRACKED_GENESIS_CONTENT = {
    ".genesis/config.toml",  # read by the control plane, written by the scaffold
    ".genesis/scripts/issues.sh",  # seeded script the control plane shells out to
    ".genesis/scripts/pre-session.sh",  # optional hook the dev repo supplies
}


def _ignored(repo: Path, path: str) -> bool:
    """Ask git whether `path` is ignored in `repo`, ignoring the index."""
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", "--", path],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if result.returncode in (0, 1):
        return result.returncode == 0
    raise AssertionError(f"git check-ignore {path}: rc={result.returncode} {result.stderr}")


def _genesis_paths_in_source() -> set[str]:
    """Every `.genesis/...` path literal genesis's own source names."""
    found: set[str] = set()
    for module in SOURCE_DIR.glob("*.py"):
        found.update(re.findall(r'"(\.genesis/[^"]+)"', module.read_text()))
    # The ignore pattern itself is a rule, not a path.
    found.discard(GITIGNORE_PATTERN)
    return found


@pytest.fixture
def scaffolded(tmp_dir: Path) -> Path:
    repo = tmp_dir / PROJECT
    scaffold_new_repo(repo, GOAL, PROJECT)
    return repo


def test_a_runtime_file_genesis_has_not_invented_yet_is_ignored(scaffolded: Path) -> None:
    """The load-bearing case, and the one an enumeration can never pass.

    Neither name exists on disk, deliberately: the rule has to hold for the next
    file `genesis serve` writes, not only for today's set. Reverting the template
    to the four-name list fails here and nowhere else.
    """
    for name in (
        ".genesis/.some-runtime-file-genesis-has-not-invented-yet",
        ".genesis/.another-one",
    ):
        assert _ignored(scaffolded, name), (
            f"{name} is not ignored: the scaffolded .gitignore enumerates genesis "
            "runtime state instead of matching the class, so the next file "
            "`genesis serve` writes will dirty the working tree the way "
            ".trigger-state did"
        )


def test_every_known_runtime_file_is_ignored(scaffolded: Path) -> None:
    """The regression half — every runtime file observed in the wild."""
    for name in (
        ".genesis/.disabled-by-genesis",  # the workflow set genesis serve paused
        ".genesis/.orchestrator.lock",  # cross-mode concurrency guard
        ".genesis/.poll-etag",  # event-poll cursor
        ".genesis/.poll-highwater",  # event-poll cursor
        ".genesis/.trigger-state",  # the one the enumeration missed
    ):
        assert _ignored(scaffolded, name), f"{name} is written by every local-mode run"


def test_tracked_genesis_content_is_not_ignored(scaffolded: Path) -> None:
    """The negative control, and the reason the pattern is `.genesis/.*` rather
    than `.genesis/*`.

    A pattern is only as good as what it declines to match: swallowing the dev
    system's own config and scripts would be a worse failure than the untracked
    file this change removes. The last path is the one that makes the control
    bite in practice — a script the dev system hasn't written yet, which no index
    entry can vouch for.
    """
    for name in (
        ".genesis/config.toml",
        ".genesis/onboarding.md",
        ".genesis/scripts/issues.sh",
        ".genesis/scripts/log.sh",
        ".genesis/scripts/a-tool-the-dev-system-adds-later.sh",
    ):
        assert not _ignored(scaffolded, name), (
            f"{name} is ignored, but it is tracked dev-system content, not "
            "per-machine runtime state"
        )


def test_genesis_writes_runtime_state_as_dotfiles(scaffolded: Path) -> None:
    """The pattern is complete only while genesis keeps its side of the
    convention, so the convention is asserted against genesis's own source.

    A new `Path(".genesis/something")` that isn't a dotfile would be runtime state
    the pattern can't see — the enumeration failure one level up. Either name it
    as a dotfile or add it to TRACKED_GENESIS_CONTENT and mean it.
    """
    literals = _genesis_paths_in_source()
    assert ".genesis/.trigger-state" in literals, (
        "this guard reads genesis's source for `.genesis/...` literals and found "
        "none of the known runtime paths — the scan broke, not the convention"
    )
    for literal in sorted(literals):
        name = literal.split("/", 1)[1]
        if name.startswith("."):
            assert _ignored(scaffolded, literal), f"{literal} is runtime state and is not ignored"
        else:
            assert literal in TRACKED_GENESIS_CONTENT, (
                f"{literal} is a non-dotfile path under .genesis/. If genesis writes "
                "it at runtime, name it as a dotfile so the scaffolded ignore "
                "pattern covers it; if it is tracked repo content, add it to "
                "TRACKED_GENESIS_CONTENT"
            )


@pytest.mark.parametrize("scaffold", ["new", "external"])
def test_every_scaffold_path_writes_the_ignore_rule(tmp_dir: Path, scaffold: str) -> None:
    """A rule the scaffold forgets to write protects nobody. Both repo-creating
    paths are checked; the adopted-repo path has its own cases below, since it
    has to merge rather than write.
    """
    repo = tmp_dir / f"{PROJECT}-{scaffold}"
    if scaffold == "new":
        scaffold_new_repo(repo, GOAL, PROJECT)
    else:
        scaffold_external_dev_repo(repo, ["owner/target"], GOAL, PROJECT)

    assert GITIGNORE_PATTERN in (repo / ".gitignore").read_text()
    assert _ignored(repo, ".genesis/.trigger-state")


def test_an_adopted_repo_keeps_its_own_ignore_rules(
    init_test_repo: Callable[[str, dict[str, str]], Path],
) -> None:
    """Genesis is a guest in an adopted repo: append, never replace."""
    repo = init_test_repo(PROJECT, {".gitignore": "node_modules/\n*.log\n"})
    scaffold_existing_repo(repo, GOAL, PROJECT)

    content = (repo / ".gitignore").read_text()
    assert "node_modules/" in content
    assert GITIGNORE_PATTERN in content
    assert _ignored(repo, ".genesis/.trigger-state")
    assert _ignored(repo, "node_modules/thing.js")


def test_re_scaffolding_does_not_stack_duplicate_sections(
    init_test_repo: Callable[[str, dict[str, str]], Path],
) -> None:
    """Re-scaffolding is how an adopted repo picks up a template change, and a
    section appended once per run turns the file into a log."""
    repo = init_test_repo(PROJECT, {".gitignore": "*.log\n"})
    scaffold_existing_repo(repo, GOAL, PROJECT)
    once = (repo / ".gitignore").read_text()
    scaffold_existing_repo(repo, GOAL, PROJECT)

    assert (repo / ".gitignore").read_text() == once
    # The rule itself, not the prose above it that also spells the pattern out.
    assert once.splitlines().count(GITIGNORE_PATTERN) == 1


def test_an_adopted_repo_without_a_gitignore_gets_one(
    init_test_repo: Callable[[str, dict[str, str]], Path],
) -> None:
    repo = init_test_repo(PROJECT, {"README.md": "# app\n"})
    scaffold_existing_repo(repo, GOAL, PROJECT)

    assert _ignored(repo, ".genesis/.trigger-state")


# --- genesis's own repo -----------------------------------------------------
#
# Everything above interrogates an artifact genesis produced. These ask the same
# questions of genesis itself, which is served by the control plane it ships:
# `genesis serve` runs against this repo and writes the same runtime state here.
#
# It didn't ignore any of it (issue #72). The rule landed in `templates/gitignore`
# for issue #40 and never crossed back, which is the drift class CLAUDE.md
# documents for `.github/workflows/` vs `templates/workflows/`, running in the
# other direction. Nothing reported it for the same reason the original went
# unnoticed: a fresh checkout has no runtime state, so CI cannot see the
# difference and `main` stays green.

GENESIS_REPO = Path(__file__).parents[2]


def test_genesis_ignores_the_runtime_state_it_writes_here() -> None:
    """Genesis is one of the repos `genesis serve` runs against, so it needs the
    rule it seeds. The unnamed file matters most for the same reason it does in a
    scaffolded repo — the rule has to hold for the next thing serve writes."""
    for name in (
        ".genesis/.disabled-by-genesis",
        ".genesis/.orchestrator.lock",
        ".genesis/.poll-etag",
        ".genesis/.poll-highwater",
        ".genesis/.trigger-state",
        ".genesis/.some-runtime-file-genesis-has-not-invented-yet",
    ):
        assert _ignored(GENESIS_REPO, name), (
            f"{name} is not ignored in genesis's own repo. `genesis serve` runs "
            "here too, and this tree has no tracked .genesis/ content, so git "
            "collapses the lot to one `?? .genesis/` line and a single `git add "
            "-A` commits a PID, two poll cursors and one machine's "
            "paused-workflow list"
        )


def test_genesis_does_not_over_ignore_its_own_dot_genesis() -> None:
    """The same negative control the scaffolded repo gets.

    This tree has no tracked `.genesis/` content today, which is precisely what
    makes the over-broad `.genesis/*` look harmless here — and it would be copied
    back to the template as an improvement.
    """
    for name in (
        ".genesis/config.toml",
        ".genesis/scripts/issues.sh",
    ):
        assert not _ignored(GENESIS_REPO, name), (
            f"{name} is ignored: genesis's own rule has been broadened past the "
            "dotfile convention the template depends on"
        )


def test_genesis_and_its_template_carry_the_same_rule() -> None:
    """The pairing itself, which is the thing that drifted.

    The two cases above would also pass on a rule that merely behaves right
    today. This one fails if the pattern is ever changed on one side of the pair
    and not the other — the failure mode that produced issue #72 and, before it,
    issues #4, #11, #14, #15 and #22.
    """
    template = (GENESIS_REPO / "templates" / "gitignore").read_text()
    own = (GENESIS_REPO / ".gitignore").read_text()

    for label, content in (("templates/gitignore", template), (".gitignore", own)):
        assert any(line.strip() == GITIGNORE_PATTERN for line in content.splitlines()), (
            f"{label} no longer contains the rule `{GITIGNORE_PATTERN}` that "
            "src/genesis/scaffold.py writes and tests for. Change both sides of "
            "the pair, or the one that wasn't changed silently stops matching"
        )
