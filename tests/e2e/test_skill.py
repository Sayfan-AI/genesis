"""Tests for the genesis-new skill definition across Claude Code and Codex."""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
CLAUDE_SKILL_DIR = REPO_ROOT / ".claude" / "skills" / "genesis-new"
CODEX_SKILL_DIR = REPO_ROOT / ".agents" / "skills" / "genesis-new"
SKILL_PATH = CLAUDE_SKILL_DIR / "SKILL.md"
CODEX_SKILL_PATH = CODEX_SKILL_DIR / "SKILL.md"
OPENAI_YAML_PATH = CODEX_SKILL_DIR / "agents" / "openai.yaml"


def test_skill_file_exists() -> None:
    assert SKILL_PATH.exists(), f"Skill file not found at {SKILL_PATH}"
    assert CODEX_SKILL_PATH.exists(), f"Codex skill file not found at {CODEX_SKILL_PATH}"


def test_codex_skill_links_to_claude_skill_so_they_cannot_drift() -> None:
    assert CODEX_SKILL_DIR.is_symlink(), (
        f"{CODEX_SKILL_DIR} must symlink to {CLAUDE_SKILL_DIR} so Claude Code "
        "and Codex read one skill definition instead of a pair that can drift"
    )
    assert CODEX_SKILL_PATH.resolve() == SKILL_PATH.resolve()


def test_codex_openai_yaml_metadata() -> None:
    assert OPENAI_YAML_PATH.exists(), f"Missing {OPENAI_YAML_PATH}"
    text = OPENAI_YAML_PATH.read_text()
    fields = dict(re.findall(r'^\s+([a-z_]+):\s+"([^"]+)"\s*$', text, re.M))
    assert fields.get("display_name")
    short_description = fields.get("short_description", "")
    assert 25 <= len(short_description) <= 64
    assert "$genesis-new" in fields.get("default_prompt", "")


def test_skill_has_frontmatter() -> None:
    content = SKILL_PATH.read_text()
    assert content.startswith("---")
    # Find the closing ---
    second_marker = content.index("---", 3)
    frontmatter = content[3:second_marker]
    assert "name: genesis-new" in frontmatter
    assert "description:" in frontmatter


def test_skill_has_no_machine_local_paths() -> None:
    content = SKILL_PATH.read_text()
    assert "/Users/" not in content
    assert "/home/" not in content


def test_skill_references_scaffold_functions() -> None:
    content = SKILL_PATH.read_text()
    assert "scaffold_new_repo" in content
    assert "scaffold_existing_repo" in content
    assert "scaffold_external_dev_repo" in content


def test_skill_references_github_integration() -> None:
    content = SKILL_PATH.read_text()
    assert "publish_to_github" in content
    assert "open_onboarding_issue" in content


def test_skill_covers_all_topologies() -> None:
    content = SKILL_PATH.read_text().lower()
    assert "new repo" in content
    assert "existing repo" in content
    assert "multi-repo" in content


def test_skill_mentions_setup_requirements() -> None:
    content = SKILL_PATH.read_text()
    assert "ANTHROPIC_API_KEY" in content
    assert "config.toml" in content
