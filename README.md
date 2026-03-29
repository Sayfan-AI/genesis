# Genesis

A minimal meta-factory that bootstraps autonomous, self-improving AI dev systems.

You give genesis a goal. Genesis creates an AI system — built on [Claude Code](https://docs.anthropic.com/en/docs/claude-code) and GitHub — that works toward that goal autonomously. The system designs its own agents, tools, and workflows. It monitors its own progress. It improves how it improves.

Genesis is a bootstrapper, not a supervisor. Once it creates and seeds the dev system, its job is done.

## How It Works

```
You: "Migrate all my Python repos to uv"

Genesis:
  1. Talks to you to understand the goal
  2. Determines the right topology (new repo, embed in existing, separate dev repo)
  3. Creates the dev system scaffold (agents, workflows, config)
  4. Opens issue #1 — the onboarding issue

Dev System (takes over from here):
  5. Refines the goal with you, breaks it into milestones
  6. Starts executing — opens PRs, runs tests, reports progress
  7. Continuously evolves its own agents and tools
  8. Communicates with you via A2H protocol (Slack, email, etc.)
```

## Example Goals

| Goal | Topology |
|------|----------|
| Scan all my repos, fix security issues | Separate dev repo (multi-repo) |
| Migrate all Python repos to uv | Separate dev repo (multi-repo) |
| Finish blog2video | Embedded in target repo |
| Implement the AI-6 roadmap | Embedded in target repo |
| Build a CLI that converts markdown to PDF | New repo with embedded dev system |

## What Genesis Seeds

Every dev system gets a scaffold that it can evolve:

- **Seed agents** — onboarding, project manager, human interaction, evolver, health
- **Orchestrator workflows** — GitHub Actions (cron + event-triggered) launching Claude Agent SDK sessions
- **Observability** — Claude Code hooks that automatically log all agent activity to Grafana Loki
- **Scripts** — shell scripts for issue management and structured logging (zero binary distribution overhead)
- **Meta-concepts** — principles the dev system operates by (see below)

## Meta-Concepts

These are seeded into every dev system as starting principles. The dev system evolves them as needed.

- **GitHub as coordination layer** — issues, PRs, CI/CD. Humans and agents speak the same protocol.
- **Self-improvement** — the dev system continuously evolves its own agents, skills, and strategies.
- **Deterministic over agentic** — if a task doesn't need LLM judgment, build a script. Reserve LLMs for fuzzy reasoning.
- **Quality gates and e2e testing** — code, tests, CI/CD, deployment are all first-class concerns.
- **Incremental planning** — only detail the current milestone. Don't over-plan the future.
- **Minimal human-in-the-loop** — do everything possible autonomously. Escalate what you can't.
- **Self-monitoring** — detect stuck/looping states, try to self-heal, escalate when stuck.

## The Evolver Agent

The most important agent in the roster. It doesn't do the work — it watches *how the system works* and makes it better:

- Reviews failures, human interventions, and stuck states to identify improvements
- Designs specialized worker agents for recurring task patterns
- Builds deterministic tools to replace agentic work where possible
- Refines the memory system (CLAUDE.md, settings, hooks)
- Refactors the agent roster as the project evolves

**Two-tier evolution:** Project evolvers fix project-specific issues directly. When the root cause is in genesis scaffolding, they open issues on the genesis repo with a `needs:evolver` label. Genesis's own evolver watches for these issues, evaluates them, and either implements the improvement or rejects it with a rationale.

The evolver agent can rewrite its own definition. The modification procedure itself is modifiable — a property formalized in the [Hyperagents](https://arxiv.org/abs/2603.19461) paper (Zhang et al., 2026) as metacognitive self-improvement.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Genesis (bootstrapper)                             │
│                                                     │
│  You ──chat──> Genesis ──scaffold──> Dev System     │
│                                                     │
│  Genesis is done. Dev system takes over.            │
└─────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────┐
│  Dev System (autonomous, self-improving)            │
│                                                     │
│  GitHub Actions (cron + events)                     │
│       │                                             │
│       ▼                                             │
│  Claude Agent SDK Orchestrator                      │
│       │                                             │
│       ├──> Onboarding Agent (goal → milestones)     │
│       ├──> Project Manager (roadmap, tasks)         │
│       ├──> Human Interaction (A2H protocol)         │
│       ├──> Evolver (evolve the system)               │
│       ├──> Health (stuck detection, quality)        │
│       └──> Worker Agents (designed by the system)   │
│                                                     │
│  CC Hooks ──scripts──> Grafana Loki (observability)  │
└─────────────────────────────────────────────────────┘
```

## Human Interaction

**With genesis:** Plain Claude Code chat. Start a session in the genesis repo, describe your goal.

**With dev systems:** The dev system communicates via the [A2H protocol](https://github.com/twilio-labs/Agent2Human) — channel-agnostic (Slack, email, SMS), with cryptographic audit trails. You can also interact directly through GitHub issues, PR reviews, and ad-hoc Claude Code sessions.

The human's role is minimized by default. The system does everything it can autonomously, highlights what it can't (missing access, ambiguous requirements), and offers to do it if given access.

## Genesis Scripts

Shell scripts (`.genesis/scripts/`) provide core capabilities to every dev system. No binary distribution needed — just bash, curl, and `gh` CLI.

```bash
bash .genesis/scripts/log.sh post-tool-use              # Activity logging (called by CC hooks)
bash .genesis/scripts/issues.sh create --title "Implement auth"  # Issue management
bash .genesis/scripts/issues.sh list --status open       # List issues
```

## Project Structure

```
genesis/
├── src/genesis/          # Core scaffolding logic (Python)
│   ├── scaffold.py       # Create/augment repos with dev system scaffolding
│   └── github.py         # GitHub integration (repo creation, issue #1)
├── templates/            # Templates for scaffolded dev systems
│   ├── agents/           # Seed agent definitions
│   ├── scripts/          # log.sh, issues.sh
│   ├── workflows/        # GitHub Actions orchestrator workflows
│   ├── claude_md.md.j2   # CLAUDE.md template
│   └── settings.json     # CC hooks configuration
├── tests/e2e/            # End-to-end tests for all topologies
├── BRAINSTORMING.md      # Living design document
└── CLAUDE.md             # Project instructions
```

## Status

Early development. The scaffolding engine and genctl CLI are functional with passing tests. See [BRAINSTORMING.md](BRAINSTORMING.md) for the full design and open questions.

## Related Work

- **[Hyperagents](https://arxiv.org/abs/2603.19461)** (Zhang et al., 2026) — formalizes self-referential agents with modifiable modification procedures. Genesis's evolver agent is a practical implementation of this concept. The paper was publicly announced exactly one day after the genesis repo was created — independent convergence on the same idea.
- **[A2H Protocol](https://github.com/twilio-labs/Agent2Human)** (Twilio) — open-source agent-to-human communication protocol used by genesis dev systems.

## License

MIT
