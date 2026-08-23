"""Behavior tests for templates/scripts/tag-milestone.sh.

The tag is derived from repository state rather than created at the moment a
milestone is signed off, and that's the whole point. The sign-off run is also the
planning run, so "also create a tag" is exactly the step that gets dropped when a
run is busy or dies partway through — and nothing ever reports a missing tag,
because a missing tag looks identical to nothing at all. Same invisible-absence
shape as an unmilestoned issue or an unanswered comment.

So the tests are mostly about the script being safe to run at any time, from any
state: idempotent, self-detecting, and never leaving behind a local-only tag that
would make it skip a milestone forever.
"""

import os
import subprocess
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).parents[2] / "templates"
TAG_SH = TEMPLATES / "scripts" / "tag-milestone.sh"

# macOS ships bash 3.2 and that's what `genesis serve` runs locally.
BASH = "/bin/bash"

FAKE_GH = """#!/bin/sh
# Records the call, then answers from the environment. Real gh applies --jq to
# its own output, so this does too: pull the expression out of the argv rather
# than assuming a position.
echo "$*" >> "$GH_CALLS"

JQ_EXPR=""
prev=""
for a in "$@"; do
  [ "$prev" = "--jq" ] && JQ_EXPR="$a"
  prev="$a"
done

if [ -n "$JQ_EXPR" ]; then
  printf '%s' "$GH_ISSUES_JSON" | jq -r "$JQ_EXPR"
else
  printf '%s' "$GH_ISSUES_JSON"
fi
"""


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """A repo with a real `origin`, so a pushed tag is observable."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    _git("init", "-q", "-b", "main", ".", cwd=work)
    _git("config", "user.email", "dev@x", cwd=work)
    _git("config", "user.name", "dev", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)
    (work / "f.txt").write_text("x\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "seed", cwd=work)
    _git("push", "-q", "-u", "origin", "main", cwd=work)
    return work


def run_tagger(repo, issues, *args, gh_fails_push=False):
    """Run the script with a `gh` that answers from a canned issue list.

    The stub applies the script's own `--jq` to the fixture rather than returning
    a pre-filtered answer: which titles count as a completion gate *is* that jq
    expression, so hand-filtering would leave the part most likely to be wrong
    untested.
    """
    bindir = repo / "fakebin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    if gh_fails_push:
        # A remote that refuses the push, without touching the working repo.
        _git("remote", "set-url", "--push", "origin", "/nonexistent/origin.git", cwd=repo)

    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["GH_CALLS"] = str(repo / "gh-calls.log")
    env["GH_ISSUES_JSON"] = issues
    return subprocess.run(
        [BASH, str(TAG_SH), *args], cwd=repo, capture_output=True, text=True, env=env
    )


def tags(repo, where="refs/tags"):
    out = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", where],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    return sorted(out.stdout.split())


NONE = "[]"
ONE = '[{"number": 9, "title": "Milestone 1 complete", "closedAt": "2026-01-01T00:00:00Z"}]'
THREE = """[
  {"number": 9, "title": "Milestone 1 complete", "closedAt": "2026-01-01T00:00:00Z"},
  {"number": 20, "title": "Milestone 2 complete", "closedAt": "2026-02-01T00:00:00Z"},
  {"number": 31, "title": "milestone 3 COMPLETE", "closedAt": "2026-03-01T00:00:00Z"}
]"""


def test_a_signed_off_milestone_gets_a_tag(repo) -> None:
    result = run_tagger(repo, ONE)
    assert result.returncode == 0, result.stderr
    assert tags(repo) == ["milestone-1"]


def test_the_tag_reaches_the_remote(repo) -> None:
    """A tag only on the machine that ran the loop isn't a checkpoint anyone else
    can check out, which is the entire request."""
    run_tagger(repo, ONE)
    remote = subprocess.run(
        ["git", "ls-remote", "--tags", "origin"], cwd=repo,
        capture_output=True, text=True, check=True,
    )
    assert "milestone-1" in remote.stdout


def test_every_signed_off_milestone_is_caught_up_at_once(repo) -> None:
    """Self-detecting, so a run that skipped the tag — or a repo that adopted the
    script late — is repaired by the next run rather than staying wrong."""
    run_tagger(repo, THREE)
    assert tags(repo) == ["milestone-1", "milestone-2", "milestone-3"]


def test_the_completion_gate_is_matched_case_insensitively(repo) -> None:
    """`milestone 3 COMPLETE` in the fixture. An agent's capitalisation drifting
    must not silently stop the tagging."""
    run_tagger(repo, THREE)
    assert "milestone-3" in tags(repo)


def test_running_it_again_changes_nothing(repo) -> None:
    """It's wired into a Hard Rule that fires on every run that sees a sign-off,
    so the no-op path is the common one."""
    run_tagger(repo, ONE)
    before = subprocess.run(
        ["git", "rev-parse", "milestone-1"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout
    result = run_tagger(repo, ONE)

    assert result.returncode == 0
    assert "already tagged" in result.stdout
    after = subprocess.run(
        ["git", "rev-parse", "milestone-1"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout
    assert before == after, "an existing tag was moved"


def test_no_signed_off_milestones_is_not_an_error(repo) -> None:
    result = run_tagger(repo, NONE)
    assert result.returncode == 0
    assert tags(repo) == []


def test_one_milestone_can_be_named(repo) -> None:
    run_tagger(repo, THREE, "2")
    assert tags(repo) == ["milestone-2"]


def test_a_tag_that_cannot_be_pushed_is_not_left_behind_locally(repo) -> None:
    """The expensive failure mode, and the reason the rollback exists.

    A local-only tag makes every later run skip that milestone — `git rev-parse`
    finds it — while nobody else can see it. The milestone then stays permanently
    untagged on the remote, and the script reports success. Deleting it means the
    next run retries.
    """
    result = run_tagger(repo, ONE, gh_fails_push=True)

    assert tags(repo) == [], "a tag that never reached the remote was kept locally"
    assert "could not push" in result.stderr


def test_the_tags_carry_who_and_when(repo) -> None:
    """Annotated rather than lightweight: a lightweight tag is a bare pointer, and
    the point of the checkpoint is being able to ask when a milestone landed."""
    run_tagger(repo, ONE)
    kind = subprocess.run(
        ["git", "cat-file", "-t", "milestone-1"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert kind == "tag", "milestone tags should be annotated, not lightweight"
