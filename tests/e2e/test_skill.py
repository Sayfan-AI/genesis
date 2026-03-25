"""Tests for the genesis-new skill definition."""

from pathlib import Path

SKILL_PATH = Path(__file__).parent.parent.parent / ".claude" / "skills" / "genesis-new" / "SKILL.md"


def test_skill_file_exists() -> None:
    assert SKILL_PATH.exists(), f"Skill file not found at {SKILL_PATH}"


def test_skill_has_frontmatter() -> None:
    content = SKILL_PATH.read_text()
    assert content.startswith("---")
    # Find the closing ---
    second_marker = content.index("---", 3)
    frontmatter = content[3:second_marker]
    assert "name: genesis-new" in frontmatter
    assert "description:" in frontmatter


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
