# Genesis

Genesis is a minimal meta-factory that bootstraps autonomous agentic AI dev systems. Given a goal, it creates a new GitHub "dev repo" — a self-improving AI system that works toward the goal autonomously. Genesis is fire-and-forget: once the dev repo is seeded, genesis's job is done.

See `docs/` for full design notes:
- [docs/design.md](docs/design.md) — architecture, agents, execution model, permissions, memory
- [docs/evaluations.md](docs/evaluations.md) — technology evaluations and decisions

## Project Principles

- **Genesis stays minimal.** It is a bootstrapper, not a supervisor. No cross-project management, no aggregated dashboards — those are goals you feed to genesis.
- **Dev repos are autonomous.** Genesis seeds patterns and meta-concepts, but the dev system decides its own shape — agents, tools, architecture, everything.
- **Deterministic over agentic.** When a task is well-understood and doesn't need LLM judgment, build a deterministic tool. Reserve LLMs for fuzzy reasoning.
- **GitHub is the coordination protocol.** Issues, PRs, CI/CD, comments — humans and agents speak the same language.
- **Incremental planning.** Only detail the current milestone. Future milestones stay high-level until they're next.

## Architecture

Genesis is a CLI/agent that:

1. Takes a goal from the user
2. Creates a new GitHub repo with seed structure:
   - `CLAUDE.md` — project-level instructions and meta-concepts
   - `.claude/` — agents, skills, hooks, settings
   - `.github/workflows/` — orchestrator CI (event-driven + cron)
3. Opens issue #1 (onboarding) with the user's goal
4. The dev system's onboarding agent takes over from there

### Seed Agent Roster

These are seeded as starting patterns. The dev system evolves them:

- **Onboarding** — refines goal with human, produces milestones
- **Project manager** — owns roadmap, tracks progress, drills down current milestone into tasks
- **Human interaction** — comms with user (reports, escalations, access requests)
- **Evolver** — evolves the dev system itself (new agents, tools, skills, memory design). Escalates framework-level improvements to genesis.
- **Health / self-review** — monitors for stuck/looping, audits quality
- **Workers** — designed by the dev system for the specific goal

## Development Guidelines

- This repo is the genesis bootstrapper itself — keep it lean
- Design docs live in `docs/` — `design.md` for architecture, `evaluations.md` for tech decisions
- When building templates for dev repos, put them under `templates/`
- Test genesis by actually bootstrapping a dev repo and verifying the onboarding flow works end-to-end
- The dev repo templates should be opinionated about process (GitHub issues, quality gates, self-monitoring) but unopinionated about implementation

## Tech Stack

- **Orchestrator:** Trigger-agnostic. Reads GitHub issues, assesses state, dispatches sub-agents, exits. Doesn't know how it was triggered.
- **Trigger layer (default):** GitHub Actions — scheduled workflows (cron) + event-triggered workflows (issues, PRs, comments). Zero setup, always on, self-contained.
- **Trigger layer (opt-in):** Local control plane — polls GitHub events, launches orchestrator sessions locally in a sandbox. For long sessions, interactive steering, or local resource access. Requires running a local process.
- **Both modes** can run together, coordinated via GitHub issues with a cross-mode concurrency guard.
- **Genesis itself:** TBD — CLI tool, Claude Code skill, or both

## Claude Code Hooks Format

The correct hooks format in `.claude/settings.json` requires a `matcher` + `hooks` array structure. Each hook event entry must look like:

```json
{"matcher": "", "hooks": [{"type": "command", "command": "..."}]}
```

NOT the flat format `{"type": "command", "command": "..."}` — that is invalid and causes Claude Code settings errors. The template at `templates/settings.json` must always use the correct format.

## Self-Improvement

This project opts in to self-improvement. Update this CLAUDE.md and project workflows as the design evolves. Keep `docs/` as the living design documents.
