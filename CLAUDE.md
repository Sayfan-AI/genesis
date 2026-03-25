# Genesis

Genesis is a minimal meta-factory that bootstraps autonomous agentic AI dev systems. Given a goal, it creates a new GitHub "dev repo" — a self-improving AI system that works toward the goal autonomously. Genesis is fire-and-forget: once the dev repo is seeded, genesis's job is done.

See BRAINSTORMING.md for full design notes and open questions.

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
- **Introspective** — evolves the dev system itself (new agents, tools, skills, memory design)
- **Health / self-review** — monitors for stuck/looping, audits quality
- **Workers** — designed by the dev system for the specific goal

## Development Guidelines

- This repo is the genesis bootstrapper itself — keep it lean
- All brainstorming and design evolution goes in BRAINSTORMING.md
- When building templates for dev repos, put them under `templates/`
- Test genesis by actually bootstrapping a dev repo and verifying the onboarding flow works end-to-end
- The dev repo templates should be opinionated about process (GitHub issues, quality gates, self-monitoring) but unopinionated about implementation

## Tech Stack

- **Trigger layer:** GitHub Actions — scheduled workflows (cron) + event-triggered workflows (issues, PRs, comments)
- **Orchestrator:** Claude Agent SDK sessions launched by GitHub Actions. Each trigger spawns an orchestrator that assesses project state and launches sub-agents.
- **Genesis itself:** TBD — CLI tool, Claude Code skill, or both

## Self-Improvement

This project opts in to self-improvement. Update this CLAUDE.md and project workflows as the design evolves. Keep BRAINSTORMING.md as the living design document.
