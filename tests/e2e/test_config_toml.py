"""The scaffolded `.genesis/config.toml` must parse as TOML (issue #133).

`config.toml.j2` rendered the goal as `goal = "{{ goal }}"`. Goals are
multi-line and quote things, so every scaffolded config was invalid TOML, and
the existing tests read the file as text and never noticed. Once
`configured_engine()` started parsing it, the failure turned live: a parse
error falls back to the default engine, so a Codex dev system with a
multi-line goal ran on Claude without a word.
"""

import subprocess
import tomllib
from pathlib import Path

import pytest

from genesis import server
from genesis.scaffold import (
    scaffold_existing_repo,
    scaffold_external_dev_repo,
    scaffold_new_repo,
    toml_string,
)

# Everything a goal written by a person tends to contain, plus what TOML
# forbids raw in a string: newlines, quotes (including a run of three, which
# would close a multi-line string), a backslash, a line starting with `name`
# (which the line-based readers in log.sh and _project_name() key on), a tab,
# CRLF, a stray control character, and a trailing quote right before the
# closing delimiter.
HOSTILE_GOAL = (
    'Build a "tiny" game.\n'
    "name = not-the-project\n"
    'Paths like C:\\temp and a run of """ three quotes.\r\n'
    "\tindented line\x07 with a bell\n"
    "[engine]\n"
    'ends in a quote"'
)
PROJECT = "hostile-goal"


def _parse(repo: Path) -> dict:
    return tomllib.loads((repo / ".genesis" / "config.toml").read_text())


def _topologies(tmp_dir: Path, engine: str) -> list[Path]:
    new = tmp_dir / f"new-{engine}"
    scaffold_new_repo(new, HOSTILE_GOAL, PROJECT, engine=engine)

    existing = tmp_dir / f"existing-{engine}"
    existing.mkdir()
    subprocess.run(["git", "init", "-q", str(existing)], check=True)
    scaffold_existing_repo(existing, HOSTILE_GOAL, PROJECT, engine=engine)

    external = tmp_dir / f"external-{engine}"
    targets = [str(tmp_dir / f"target-{engine}-a"), str(tmp_dir / f"target-{engine}-b")]
    for target in targets:
        subprocess.run(["git", "init", "-q", target], check=True)
    scaffold_external_dev_repo(external, targets, HOSTILE_GOAL, PROJECT, engine=engine)
    return [new, existing, external]


@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_every_topology_writes_a_config_that_parses_and_round_trips(tmp_dir: Path, engine: str) -> None:
    for repo in _topologies(tmp_dir, engine):
        config = _parse(repo)
        assert config["project"]["name"] == PROJECT, repo.name
        assert config["project"]["goal"] == HOSTILE_GOAL, repo.name
        assert config["engine"]["name"] == engine, repo.name


def test_the_external_topology_round_trips_its_target_repos(tmp_dir: Path) -> None:
    targets = [str(tmp_dir / 'odd "name"\\a'), str(tmp_dir / "plain")]
    for target in targets:
        subprocess.run(["git", "init", "-q", target], check=True)
    dev = tmp_dir / "dev"
    scaffold_external_dev_repo(dev, targets, HOSTILE_GOAL, PROJECT)
    assert _parse(dev)["targets"]["repos"] == targets


def test_a_codex_system_with_a_multi_line_goal_runs_on_codex(tmp_dir: Path, monkeypatch) -> None:
    """The live consequence of #133: the plane reads the engine from this file."""
    repo = tmp_dir / "codex"
    scaffold_new_repo(repo, HOSTILE_GOAL, PROJECT, engine="codex")
    monkeypatch.chdir(repo)
    monkeypatch.delenv("GENESIS_ENGINE", raising=False)
    assert server.configured_engine() == "codex"


def test_the_line_based_project_readers_still_find_the_project_name(tmp_dir: Path, monkeypatch) -> None:
    """log.sh and _project_name() grep the first `name` line. The goal's own
    `name = ...` line must not get there first, or it becomes the Loki label."""
    repo = tmp_dir / "readers"
    scaffold_new_repo(repo, HOSTILE_GOAL, PROJECT)
    monkeypatch.chdir(repo)
    assert server._project_name() == PROJECT


@pytest.mark.parametrize(
    "value",
    ["", "plain", '"', '"""', "\\", "a\nb", "\r\n", "\t", "\x00\x1f\x7f", "ünïcödé ✓", 'x"'],
)
@pytest.mark.parametrize("multiline", [False, True])
def test_toml_string_round_trips(value: str, multiline: bool) -> None:
    assert tomllib.loads(f"v = {toml_string(value, multiline=multiline)}")["v"] == value
