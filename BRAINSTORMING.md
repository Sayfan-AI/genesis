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

Genesis does NOT pre-decompose the goal into issues or milestones. That's the dev system's job after genesis hands off.

## Onboarding (Split Between Genesis and Dev System)

### Phase 1: Genesis Onboarding
Genesis handles the initial interaction with the human:
1. Understand the goal
2. Determine topology (new repo, embed in existing, separate dev repo)
3. Audit the target if it's an existing repo (what's already there?)
4. Create or augment the artifact (see Artifact Shape below)
5. Open issue #1 for the dev system to take over

### Phase 2: Dev System Onboarding
The dev system's onboarding agent takes over via issue #1:
1. Deeper goal refinement with the human
2. Break down into high-level milestones
3. Drill down on milestone 1
4. Begin execution

## Artifact Shape

What genesis produces depends on the target:

### New Repo (Greenfield)
Full scaffold — clean slate, no conflicts:
- `CLAUDE.md` — project instructions + genesis meta-concepts
- `.claude/agents/` — seed agent roster
- `.claude/skills/` — seed skills
- `.claude/hooks/` — seed hooks
- `.claude/settings.json` — configuration
- `.github/workflows/` — orchestrator triggers (cron + event-driven)
- `README.md`

### Existing Repo Without Claude Code
Minimal footprint — add dev scaffolding without reorganizing what's there:
- Add `.claude/` directory with genesis agents, skills, hooks, settings
- Add `.github/workflows/` for orchestrator triggers
- Create `CLAUDE.md` (or augment if project has one for other purposes)

### Existing Repo WITH Claude Code Scaffolding
Genesis audits what's already there (CLAUDE.md, agents, skills, hooks, workflows) and presents options to the human:
- Namespace genesis artifacts alongside existing ones
- Merge/augment existing scaffolding
- Replace specific components
- Human decides per-component

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
- **Evolver agent** — responsible for evolving the dev system itself. Observes how the system operates, designs specialized worker agents for recurring task patterns, creates tools and skills, designs and refines the memory system (CLAUDE.md files at different levels, settings.json, hooks). Refactors the agent roster as the project evolves. Learns from failures and adapts the system's approach.
- **Self-review / health agent** — monitors system health, catches loops, audits quality.

## Execution Model

GitHub Actions serve as the trigger layer:
- **Scheduled workflows** (cron) — periodic advancement of project state
- **Event-triggered workflows** — new issue opened/closed, PR merged, human feedback, comments, etc.

Each workflow trigger launches a **Claude Agent SDK session** as the orchestrator. The orchestrator assesses the current state of the project and launches sub-agents to perform tasks and advance progress. The dev system's evolver agent evolves these workflows and triggers as the project matures.

## Milestones and Completion

- The dev system defines milestones with done criteria.
- When a milestone is reached, it is reported to the human.
- The human can always give feedback: reopen closed issues, provide free-text feedback, clarify that something is not done.
- The system then goes back and corrects itself.
- Some goals are bounded ("migrate to uv") and some are open-ended ("implement the roadmap"). The dev system handles both.

## Human Interaction Modes

### With Genesis
- Plain Claude Code chat — genesis is interactive and local, no async protocol needed
- Fast path: `/genesis new` skill that walks through structured onboarding (goal → topology → audit → preview → create)
- Free-form chat for brainstorming and less structured work

### With Dev Systems
- Daily/periodic reports on project status (email or Slack — human's choice)
- Human opening GitHub issues (the system normally opens issues to itself, but humans can too)
- Human starting ad-hoc sessions (CC in the dev repo)
- Human interacting directly through issues, PR reviews, comments

### A2H Protocol (Agent-to-Human)
Dev systems use the [A2H protocol](https://github.com/twilio-labs/Agent2Human) as the standard communication layer with humans. A2H is an open-source, channel-agnostic protocol with five intent types:

- **INFORM** — one-way notifications (status updates, daily reports, milestone completions)
- **COLLECT** — request structured input (onboarding questions, goal refinement, clarifications)
- **AUTHORIZE** — approval requests with cryptographic proof (access requests, merge approvals, deployment gates)
- **ESCALATE** — handoff when the system is stuck and needs human help
- **RESULT** — task/milestone/goal completion reports

The human interaction agent speaks A2H. An A2H gateway handles channel routing (Slack, email, SMS, etc.) — the dev system doesn't need to know the human's preferred channel. All interactions produce signed audit artifacts.

GitHub issues remain the coordination layer for tracking work, but A2H handles the real-time human communication. Access requests that were previously ad-hoc (`blocked:human-action` labels) now go through A2H AUTHORIZE intents with proper audit trails.

## Access and Permissions

- The system notifies the user of all its needs.
- Issues are blocked until access is provided by the human.
- Agents should have their own GitHub machine users for clear audit trails and per-agent access control.
- If an issue is blocked on a human action, an issue can be opened and assigned to the human.
- The system highlights what it could do autonomously if given access, letting the human decide.
- See **Permission Architecture** below for the layered token management model.

## Dev System Topology

The dev system doesn't always need its own repo. Genesis supports three modes, decided during onboarding:

- **Separate dev repo** — best for multi-repo goals (scan all repos, bump versions), temporary/bounded goals (migrate to uv), or when the target repo has strict conventions you don't want to pollute. Dev repo can be torn down when done.
- **Embedded in target repo** — best for single-repo goals (finish blog2video), ongoing work, or when simplicity matters. Agents, skills, and workflows live alongside the code they're evolving. No cross-repo permissions needed.
- **New repo with embedded dev system** — for goals that create something new. The dev system and the deliverable start as one repo. Dev scaffolding can stay (if the system should be self-evolving) or be stripped out when the goal is complete.

**Defaults:**
- Single-repo goals (new or existing): **embedded**. The dev system lives in the target repo.
- Multi-repo goals: **separate dev repo**.

**On removal:** When the goal is accomplished, the dev sub-system *could* be removed but generally shouldn't be — you'll likely want to evolve the system further. Keep it around.

**Exception — building for others:** When the deliverable is a system for someone else, use a separate dev repo you manage. The final system stays clean of genesis-based dev scaffolding. The dev repo drives work on the target repo externally.

The onboarding agent confirms the topology with the human during goal refinement.

## Tech Stack Preferences (Recommendations to Dev Systems)

These are genesis's default recommendations. The human can override any of these. For existing projects, use their existing stack.

### General
- Open source + free tier only (unless human overrides)

### Languages
- **Backend:** Rust preferred. Go if Kubernetes-heavy.
- **CLI:** Rust
- **Frontend:** TypeScript
- **Desktop:** Tauri (Rust backend + TypeScript/web frontend, cross-platform)
- **Mobile:** React Native (with Expo)

### Backend
- gRPC for all internal service communication

### Frontend
- **SPA by default:** Vite + React + TanStack Router + TanStack Query
- **Styling:** Tailwind CSS
- **Language:** TypeScript (strict)
- Dev system can switch to SSR/framework if project requirements demand it

### Local Development
- Tilt + kind for Kubernetes deployments
- LocalStack as AWS simulator

### Deployment / DevOps
- Cloud free-tier
- Cloudflare (CDN, workers, DNS)
- Database: Neon (serverless Postgres)

### Auth
- **K8s / microservices:** Ory stack (Kratos + Hydra) — open source, Go-based, runs as sidecar containers. Backend calls Ory over HTTP/REST to validate sessions, manage users. No auth logic in app code.
- **Simple apps / single-binary Rust:** Roll auth with standard crates (`argon2`, `jsonwebtoken`, `oauth2`, `totp-rs`)
- **When self-hosting is overkill:** Clerk (managed, generous free tier, good DX)
- Dev system decides based on deployment topology

### Observability
- **Instrumentation:** OpenTelemetry (vendor-neutral)
- **Stack:** Grafana Cloud free tier (metrics, logs, traces) or self-hosted Grafana + Loki + Tempo + Prometheus for more control

## Agent Activity Logging

All agent activity is logged to **[Grafana Cloud Loki](https://grafana.com/pricing/)** via **Claude Code hooks**. Logging is automatic, comprehensive, and requires zero effort from individual agents.

### Implementation: CC Hooks → Loki

Genesis seeds `.claude/settings.json` with HTTP hooks that POST structured log entries directly to Grafana Cloud Loki. No intermediate scripts, no agent-side logging code.

**Key hook events used:**

| Hook Event | What it captures |
|---|---|
| `SessionStart` | Agent session begins (model, source, session_id) |
| `SessionEnd` | Agent session ends (duration) |
| `PreToolUse` | Every tool call attempt (tool name, inputs) |
| `PostToolUse` | Successful tool results (outputs, exit codes) |
| `PostToolUseFailure` | Failed tool calls (errors) |
| `SubagentStart` | Sub-agent spawned (agent type, id) |
| `SubagentStop` | Sub-agent finished (result) |
| `UserPromptSubmit` | Human interaction (prompt text) |

Each hook is `type: "command"` — calls `.genesis/scripts/log.sh` which reads the hook's stdin JSON, enriches it, and pushes to Loki via curl. Fully deterministic, no LLM in the logging loop. No binary distribution needed — just bash + curl.

### Why this approach
- **Automatic** — every agent gets logging via hooks, no opt-in required
- **Comprehensive** — every tool call, sub-agent spawn, session, human interaction
- **Zero-cost to agents** — agents don't know about logging, hooks handle it transparently
- **Deterministic** — HTTP POST, no LLM involved in logging (follows the "deterministic over agentic" principle)
- **Concurrent-safe** — Loki handles concurrent writes natively, unlike git branches
- **Queryable** — LogQL for health agent, evolver agent, and dashboards

### Loki labels and fields

**Labels:** `project`, `agent_type`, `session_id`, `hook_event`

**Structured fields:**
- Tool name, inputs, outputs
- Trigger (cron, issue event, PR event, spawned by another agent)
- GitHub/Linear issue being worked on
- Sub-agents spawned
- Files modified / PRs created / issues opened
- Errors and failure reasons

### Grafana Cloud Free Tier Limits (reference)
- Logs: 50 GB/month, 14 days retention
- Metrics: 10,000 active series/month, 14 days retention
- Traces: 50 GB/month, 14 days retention
- 3 active visualization users/month

Full pricing: https://grafana.com/pricing/

### Aggregation
The raw logs in Loki serve as the source of truth. If aggregated views are needed (weekly summaries, cross-project reports), a separate job can query Loki and produce them. This is not built into genesis — it's a goal you can feed to genesis if you want it.

## Genesis Scripts (`.genesis/scripts/`)

Shell scripts that provide core capabilities to every dev system. No binary distribution needed — just bash, curl, and `gh` CLI (already available in GitHub Actions).

### Scripts

- **`log.sh`** — called by CC hooks. Reads hook stdin JSON, pushes structured logs to Loki via curl. Falls back to stderr if Loki is not configured.
- **`issues.sh`** — thin wrapper around `gh` CLI. Provides `create`, `list`, `close`, `assign` subcommands.

### Usage

```bash
# Logging (called automatically by CC hooks, or manually)
bash .genesis/scripts/log.sh post-tool-use

# Issues
bash .genesis/scripts/issues.sh create --title "Implement auth" --labels "milestone:1"
bash .genesis/scripts/issues.sh list --status open --milestone 1
bash .genesis/scripts/issues.sh close --id 5 --reason completed
bash .genesis/scripts/issues.sh assign --id 5 --to worker-1
```

### Configuration

Scripts read from `.genesis/config.toml` and environment variables:
- `GENCTL_LOKI_URL`, `GENCTL_LOKI_USER`, `GENCTL_LOKI_TOKEN` — Loki credentials
- `gh` CLI uses standard GitHub auth (GITHUB_TOKEN or `gh auth`)

### Design Decision: Scripts over genctl CLI

We initially designed a Rust CLI (`genctl`) but chose shell scripts instead:
- **Zero distribution overhead** — no binary to build, cache, or install
- **Works everywhere** — bash + curl + gh are available on all GitHub Actions runners
- **Simpler to evolve** — the evolver agent can modify scripts directly
- **Good enough** — these are thin wrappers, not complex logic
- **genctl can come later** — if scripts become unwieldy, the evolver agent can build a proper CLI

## Memory System

The dev system uses Claude Code's native memory mechanisms — CLAUDE.md files and `.claude/memory/` — as its persistent memory. Memory is how the system gets smarter across sessions. GitHub Actions runners are ephemeral; without committed memory, the system starts from scratch every time.

### Memory is shared, not per-agent

All agents read and write to the same memory system. No silos. Agents tag their memories with author context so you can filter by source. The evolver agent curates the memory: decides what's worth persisting, prunes stale entries, prevents duplication.

### Where memory lives

- **Project-level `CLAUDE.md`** — evolving understanding of the project: conventions, architecture decisions, human preferences
- **Directory-level `CLAUDE.md` files** — context specific to subsystems (e.g., `src/auth/CLAUDE.md` for auth-related learnings)
- **`.claude/memory/`** — structured memories with frontmatter (type, description, indexed in `MEMORY.md`)

### What gets remembered

- Patterns learned from failures ("this API paginates, always handle it")
- Operational insights ("CI takes ~8 min, don't wait synchronously")
- Human preferences ("prefers small PRs over large ones")
- Risk areas ("module X has no tests, be careful")
- Architecture decisions and their rationale
- Cross-agent learnings (health agent flags something, evolver agent persists the insight)

### What does NOT get remembered

- Things derivable from code or git history
- Ephemeral task state (use GitHub issues for that)
- Debugging specifics (the fix is in the code, the commit message has context)

### Curation

The evolver agent is responsible for memory curation:
- Watches agent activity (via Loki logs) for insights worth persisting
- Writes memories to the appropriate level (project-wide vs directory-specific)
- Prunes stale or outdated memories
- Resolves conflicts when new learnings contradict old memories
- Keeps `MEMORY.md` index concise

## Seed Agents (Minimal Bootstrap Set)

Genesis seeds only the minimum viable agent set. The dev system designs the rest.

### Seeded at bootstrap

- **Orchestrator agent** — the brain. Runs on every cron/event trigger via GitHub Actions. Assesses project state (via `issues.sh summary`), breaks down milestones into tasks, prioritizes, manages dependencies, dispatches work to other agents.
- **Human interaction agent** — all communication with the human, both interactive and async. Onboarding is its first task (not a separate agent). See "Human Interaction" section below.
- **Evolver agent** — must exist from day one. Watches how the system operates and evolves it. Creates new agents, tools, scripts. Curates the memory system.

### Created by the evolver agent as needed

- **Health / quality agent** — created when there are enough moving parts to monitor. Watches for stuck/looping agents, reviews PR quality, verifies done criteria.
- **Worker agents** — created as specific task patterns emerge from the project's work.

### Agent Responsibilities Breakdown

| Responsibility | Agent |
|---|---|
| Assess state, delegate work | Orchestrator |
| Break down milestones into tasks | Orchestrator |
| Prioritization | Orchestrator |
| Dependency management | Orchestrator |
| Communicate with human | Human interaction |
| Onboarding (goal refinement) | Human interaction (first task) |
| Improve the system | Evolver |
| Curate memory | Evolver |
| Detect stuck/spinning | Health (created later) |
| Quality control / PR review | Health (created later) |

## Human Interaction (Detailed)

The human interaction agent operates in two modes:

### Interactive mode (human starts CC session)
When a human opens a Claude Code session in the dev repo, the human interaction agent responds. This is achieved via CLAUDE.md instructions + skills. The agent:
- Knows the project state (reads issues, milestones, recent activity)
- Can answer questions about progress
- Takes feedback and translates it into issue updates
- Runs onboarding (its first interactive task)

### Async mode (system reaches out to human)
When the system needs something from the human (approval, access, clarification, or just reporting progress):
- **Baseline:** opens a GitHub issue tagged `needs:human`. Always works, no setup needed.
- **Optional channels** (configured during onboarding):
  - Slack webhook notifications
  - Email via GitHub notifications (free, already works)
  - Daily digest committed as `.genesis/reports/daily-YYYY-MM-DD.md`
  - A2H gateway (full protocol, when configured)

### Onboarding configures comms
During onboarding, the human interaction agent asks:

> How do you want me to communicate with you?
> - GitHub issues + email notifications (default, works out of the box)
> - Slack notifications (I'll need a webhook URL)
> - Daily digest file in the repo
> - Something else?
>
> You can change this anytime.

Then it **builds the comms infrastructure** based on the answer — creates notification scripts, cron workflows for digests, etc. No hard choices. Sensible defaults. Always overridable.

### Principles
- Don't bother the human with hard choices — offer options with clear defaults
- The human can always override by opening issues, commenting, or starting a CC session
- Batch communications when possible — don't spam
- The human interaction agent builds its own tooling based on the human's preferences

## Future: Head-to-Head Evaluation — Genesis vs ECC

[Everything Claude Code (ECC)](https://github.com/affaan-m/everything-claude-code) takes the opposite approach to genesis: 28 pre-defined agents, 65+ skills, 24 commands, all hand-crafted over 10+ months. It's a static, kitchen-sink configuration optimized for a human developer working interactively with Claude Code. No autonomous operation, no self-improvement, no orchestrator loop.

**Proposed experiment:** Create two identical test repos with the same goal. Install ECC into one, bootstrap genesis into the other. Let them both work toward the goal. Evaluate:

- Time to completion
- Code quality
- Test coverage
- How much human intervention was needed
- How the systems adapted to obstacles
- Final architecture decisions

Key difference: ECC requires a human driving it, genesis runs autonomously. So the real comparison is **human + ECC vs genesis autonomous**. This is the more meaningful real-world question: can an autonomous self-improving system match or beat a human-directed one with pre-built tooling?

**ECC → Genesis is like AlphaGo → AlphaZero.** ECC encodes human expert knowledge (hand-crafted agents, skills, rules refined over 10+ months). Genesis learns from scratch — starts with 3 agents and evolves what it needs. AlphaZero surpassed AlphaGo by discovering strategies humans never considered. The question is whether genesis dev systems will do the same.

The comparison is aspirational — we don't know if genesis even works yet. But once we've bootstrapped a few real projects and the systems have had time to evolve, the head-to-head becomes meaningful.

**Why we don't feed ECC to genesis dev systems:** It would bias the evolver agent toward recreating ECC's structure rather than discovering what the project actually needs. Most of ECC is language-specific and designed for human interaction patterns. Genesis dev systems should evolve from a clean slate.

**Patterns worth noting** (the evolver agent may independently discover these):
- Language-specific rules directories (instead of one big CLAUDE.md)
- Verification loops (iterative checks until passing)
- Hooks for memory persistence across sessions

## Related Work

- **[Hyperagents](https://arxiv.org/abs/2603.19461)** (Zhang et al., 2026) — formalizes "self-referential agents" where the modification procedure itself is modifiable, enabling metacognitive self-improvement. Directly validates genesis's evolver agent design: the dev system doesn't just improve at its task, it improves *how it improves*. Key finding: meta-level improvements (memory persistence, performance tracking) transfer across problem domains — supports our approach of seeding standard meta-concepts across all dev systems. The paper was publicly announced on March 23, 2026 — exactly one day after the genesis repo was created (March 22, 2026). Independent convergence.

## Permission Architecture

One GitHub App, scoped tokens per project. This avoids the 100-App registration limit (per user/org) while maintaining least-privilege per project.

### Design: Single App, scoped installation tokens

GitHub's `POST /app/installations/{id}/access_tokens` endpoint accepts both `repositories` and `permissions` parameters. The token's permissions must be **equal to or less than** the App's registered permissions. So we register one App with the union of all permissions any project could need, and each project mints a narrowly scoped 1-hour token at runtime.

**Genesis App registration (broad — the ceiling):**
- Installed on **all orgs** and **all repos** that genesis manages
- Registered with the union of all permissions: `contents:write`, `pull_requests:write`, `issues:write`, `security_events:read`, `actions:read`, `metadata:read`, etc.
- Private key stored as an **org-level encrypted secret**

**Per-project token (narrow — what each project actually gets):**
```yaml
# repo-guardian example — needs cross-org write + security access
- uses: actions/create-github-app-token@v1
  with:
    app-id: ${{ secrets.GENESIS_APP_ID }}
    private-key: ${{ secrets.GENESIS_APP_PRIVATE_KEY }}
    permission-contents: write
    permission-pull-requests: write
    permission-security-events: read
    permission-issues: write

# read-only auditor example — minimal access
- uses: actions/create-github-app-token@v1
  with:
    app-id: ${{ secrets.GENESIS_APP_ID }}
    private-key: ${{ secrets.GENESIS_APP_PRIVATE_KEY }}
    permission-contents: read
    permission-metadata: read
```

### Why this works
- **1 App, unlimited projects** — no scaling limit, no per-project App ceremony
- **Least-privilege per project** — each project declares exactly what it needs in its workflow
- **1-hour token lifetime** — no long-lived credentials
- **Single install per org** — install once, all projects use it
- **Clean audit trail** — all actions appear as `genesis-dev-bot[bot]`

### Why not per-project Apps
- GitHub limits App **registrations** to 100 per user/org — genesis can easily exceed this
- App registrations cannot be created or deleted programmatically (no API, UI only)
- The manifest flow (`POST /app-manifests/{code}/conversions`) still requires a browser redirect
- Per-token scoping gives the same least-privilege benefit without burning registrations

### Why GitHub Apps over PATs
- PATs are tied to a **person** — if the person leaves or revokes, everything breaks
- PATs can't be cleanly scoped per-project (fine-grained PATs can, but they expire max 1 year and need manual renewal)
- GitHub Apps generate short-lived tokens automatically and produce clean audit trails
- Apps appear as distinct actors in git history and GitHub UI

### Risk: private key leak
If the genesis App's private key leaks, an attacker could mint a max-permission token across all installed repos. Mitigations:
- Store private key as **org-level encrypted secret** (not copied per-repo)
- Rotate the key periodically (GitHub UI: generate new key, revoke old)
- App installation acts as a second gate — must be installed on target repo/org
- Same trust model used by all major GitHub Actions (e.g., `actions/create-github-app-token`)

### Setup flow
1. Human creates the genesis GitHub App with broad permissions (once, manual)
2. Human installs it on all target orgs (once per org, manual)
3. Genesis bootstrapping generates workflows with scoped `permission-*` inputs per project
4. Each project declares its required permissions in `.genesis/config.toml`
5. No per-project setup needed — just deploy and go

### Migration path (existing projects using PATs)
1. Ensure genesis App has all needed permissions registered
2. Ensure genesis App is installed on all target repos/orgs
3. Update workflows to use `actions/create-github-app-token` with scoped permissions
4. Remove PAT secrets

## Open Questions

- Machine user provisioning: how does genesis set up GitHub machine users for agents?
- Cost management: autonomous agents can burn through API credits. Should there be budget awareness?
- State management: where does the orchestrator keep its state between runs? GitHub issues alone, or a lightweight state file in the repo?
