"""Scaffolded shell scripts must be LF on every host (issue #158).

A dev system scaffolded from Windows committed all eight seeded
`.genesis/scripts/*.sh` as CRLF, and every one of them died on its first line
with `set: pipefail\\r: invalid option name`. Two things produced it, and each
has its own guard here:

- **The write.** `Path.write_text` in text mode turns `\\n` into the host's line
  separator. The tests can't run on Windows, so they make `write_text` behave as
  it does there (translate when no `newline` is passed) and check that no
  scaffolded byte is a carriage return. That's the same branch CPython takes on
  Windows, where the translation is compiled in and can't be switched on any
  other way.
- **Git.** Even LF files come back CRLF from a Windows clone with
  `core.autocrlf=true`, and a commit from a host without it can put CRLF in the
  blobs. The seeded `.gitattributes` pins `*.sh` to LF, and the tests ask git
  itself, through a real clone, rather than reparsing the attribute.
"""

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from genesis import scaffold
from genesis.scaffold import (
    GITATTRIBUTES_RULE,
    GITATTRIBUTES_TEMPLATE,
    TEMPLATES_DIR,
    scaffold_existing_repo,
    scaffold_external_dev_repo,
    scaffold_new_repo,
)

GOAL = "Keep shell scripts runnable on every host"
PROJECT = "eoltest"
REPO_ROOT = Path(__file__).parents[2]


def _new(path: Path) -> Path:
    scaffold_new_repo(path, GOAL, PROJECT)
    return path


def _existing(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    (path / "README.md").write_bytes(b"existing\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], check=True, capture_output=True)
    scaffold_existing_repo(path, GOAL, PROJECT)
    return path


def _external(path: Path) -> Path:
    scaffold_external_dev_repo(path, ["owner/target"], GOAL, PROJECT)
    return path


SCAFFOLDERS: dict[str, Callable[[Path], Path]] = {
    "new": _new,
    "existing": _existing,
    "external": _external,
}


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GENESIS_CONFIG_DIR", str(tmp_path / "config"))


@pytest.fixture
def windows_text_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `Path.write_text` translate newlines the way it does on Windows."""
    original = Path.write_text

    def write_text(self, data, encoding=None, errors=None, newline=None):  # type: ignore[no-untyped-def]
        if newline is None:
            data = data.replace("\n", "\r\n")
            newline = ""
        return original(self, data, encoding, errors, newline)

    monkeypatch.setattr(Path, "write_text", write_text)


def _files_with_cr(root: Path) -> list[str]:
    return sorted(
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(root).parts and b"\r" in p.read_bytes()
    )


def test_simulated_windows_text_mode_does_translate(tmp_path: Path, windows_text_mode: None) -> None:
    """Control: without this the next test could pass because the fake did nothing."""
    probe = tmp_path / "probe"
    probe.write_text("a\nb\n")
    assert probe.read_bytes() == b"a\r\nb\r\n"


@pytest.mark.parametrize("kind", SCAFFOLDERS)
def test_scaffold_writes_lf_under_windows_text_mode(tmp_path: Path, windows_text_mode: None, kind: str) -> None:
    repo = SCAFFOLDERS[kind](tmp_path / "repo")
    assert _files_with_cr(repo) == []


def test_local_env_is_lf_under_windows_text_mode(tmp_path: Path, windows_text_mode: None) -> None:
    """`activate.sh` sources this file, so a CR would end up inside every secret."""
    env = scaffold.ensure_local_env()
    assert b"\r" not in env.read_bytes()


def test_crlf_templates_still_scaffold_lf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Genesis checked out on Windows with `core.autocrlf=true` has CRLF templates.

    The scaffold reads them as text, whose universal newlines drop the CR, so the
    seeded files come out LF anyway. A byte-for-byte copy would carry the CR
    straight into the dev repo, and this is the test that says so.
    """
    crlf_templates = tmp_path / "templates"
    shutil.copytree(TEMPLATES_DIR, crlf_templates)
    for path in crlf_templates.rglob("*"):
        if path.is_file():
            path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    monkeypatch.setattr(scaffold, "TEMPLATES_DIR", crlf_templates)

    repo = _new(tmp_path / "repo")
    scripts = sorted((repo / ".genesis" / "scripts").glob("*.sh"))
    assert scripts
    assert [s.name for s in scripts if b"\r" in s.read_bytes()] == []


@pytest.mark.parametrize("kind", SCAFFOLDERS)
def test_seeded_scripts_are_lf_in_git_and_in_an_autocrlf_clone(tmp_path: Path, kind: str) -> None:
    repo = SCAFFOLDERS[kind](tmp_path / "repo")

    eol = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--eol", ".genesis/scripts"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert eol
    assert [line for line in eol if not line.startswith("i/lf")] == []

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "-c", "core.autocrlf=true", "clone", "-q", str(repo), str(clone)],
        check=True,
        capture_output=True,
    )
    scripts = sorted((clone / ".genesis" / "scripts").glob("*.sh"))
    assert scripts
    assert [s.name for s in scripts if b"\r" in s.read_bytes()] == []
    for script in scripts:
        subprocess.run(["bash", "-n", str(script)], check=True, capture_output=True)


def test_without_the_attribute_an_autocrlf_clone_gets_crlf(tmp_path: Path) -> None:
    """Control for the clone test: autocrlf really does rewrite on this git."""
    repo = _new(tmp_path / "repo")
    (repo / ".gitattributes").unlink()
    subprocess.run(["git", "-C", str(repo), "commit", "-qam", "drop attributes"], check=True, capture_output=True)

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "-c", "core.autocrlf=true", "clone", "-q", str(repo), str(clone)],
        check=True,
        capture_output=True,
    )
    assert b"\r\n" in (clone / ".genesis" / "scripts" / "issues.sh").read_bytes()


def test_existing_gitattributes_is_extended_not_replaced(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    (repo / ".gitattributes").write_bytes(b"*.png binary")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)

    scaffold_existing_repo(repo, GOAL, PROJECT)
    content = (repo / ".gitattributes").read_text()
    assert content.startswith("*.png binary\n\n")
    assert GITATTRIBUTES_RULE in content.splitlines()

    scaffold._write_gitattributes(repo)
    assert (repo / ".gitattributes").read_text() == content


def test_genesis_and_template_carry_the_same_rule() -> None:
    """The pair: genesis copies its scripts out of its own checkout and runs
    `.genesis/scripts/` itself, so it needs the rule as much as a dev repo does."""
    for path in (REPO_ROOT / ".gitattributes", TEMPLATES_DIR / GITATTRIBUTES_TEMPLATE):
        lines = [line.split() for line in path.read_text().splitlines()]
        assert GITATTRIBUTES_RULE.split() in lines, path
