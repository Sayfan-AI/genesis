"""Core scaffolding logic for genesis.

Creates and augments repositories with autonomous dev system scaffolding.
"""

import subprocess
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

TEMPLATES_DIR = Path(__file__).parent.parent.parent / "templates"

SEED_AGENTS = [
    "onboarding",
    "project_manager",
    "human_interaction",
    "introspective",
    "health",
]

SEED_WORKFLOWS = [
    "genesis-orchestrator.yml",
    "genesis-events.yml",
]


def _render_template(name: str, **kwargs: object) -> str:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        keep_trailing_newline=True,
    )
    template = env.get_template(name)
    return template.render(**kwargs)


def _git(repo_path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo_path), *args],
        check=True,
        capture_output=True,
    )


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _write_seed_agents(base: Path) -> None:
    agents_dir = base / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    for agent in SEED_AGENTS:
        src = TEMPLATES_DIR / "agents" / f"{agent}.md"
        dst = agents_dir / f"{agent}.md"
        dst.write_text(src.read_text())


def _write_seed_workflows(base: Path) -> None:
    workflows_dir = base / ".github" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    for workflow in SEED_WORKFLOWS:
        src = TEMPLATES_DIR / "workflows" / workflow
        dst = workflows_dir / workflow
        dst.write_text(src.read_text())


def _write_settings(base: Path) -> None:
    settings_path = base / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    src = TEMPLATES_DIR / "settings.json"
    settings_path.write_text(src.read_text())


def _write_genesis_config(
    base: Path,
    project_name: str,
    goal: str,
    target_repos: list[str] | None = None,
) -> None:
    content = _render_template(
        "config.toml.j2",
        project_name=project_name,
        goal=goal,
        target_repos=target_repos or [],
    )
    _write_file(base / ".genesis" / "config.toml", content)


def _write_onboarding_issue(
    base: Path,
    project_name: str,
    goal: str,
    target_repos: list[str] | None = None,
) -> None:
    content = _render_template(
        "onboarding_issue.md.j2",
        project_name=project_name,
        goal=goal,
        target_repos=target_repos or [],
    )
    _write_file(base / ".genesis" / "onboarding.md", content)


def scaffold_new_repo(
    path: Path,
    goal: str,
    project_name: str,
) -> None:
    """Create a new repo with full dev system scaffolding (embedded)."""
    path.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        ["git", "init", str(path)],
        check=True,
        capture_output=True,
    )

    # CLAUDE.md
    claude_md = _render_template(
        "claude_md.md.j2",
        project_name=project_name,
        goal=goal,
        target_repos=[],
    )
    _write_file(path / "CLAUDE.md", claude_md)

    # README.md
    readme = _render_template(
        "readme.md.j2",
        project_name=project_name,
        goal=goal,
    )
    _write_file(path / "README.md", readme)

    # Seed agents, workflows, settings, config
    _write_seed_agents(path)
    _write_seed_workflows(path)
    _write_settings(path)
    _write_genesis_config(path, project_name, goal)

    # Onboarding issue
    _write_onboarding_issue(path, project_name, goal)

    # Initial commit
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "Initial scaffold by genesis")


def scaffold_existing_repo(
    path: Path,
    goal: str,
    project_name: str,
) -> None:
    """Add dev system scaffolding to an existing repo (embedded)."""
    if not (path / ".git").is_dir():
        raise ValueError(f"{path} is not a git repository")

    # Check for existing CLAUDE.md
    claude_md_path = path / "CLAUDE.md"
    claude_md_content = _render_template(
        "claude_md.md.j2",
        project_name=project_name,
        goal=goal,
        target_repos=[],
    )

    if claude_md_path.exists():
        # Append genesis section to existing CLAUDE.md
        existing = claude_md_path.read_text()
        separator = "\n\n---\n\n# Genesis Dev System\n\n"
        claude_md_path.write_text(existing + separator + claude_md_content)
    else:
        _write_file(claude_md_path, claude_md_content)

    # Seed agents, workflows, settings, config
    _write_seed_agents(path)
    _write_seed_workflows(path)
    _write_settings(path)
    _write_genesis_config(path, project_name, goal)

    # Onboarding issue
    _write_onboarding_issue(path, project_name, goal)

    # Commit
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "Add genesis dev system scaffolding")


def scaffold_external_dev_repo(
    dev_path: Path,
    target_repos: list[str],
    goal: str,
    project_name: str,
) -> None:
    """Create a separate dev repo that manages work across target repos."""
    dev_path.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        ["git", "init", str(dev_path)],
        check=True,
        capture_output=True,
    )

    # CLAUDE.md with target repo references
    claude_md = _render_template(
        "claude_md.md.j2",
        project_name=project_name,
        goal=goal,
        target_repos=target_repos,
    )
    _write_file(dev_path / "CLAUDE.md", claude_md)

    # README.md
    readme = _render_template(
        "readme.md.j2",
        project_name=project_name,
        goal=goal,
    )
    _write_file(dev_path / "README.md", readme)

    # Seed agents, workflows, settings, config
    _write_seed_agents(dev_path)
    _write_seed_workflows(dev_path)
    _write_settings(dev_path)
    _write_genesis_config(dev_path, project_name, goal, target_repos)

    # Onboarding issue with target repo references
    _write_onboarding_issue(dev_path, project_name, goal, target_repos)

    # Initial commit
    _git(dev_path, "add", "-A")
    _git(dev_path, "commit", "-m", "Initial scaffold by genesis")
