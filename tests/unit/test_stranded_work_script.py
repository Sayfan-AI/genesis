"""Behaviour tests for templates/scripts/stranded-work.sh (genesis issue #156).

A session under `genesis serve` can end between its commit and its push, which
leaves finished work on a local branch that nothing else can see. The script
runs before every session and names those branches. Each test builds a real
clone with a real origin and stubs only `gh`, because what's under test is the
two-part rule: commits missing from the default branch by patch, *and* a branch
name that was never a pull request head. Either half alone is wrong. Patch
alone names every squash-merged branch. PR heads alone can't tell a branch with
work from one without.

Runs under /bin/bash, which on macOS is 3.2 and is what `genesis serve` gets.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).parents[2] / "templates" / "scripts"
SCRIPT = TEMPLATES / "stranded-work.sh"
PRE_SESSION = TEMPLATES / "pre-session.sh"
BASH = "/bin/bash"

FAKE_GH = """#!/bin/sh
if [ -n "$GH_FAIL" ]; then
  echo "gh: not authenticated" >&2
  exit 1
fi
cat "$GH_HEADS"
"""


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, name: str, content: str) -> None:
    (repo / name).write_text(content)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"add {name}")


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _commit(origin, "f", "1\n")
    repo = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(repo))
    return repo


@pytest.fixture
def gh(tmp_path: Path) -> dict:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "gh"
    fake.write_text(FAKE_GH)
    fake.chmod(0o755)
    heads = tmp_path / "heads.txt"
    heads.write_text("")
    return {"bindir": bindir, "heads": heads}


def _run(repo: Path, gh: dict, script: Path = SCRIPT, **env: str) -> subprocess.CompletedProcess:
    full_env = {k: v for k, v in os.environ.items() if k != "GH_FAIL"}
    full_env.update(
        PATH=f"{gh['bindir']}{os.pathsep}{os.environ['PATH']}",
        GH_HEADS=str(gh["heads"]),
        **env,
    )
    return subprocess.run(
        [BASH, str(script)], cwd=repo, capture_output=True, text=True, env=full_env
    )


def test_a_committed_branch_that_never_had_a_pull_request_is_named(clone, gh) -> None:
    _git(clone, "switch", "-q", "-c", "evolver/forgotten")
    _commit(clone, "g", "work\n")
    _commit(clone, "h", "more\n")
    _git(clone, "switch", "-q", "main")
    proc = _run(clone, gh)
    assert proc.returncode == 0
    assert "STRANDED WORK" in proc.stdout
    assert "'evolver/forgotten' has 2 commit(s)" in proc.stdout


def test_a_squash_merged_branch_is_excluded_by_its_pull_request(clone, gh) -> None:
    # Its commits never reach main by patch, which is the whole reason the
    # pull-request half of the rule exists.
    _git(clone, "switch", "-q", "-c", "squashed")
    _commit(clone, "g", "work\n")
    _git(clone, "switch", "-q", "main")
    gh["heads"].write_text("other\nsquashed\n")
    proc = _run(clone, gh)
    assert proc.returncode == 0
    assert "STRANDED" not in proc.stdout


def test_a_pull_request_head_only_excludes_an_exact_name(clone, gh) -> None:
    _git(clone, "switch", "-q", "-c", "fix")
    _commit(clone, "g", "work\n")
    _git(clone, "switch", "-q", "main")
    gh["heads"].write_text("fix-later\nhotfix\n")
    proc = _run(clone, gh)
    assert "'fix' has 1 commit(s)" in proc.stdout


def test_a_branch_whose_work_is_on_main_is_not_named(clone, gh) -> None:
    _git(clone, "switch", "-q", "-c", "landed")
    _commit(clone, "g", "work\n")
    _git(clone, "switch", "-q", "main")
    # Land it on origin by patch, the way a rebase merge would, then fetch.
    origin = clone.parent / "origin"
    _commit(origin, "g", "work\n")
    _git(clone, "fetch", "-q", "origin")
    proc = _run(clone, gh)
    assert proc.returncode == 0
    assert "STRANDED" not in proc.stdout


def test_unpushed_commits_on_the_default_branch_itself_are_not_its_job(clone, gh) -> None:
    _commit(clone, "g", "work\n")
    proc = _run(clone, gh)
    assert "STRANDED" not in proc.stdout


def test_when_gh_fails_it_says_so_and_names_nothing(clone, gh) -> None:
    # Reporting every candidate here would name every squash-merged branch.
    _git(clone, "switch", "-q", "-c", "maybe")
    _commit(clone, "g", "work\n")
    _git(clone, "switch", "-q", "main")
    proc = _run(clone, gh, GH_FAIL="1")
    assert proc.returncode == 0
    assert "STRANDED" not in proc.stdout
    assert "couldn't list pull requests" in proc.stdout


def test_gh_is_not_called_when_no_branch_has_work(clone, gh) -> None:
    # It's on every session's critical path, so the common case stays local.
    proc = _run(clone, gh, GH_FAIL="1")
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_a_repo_without_an_origin_is_skipped_not_failed(tmp_path, gh) -> None:
    repo = tmp_path / "solo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "f", "1\n")
    proc = _run(repo, gh)
    assert proc.returncode == 0
    assert "skipped" in proc.stdout


def test_a_non_default_origin_head_is_followed(tmp_path, gh) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "trunk")
    _commit(origin, "f", "1\n")
    repo = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(repo))
    _git(repo, "switch", "-q", "-c", "work")
    _commit(repo, "g", "work\n")
    proc = _run(repo, gh)
    assert "not on origin/trunk" in proc.stdout
    assert "'trunk'" not in proc.stdout


def test_the_seeded_pre_session_runs_it(clone, gh) -> None:
    scripts = clone / ".genesis" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(SCRIPT, scripts / "stranded-work.sh")
    _git(clone, "switch", "-q", "-c", "forgotten")
    _commit(clone, "g", "work\n")
    _git(clone, "switch", "-q", "main")
    proc = _run(clone, gh, script=PRE_SESSION)
    assert proc.returncode == 0
    assert "'forgotten' has 1 commit(s)" in proc.stdout


def test_it_is_seeded_into_new_projects() -> None:
    from genesis import scaffold

    assert "stranded-work.sh" in scaffold.SEED_SCRIPTS
