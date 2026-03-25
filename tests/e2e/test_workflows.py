"""Tests for workflow template content."""

from pathlib import Path

from genesis.scaffold import TEMPLATES_DIR, scaffold_new_repo


def test_orchestrator_workflow_uses_claude_action() -> None:
    content = (TEMPLATES_DIR / "workflows" / "genesis-orchestrator.yml").read_text()
    assert "anthropics/claude-code-action@v1" in content
    assert "ANTHROPIC_API_KEY" in content
    assert "cron:" in content
    assert "workflow_dispatch" in content


def test_events_workflow_uses_claude_action() -> None:
    content = (TEMPLATES_DIR / "workflows" / "genesis-events.yml").read_text()
    assert "anthropics/claude-code-action@v1" in content
    assert "ANTHROPIC_API_KEY" in content
    assert "issues:" in content
    assert "pull_request:" in content
    assert "issue_comment:" in content


def test_events_workflow_skips_bot_events() -> None:
    content = (TEMPLATES_DIR / "workflows" / "genesis-events.yml").read_text()
    assert "github-actions[bot]" in content


def test_workflows_have_correct_permissions() -> None:
    for name in ["genesis-orchestrator.yml", "genesis-events.yml"]:
        content = (TEMPLATES_DIR / "workflows" / name).read_text()
        assert "contents: write" in content
        assert "issues: write" in content
        assert "pull-requests: write" in content


def test_scaffolded_workflows_match_templates(tmp_dir: Path) -> None:
    repo = tmp_dir / "test-project"
    scaffold_new_repo(repo, "test goal", "test-project")

    for name in ["genesis-orchestrator.yml", "genesis-events.yml"]:
        template = (TEMPLATES_DIR / "workflows" / name).read_text()
        scaffolded = (repo / ".github" / "workflows" / name).read_text()
        assert scaffolded == template
