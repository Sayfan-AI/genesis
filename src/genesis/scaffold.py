"""Core scaffolding logic for genesis.

Creates and augments repositories with autonomous dev system scaffolding.
"""

import os
import subprocess
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

TEMPLATES_DIR = Path(__file__).parent.parent.parent / "templates"

# Placeholder for the adopter-local secrets file. Written once to
# ~/.config/genesis/.env (never into a dev repo) and shared across every
# project the adopter bootstraps. A dev repo's activate.sh sources it.
LOCAL_ENV_TEMPLATE = """\
# Genesis adopter-local secrets — ONE set, SHARED across ALL your genesis projects.
# A single GitHub App backs every project you bootstrap, so you fill these in once,
# here. A dev repo's `.genesis/scripts/activate.sh` then copies them into that
# repo's GitHub Actions secrets
# (ANTHROPIC_API_KEY / GENESIS_APP_ID / GENESIS_APP_PRIVATE_KEY) and enables it.
#
#   ANTHROPIC_API_KEY          your Anthropic API key (console.anthropic.com)
#   GENESIS_GITHUB_APP_ID      the numeric App ID of your genesis GitHub App
#   GENESIS_GITHUB_APP_SECRET  the App's private key — paste the FULL PEM,
#                              including the BEGIN/END lines, between the quotes.

ANTHROPIC_API_KEY=
GENESIS_GITHUB_APP_ID=
GENESIS_GITHUB_APP_SECRET="-----BEGIN RSA PRIVATE KEY-----
...paste the full PEM here, keeping the BEGIN/END lines...
-----END RSA PRIVATE KEY-----"
"""


def _genesis_config_dir() -> Path:
    """Adopter-local config dir (override with GENESIS_CONFIG_DIR; mainly for tests)."""
    override = os.environ.get("GENESIS_CONFIG_DIR")
    return Path(override) if override else Path.home() / ".config" / "genesis"


def ensure_local_env() -> Path:
    """Create the adopter-local `.env` with placeholders if it doesn't exist yet.

    The three secrets are identical for every project an adopter bootstraps, so
    this lives once in `~/.config/genesis/.env` - never inside a dev repo. If the
    file is already there (e.g. from a previous genesis project), it's left
    untouched so the human only populates it once. Returns the env file path.
    """
    config_dir = _genesis_config_dir()
    env_path = config_dir / ".env"
    if env_path.exists():
        return env_path
    config_dir.mkdir(parents=True, exist_ok=True)
    config_dir.chmod(0o700)
    _write_file(env_path, LOCAL_ENV_TEMPLATE)
    env_path.chmod(0o600)
    return env_path

SEED_AGENTS = [
    "orchestrator",
    "human_interaction",
    "evolver",
]

SEED_WORKFLOWS = [
    "genesis-orchestrator.yml",
    "genesis-events.yml",
    "genesis-evolver.yml",
    "genesis-merge.yml",
    "genesis-ci-failure.yml",
    "genesis-push-trigger.yml",
]

SEED_SCRIPTS = [
    "log.sh",
    "issues.sh",
    "activate.sh",
    "host-guard.sh",
    "claude-dir-guard.sh",
    "tag-milestone.sh",
    "pre-session.sh",
    "stranded-work.sh",
    "escalate.sh",
]

# The dev repo's `.gitignore`, carried in the template tree WITHOUT the leading
# dot: a file named `templates/.gitignore` would be a live ignore file for
# genesis's own templates directory. The scaffold renames it on the way out.
GITIGNORE_TEMPLATE = "gitignore"

# What "the genesis section is already here" is decided by. The pattern itself
# rather than the comment above it: a stale rule under a matching comment is
# exactly the case this must not treat as done.
GITIGNORE_PATTERN = ".genesis/.*"

# The dev repo's `.gitattributes`, carried without the leading dot for the same
# reason as the ignore file: `templates/.gitattributes` would apply to genesis's
# own tree. The rule keeps seeded shell scripts LF in every checkout (issue #158).
GITATTRIBUTES_TEMPLATE = "gitattributes"
GITATTRIBUTES_RULE = "*.sh text eol=lf"

# Turn-budget classes for the Claude-invoking workflow templates.
#
# "orchestrator" — open-ended work, and dispatched subagents spend from the same
# budget. These need headroom: a run that dies at `error_max_turns` produces no
# progress, no diagnosis, and the next run redoes the work from scratch.
# "narrow" — a fixed procedure. Kept deliberately small so a run that needs more
# turns fails fast instead of wandering.
#
# Every Claude-invoking template must be classified here and must declare an
# explicit `--max-turns`. Enforced by tests/e2e/test_workflows.py, which also
# fails if a template listed here stopped invoking Claude - a stale entry is how
# a budget rule outlives the thing it governed.
#
# The narrow bucket is currently empty, and that is the interesting part.
# `genesis-merge.yml` was its only member until the merge rule turned out not to
# need a model at all; rewriting it as shell steps is what *deterministic over
# agentic* looks like when you follow it to the end, and the reward is a step
# that can't die at `error_max_turns` in the first place. The class stays
# documented because the next fixed-procedure agent still needs a small budget
# and a reason for it.
WORKFLOW_TURN_CLASSES = {
    "genesis-orchestrator.yml": "orchestrator",
    "genesis-events.yml": "orchestrator",
    "genesis-evolver.yml": "orchestrator",
    # Triage looks narrow — one failing check, one procedure — and isn't. It runs
    # the same agent definition as the two above, and reading a failing run's log
    # and fixing the defect is open-ended in exactly the way a fixed procedure
    # isn't. It's also the run most likely to hit the floor: it starts with a
    # problem already in progress. `genesis-ci-failure.yml` exists because a
    # triage session died at max-turns; classifying it narrow would rebuild the
    # trap it was written to remove.
    "genesis-ci-failure.yml": "orchestrator",
}

# Minimum turns for an orchestrator-class workflow. Two separate dev-system
# workflows died at `error_max_turns` at 20 before this floor existed.
ORCHESTRATOR_TURN_FLOOR = 30


def toml_string(value: object, multiline: bool = False) -> str:
    """Render `value` as a TOML basic string, quotes included.

    `config.toml.j2` used to write `goal = "{{ goal }}"`, and goals are
    multi-line and quote things, so every scaffolded config was invalid TOML
    (issue #133). That stayed harmless until `configured_engine()` started
    parsing the file: a parse failure falls back to the default engine, so a
    Codex dev system with a multi-line goal ran on Claude without a word.

    `multiline` keeps a long goal readable in the file: newlines and tabs stay
    literal inside `\"\"\"...\"\"\"`, and everything TOML forbids raw is escaped.
    A newline right after the opening delimiter is trimmed by TOML, so the
    value starts on its own line and round-trips exactly.
    """
    out = []
    for ch in str(value):
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif multiline and ch in "\n\t":
            out.append(ch)
        elif ch in _TOML_SHORT_ESCAPES:
            out.append(_TOML_SHORT_ESCAPES[ch])
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\u{code:04X}")
        else:
            out.append(ch)
    body = "".join(out)
    return f'"""\n{body}"""' if multiline else f'"{body}"'


_TOML_SHORT_ESCAPES = {"\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}


def _render_template(name: str, **kwargs: object) -> str:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        keep_trailing_newline=True,
    )
    env.filters["toml_string"] = toml_string
    template = env.get_template(name)
    return template.render(**kwargs)


def _git(repo_path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo_path), *args],
        check=True,
        capture_output=True,
    )


def _write_file(path: Path, content: str) -> None:
    """Write a scaffolded file with LF line endings on every host.

    `Path.write_text` defaults to text mode, which turns every `\n` into the
    host's line separator. On Windows that's `\r\n`, and a seeded shell script
    written that way dies on its first line with `set: pipefail\r: invalid
    option name` (issue #158, where a dev system scaffolded from Windows
    committed all eight seeded scripts as CRLF). `newline="\n"` writes exactly
    what the content holds. Reading templates with `read_text`, whose universal
    newlines turn a `\r\n` back into `\n`, keeps this true even when genesis
    itself was checked out with CRLF, which is why the copies below go through
    text rather than copying bytes.

    Every file the scaffold writes goes through here, and
    `tests/e2e/test_line_endings.py` fails if one doesn't.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, newline="\n")


def _write_seed_agents(base: Path) -> None:
    agents_dir = base / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    for agent in SEED_AGENTS:
        src = TEMPLATES_DIR / "agents" / f"{agent}.md"
        dst = agents_dir / f"{agent}.md"
        _write_file(dst, src.read_text())


def _write_seed_workflows(base: Path) -> None:
    workflows_dir = base / ".github" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    for workflow in SEED_WORKFLOWS:
        src = TEMPLATES_DIR / "workflows" / workflow
        dst = workflows_dir / workflow
        _write_file(dst, src.read_text())


def _write_seed_scripts(base: Path) -> None:
    scripts_dir = base / ".genesis" / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    for script in SEED_SCRIPTS:
        src = TEMPLATES_DIR / "scripts" / script
        dst = scripts_dir / script
        _write_file(dst, src.read_text())
        dst.chmod(0o755)


def _write_gitignore(base: Path) -> None:
    """Write, or extend, the dev repo's `.gitignore` with the genesis section.

    An ignore rule is the only thing between `genesis serve`'s per-machine state
    and the repo's history: local mode writes its control-plane state as dotfiles
    under `.genesis/`, agents here read `git status` to decide what a run
    changed, and `git add -A` sits on several of their paths.

    An existing `.gitignore` is appended to, never replaced — genesis is a guest
    in an adopted repo. The append is skipped when the pattern is already
    present, so re-scaffolding a repo doesn't stack duplicate sections.
    """
    section = (TEMPLATES_DIR / GITIGNORE_TEMPLATE).read_text()
    path = base / ".gitignore"
    if not path.exists():
        _write_file(path, section)
        return
    existing = path.read_text()
    # A line that IS the rule, not prose that mentions it — the section's own
    # comment spells the pattern out, and so would a note explaining why someone
    # removed the rule.
    if any(line.strip() == GITIGNORE_PATTERN for line in existing.splitlines()):
        return
    separator = "\n" if existing.endswith("\n") else "\n\n"
    _write_file(path, existing + separator + section)


def _write_gitattributes(base: Path) -> None:
    """Write, or extend, the dev repo's `.gitattributes` so shell scripts stay LF.

    Writing LF isn't enough on its own: a Windows clone with `core.autocrlf=true`
    checks the scripts out as CRLF, and a commit from a host without it can put
    CRLF into the blobs, which no later checkout setting repairs. The attribute
    covers both. Same guest rules as the ignore file: an existing file is
    appended to, never replaced, and not touched again once it has the rule.
    """
    section = (TEMPLATES_DIR / GITATTRIBUTES_TEMPLATE).read_text()
    path = base / ".gitattributes"
    if not path.exists():
        _write_file(path, section)
        return
    existing = path.read_text()
    if any(line.split() == GITATTRIBUTES_RULE.split() for line in existing.splitlines()):
        return
    separator = "\n" if existing.endswith("\n") else "\n\n"
    _write_file(path, existing + separator + section)


ENGINES = ("claude", "codex")

# A new dev system's rules live in AGENTS.md, which both engines read: Codex
# natively, Claude Code through this import (supported since 2.1.277, measured on
# 2.1.325). One file means switching a system's engine is a config change, not an
# instruction migration (genesis issue #146).
CLAUDE_MD_IMPORT = """@AGENTS.md

<!-- This dev system's rules live in AGENTS.md, which Claude Code and Codex both
read. This file only imports it. Anything that applies to Claude Code alone goes
below this comment. -->
"""

# Under Codex nothing refuses an agent's writes to its own agent definitions and
# hook wiring the way the Claude Code harness refuses writes under `.claude/`, so
# a Codex dev system holds pull requests that touch them for a person instead.
# AGENTS.md is deliberately not here: it's where the evolver puts prose rules,
# the job CLAUDE.md does for a Claude system, and holding it would stall that.
CODEX_PROTECTED_PATHS = """# Paths an autonomous pull request can't land on its own.
#
# The merge sweep (genesis serve) and genesis-merge.yml read this file from the
# default branch, never from the pull request. A bot pull request that touches
# any of these stays open with `needs:human` and a comment naming the file.
#
# One prefix per line. A prefix covers itself and everything beneath it.

# Agent definitions and Claude Code settings. Codex can write here; Claude Code
# can't, which is why a Claude dev system doesn't need this line.
.claude/

# The hooks Codex sessions run under, including host-guard.sh's wiring.
.codex/

# This list.
.genesis/protected-paths
"""


def _check_engine(engine: str) -> str:
    if engine not in ENGINES:
        raise ValueError(f"unknown engine {engine!r}; choose one of: {', '.join(ENGINES)}")
    return engine


def _write_instructions(base: Path, rules: str, *, existing: bool) -> None:
    """Write the rules to AGENTS.md and make CLAUDE.md import it.

    In an existing repo both files may already exist. AGENTS.md gets the genesis
    section appended, the way CLAUDE.md used to, and an existing CLAUDE.md keeps
    its content and gains the import line. Codex reads only AGENTS.md, so rules a
    repo keeps in its own CLAUDE.md don't reach Codex sessions until someone
    moves them across.
    """
    agents_md = base / "AGENTS.md"
    if existing and agents_md.exists():
        _write_file(agents_md, agents_md.read_text() + "\n\n---\n\n# Genesis Dev System\n\n" + rules)
    else:
        _write_file(agents_md, rules)

    claude_md = base / "CLAUDE.md"
    if existing and claude_md.exists():
        current = claude_md.read_text()
        if "@AGENTS.md" not in current:
            _write_file(claude_md, current.rstrip("\n") + "\n\n" + CLAUDE_MD_IMPORT)
    else:
        _write_file(claude_md, CLAUDE_MD_IMPORT)


def _write_codex_hooks(base: Path) -> None:
    """Seed `.codex/hooks.json` in every new dev system, whichever engine it
    starts on, so a switch to Codex keeps the logging and host-guard layer."""
    hooks_path = base / ".codex" / "hooks.json"
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    _write_file(hooks_path, (TEMPLATES_DIR / "codex_hooks.json").read_text())


def _write_engine_extras(base: Path, engine: str) -> None:
    if engine != "codex":
        return
    path = base / ".genesis" / "protected-paths"
    if not path.exists():
        _write_file(path, CODEX_PROTECTED_PATHS)
        return
    # An existing repo may already keep a list. Add what's missing rather than
    # replacing it, so nothing it protects today stops being protected.
    current = path.read_text()
    have = {line.strip() for line in current.splitlines() if line.strip() and not line.startswith("#")}
    wanted = [line for line in CODEX_PROTECTED_PATHS.splitlines() if line and not line.startswith("#")]
    missing = [line for line in wanted if line not in have]
    if missing:
        _write_file(path, current.rstrip("\n") + "\n\n# Added by genesis for the Codex engine.\n" + "\n".join(missing) + "\n")


def _write_settings(base: Path) -> None:
    settings_path = base / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    src = TEMPLATES_DIR / "settings.json"
    _write_file(settings_path, src.read_text())


def _write_genesis_config(
    base: Path,
    project_name: str,
    goal: str,
    target_repos: list[str] | None = None,
    engine: str = "claude",
) -> None:
    content = _render_template(
        "config.toml.j2",
        project_name=project_name,
        goal=goal,
        target_repos=target_repos or [],
        engine=engine,
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
    engine: str = "claude",
) -> None:
    """Create a new repo with full dev system scaffolding (embedded)."""
    _check_engine(engine)
    ensure_local_env()
    path.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        ["git", "init", str(path)],
        check=True,
        capture_output=True,
    )

    # AGENTS.md, imported by CLAUDE.md
    rules = _render_template(
        "agents_md.md.j2",
        project_name=project_name,
        goal=goal,
        target_repos=[],
    )
    _write_instructions(path, rules, existing=False)

    # README.md
    readme = _render_template(
        "readme.md.j2",
        project_name=project_name,
        goal=goal,
        engine=engine,
    )
    _write_file(path / "README.md", readme)

    # Seed agents, workflows, settings, config
    _write_seed_agents(path)
    _write_seed_workflows(path)
    _write_settings(path)
    _write_codex_hooks(path)
    _write_genesis_config(path, project_name, goal, engine=engine)
    _write_engine_extras(path, engine)
    _write_seed_scripts(path)
    _write_gitignore(path)
    _write_gitattributes(path)

    # Onboarding issue
    _write_onboarding_issue(path, project_name, goal)

    # Initial commit
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "Initial scaffold by genesis")


def scaffold_existing_repo(
    path: Path,
    goal: str,
    project_name: str,
    engine: str = "claude",
) -> None:
    """Add dev system scaffolding to an existing repo (embedded)."""
    _check_engine(engine)
    if not (path / ".git").is_dir():
        raise ValueError(f"{path} is not a git repository")

    ensure_local_env()

    rules = _render_template(
        "agents_md.md.j2",
        project_name=project_name,
        goal=goal,
        target_repos=[],
    )
    _write_instructions(path, rules, existing=True)

    # Seed agents, workflows, settings, config
    _write_seed_agents(path)
    _write_seed_workflows(path)
    _write_settings(path)
    _write_codex_hooks(path)
    _write_genesis_config(path, project_name, goal, engine=engine)
    _write_engine_extras(path, engine)
    _write_seed_scripts(path)
    _write_gitignore(path)
    _write_gitattributes(path)

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
    engine: str = "claude",
) -> None:
    """Create a separate dev repo that manages work across target repos."""
    _check_engine(engine)
    ensure_local_env()
    dev_path.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        ["git", "init", str(dev_path)],
        check=True,
        capture_output=True,
    )

    # AGENTS.md with target repo references, imported by CLAUDE.md
    rules = _render_template(
        "agents_md.md.j2",
        project_name=project_name,
        goal=goal,
        target_repos=target_repos,
    )
    _write_instructions(dev_path, rules, existing=False)

    # README.md
    readme = _render_template(
        "readme.md.j2",
        project_name=project_name,
        goal=goal,
        engine=engine,
    )
    _write_file(dev_path / "README.md", readme)

    # Seed agents, workflows, settings, config
    _write_seed_agents(dev_path)
    _write_seed_workflows(dev_path)
    _write_settings(dev_path)
    _write_codex_hooks(dev_path)
    _write_genesis_config(dev_path, project_name, goal, target_repos, engine=engine)
    _write_engine_extras(dev_path, engine)
    _write_seed_scripts(dev_path)
    _write_gitignore(dev_path)
    _write_gitattributes(dev_path)

    # Onboarding issue with target repo references
    _write_onboarding_issue(dev_path, project_name, goal, target_repos)

    # Initial commit
    _git(dev_path, "add", "-A")
    _git(dev_path, "commit", "-m", "Initial scaffold by genesis")
