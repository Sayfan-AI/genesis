# Genesis - Brainstorming

## Core Idea

Genesis is a minimal meta-factory that bootstraps autonomous agentic AI systems. Given a goal from a user, genesis creates a new GitHub repo — a "dev repo" — that is a custom AI system designed to accomplish that goal autonomously.

The dev repo is NOT the repo of the final deliverable. It IS the AI system that will work toward the goal. The dev system is self-improving: it designs its own agents, skills, hooks, and evolves its own approach over time. Genesis does not dictate the shape of the solution — that's the job of the dev system.

Genesis itself stays minimal — a pure bootstrapper. Once it creates the dev repo and seeds the initial structure, its job is done. Cross-project dashboards, aggregated reporting, etc. are themselves goals you can feed to genesis, not features of genesis itself.

## Example Goals

1. Scan all my repos, identify all security issues and resolve them
2. Scan all my repos, bump all versions
3. Migrate all my Python repos to uv
4. Automatically implement the AI-6 roadmap
5. Finish blog2video
6. Finish butterfly

Some goals involve creating new agentic AI systems in new repos. Some involve evolving existing systems (agentic or not).

## What Genesis Produces

When bootstrapping a dev repo, genesis creates:

1. A new GitHub repo with base structure (CLAUDE.md, agents/, skills/, hooks/, .github/workflows/)
2. The orchestrator scaffolding (event-driven agent loop)
3. Issue #1: the onboarding issue containing the user's goal — this kicks off an interactive process where the dev system works with the user to refine the goal, break it into milestones, and start executing

Genesis does NOT pre-decompose the goal into issues or milestones. That's the dev system's job during onboarding.

## Meta-Concepts (Seeded by Genesis)

These are principles genesis imbues into every dev repo as a starting pattern. The dev system can evolve them as needed:

- **GitHub as coordination layer** — issues track progress, PRs deliver changes, CI/CD enforces quality. The AI system and humans speak the same protocol.
- **Quality gates and e2e testing** — code, tests, CI/CD, deployment are all first-class concerns.
- **Self-improvement** — the dev system continuously evolves its own agents, skills, and strategies.
- **Self-monitoring** — the system monitors its own progress, detects when it's stuck or looping, tries to unblock itself, and escalates to the human when it can't.
- **Minimal human-in-the-loop** — the system does everything it can autonomously. What it can't do (missing access, ambiguous requirements, approval gates), it highlights to the human and offers to do it if given access.
- **Deterministic over agentic when possible** — if a role is well understood and doesn't require an LLM to interpret fuzzy state, prefer building a deterministic tool (script, CLI, CI step) to get the job done. Reserve LLMs for tasks that genuinely need judgment. Deterministic tools are faster, cheaper, and more reliable.
- **Incremental planning** — only drill down on the current milestone. Detailed plans for future milestones are waste if earlier milestones change direction.

## Standard Agent Roster (Seed Pattern)

Genesis seeds these as a starting pattern. The dev system can rename, merge, split, or add agents as the goal demands:

- **Onboarding agent** — runs once at project start. Interacts with human to refine the goal into high-level milestones. Produces an executable roadmap.
- **Project manager agent** — owns the roadmap. Tracks progress, detects stuck work, drills down milestones into tasks as they become current.
- **Human interaction agent** — all communications with the user: daily reports, escalations, access requests, milestone sign-offs.
- **Worker agents** — do the actual work (code, tests, infra, etc.). These are the ones the dev system designs for itself based on the goal.
- **Introspective agent** — responsible for evolving the dev system itself. Observes how the system operates, designs specialized worker agents for recurring task patterns, creates tools and skills, designs and refines the memory system (CLAUDE.md files at different levels, settings.json, hooks). Refactors the agent roster as the project evolves. Learns from failures and adapts the system's approach.
- **Self-review / health agent** — monitors system health, catches loops, audits quality.

## Execution Model

An event-driven orchestrator (or multiple coordinating orchestrator agents) that runs both:
- **Periodically** (cron) — to advance the project state
- **Event-triggered** — new issue opened/closed, PR merged, human feedback, etc.

The orchestrator launches worker agents to perform tasks and advance the state of the project.

## Milestones and Completion

- The dev system defines milestones with done criteria.
- When a milestone is reached, it is reported to the human.
- The human can always give feedback: reopen closed issues, provide free-text feedback, clarify that something is not done.
- The system then goes back and corrects itself.
- Some goals are bounded ("migrate to uv") and some are open-ended ("implement the roadmap"). The dev system handles both.

## Human Interaction Modes

- Daily/periodic reports on project status (email or Slack — human's choice)
- Human opening GitHub issues (the system normally opens issues to itself, but humans can too)
- Human starting ad-hoc sessions
- Human interacting directly with the dev repo through issues, PR reviews, comments

## Access and Permissions

- The system notifies the user of all its needs.
- Issues are blocked until access is provided by the human.
- Agents should have their own GitHub machine users for clear audit trails and per-agent access control.
- If an issue is blocked on a human action, an issue can be opened and assigned to the human.
- The system highlights what it could do autonomously if given access, letting the human decide.

## Open Questions

- Exact execution model: GitHub Actions + Claude API? Claude Code sessions via cron/webhooks? Hybrid?
- Machine user provisioning: how does genesis set up GitHub machine users for agents?
- Cost management: autonomous agents can burn through API credits. Should there be budget awareness?
- Security boundaries: how to safely scope access for multi-repo goals?
- State management: where does the orchestrator keep its state between runs? GitHub issues alone, or a lightweight state file in the repo?
