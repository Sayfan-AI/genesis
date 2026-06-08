# Genesis - Design

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
- **Human interaction agent** — all communications with the user via CC sessions, Claude Code Channels (Telegram/Discord/iMessage), GitHub issues, and custom notification layers. Handles onboarding, reports, escalations, access requests, milestone sign-offs. Owns and evolves the comms infrastructure.
- **Worker agents** — do the actual work (code, tests, infra, etc.). These are the ones the dev system designs for itself based on the goal.
- **Evolver agent** — responsible for evolving the dev system itself. Observes how the system operates, designs specialized worker agents for recurring task patterns, creates tools and skills, designs and refines the memory system (CLAUDE.md files at different levels, settings.json, hooks). Refactors the agent roster as the project evolves. Learns from failures and adapts the system's approach.
- **Self-review / health agent** — monitors system health, catches loops, audits quality.

## Execution Model

The orchestrator is **trigger-agnostic**. It reads GitHub issues, assesses project state, dispatches sub-agents, and exits. It doesn't know or care how it was triggered. Genesis supports two execution modes that can run independently or together.

### GitHub Actions Mode (Default)

GitHub Actions serve as the trigger layer:
- **Scheduled workflows** (cron) — periodic advancement of project state
- **Event-triggered workflows** — new issue opened/closed, PR merged, human feedback, comments, etc.

Each trigger launches the orchestrator as a Claude Agent SDK session on a GHA runner. The runner is ephemeral — state persists in GitHub issues and committed files, not on the runner.

**Why it's the default:**
- **Zero setup** — works out of the box with any GitHub repo
- **Always on** — runs when the user's machine is off, on vacation, at 3am
- **Self-contained** — no daemon to manage, no "did I leave it running?"
- **Event-driven natively** — GitHub triggers workflows on issue/PR/comment events directly
- **Clean environment** — every run starts fresh, no state leaks, no zombie processes
- **Built-in secrets management** — encrypted secrets, no local credential files
- **Audit trail** — every run logged in the Actions tab
- **Scales to many projects** — 50 dev repos all running on GHA without 50 local processes

**Limitations:** Runner time limits (6 hours max per job on free tier), limited free minutes (2000/month), cold start latency, concurrency caps, no access to local resources.

In practice, the time limit is rarely a constraint — the orchestrator is a thin coordinator that should assess, dispatch, and exit quickly. Sub-agents doing implementation work should also be scoped to fit within reasonable windows.

### Local Mode (Opt-in)

For users who need longer sessions, interactive steering, or access to local resources, genesis supports a local control plane that runs the same orchestrator on the user's machine.

The local control plane is the `genesis serve` subcommand of the genesis CLI. The user installs genesis once and runs `genesis serve` from inside any dev repo. Structurally parallel to the GHA workflow — GHA is "on event, run claude with orchestrator prompt", `genesis serve` is the same thing in a poll loop:

1. **Disable GHA** — on start, `genesis serve` disables all currently-active GitHub Actions workflows in the repo so we don't have "two cooks in the kitchen". On graceful shutdown, it re-enables them. `genesis workflows enable|disable` lets the user manage this manually if a non-graceful exit leaves them disabled.
2. **Poll** — queries the GitHub repo events API (`/repos/{owner}/{repo}/events`) with ETags. One API call covers all repo activity (issues, comments, PRs, pushes, labels). Returns `304 Not Modified` when nothing changed — doesn't count against rate limit.
3. **Launch** — on first start (initial assessment) and whenever new activity is detected, launches `claude -p` with the orchestrator prompt. Same agent definition, same logic as GHA mode. The orchestrator reads full issue state when it runs.
4. **Concurrency guard** — `.genesis/.orchestrator.lock` (PID-based) prevents multiple `genesis serve` instances on the same repo. If a session exceeds its timeout, the entire process tree is killed. Intermediate work is already in GitHub issues or committed files — the next run picks up.

**Secrets:** Needs `ANTHROPIC_API_KEY` (for Claude) and `gh` CLI authentication (for GitHub API). The user already has both if they're running Claude Code locally. No additional secrets infrastructure needed — the user's machine is a trusted environment.

**Sandboxing:** The orchestrator session can run with Claude Code's built-in sandboxing. For stronger isolation, the user can run `genesis serve` inside Docker.

**Configuration** (environment variables):
- `GENESIS_POLL_INTERVAL` — seconds between polls (default: 60)
- `GENESIS_SESSION_TIMEOUT` — max seconds per orchestrator session (default: 3600)
- `GENESIS_REPO` — owner/repo (default: detected from git remote)

**When to use local mode:**
- Sessions that need to exceed GHA's 6-hour limit
- Work that requires access to local services, databases, or hardware
- The user wants to steer the system interactively alongside the orchestrator
- Cost optimization — no GHA minutes consumed, only API tokens

**Limitations:** Requires a machine running (user's laptop, a server, a container). The user must run the local control plane process. System stops when the machine sleeps or shuts down.

**Interactive steering:** The user can always start an interactive Claude Code session in the dev repo while the orchestrator is running. The interactive session is aware of running orchestrator sessions via the lock file at `.genesis/.orchestrator.lock`, so the user can check status, pause, or redirect work.

### Running Both Modes

GHA and local mode coordinate through the same GitHub issues — no conflict. The two modes are mutually exclusive in practice: when the user runs `genesis serve`, it disables all active GHA workflows on start and re-enables them on graceful shutdown. This is the cross-mode concurrency guard — there's no need for GHA to check whether a local session is running, because if one is, GHA workflows are already disabled. If `genesis serve` exits non-gracefully, workflows stay disabled until the user re-runs `genesis serve` or runs `genesis workflows enable`.

A natural pattern for active projects: GHA handles event triggers around the clock (PR merged at 3am, external contributor opens an issue) most of the time, and the user temporarily switches to local mode (`genesis serve`) for sessions that need faster iteration, interactive steering, or local resource access. The onboarding agent configures the initial mode; the evolver can adjust the mix as the project's needs change.

### Orchestrator Behavior

Regardless of mode, the orchestrator:
- Reads GitHub issues to assess project state
- Breaks down current milestone work into tasks (issues)
- Dispatches sub-agents for execution
- May spawn sub-agents that spawn their own sub-agents
- Should be aware of its time budget (passed as env var) to avoid starting large tasks near a deadline
- Captures all intermediate state in GitHub issues or committed files — never relies on in-memory state surviving across runs

The orchestrator is a **thin coordinator**: assess, decide, dispatch. It does not do implementation work itself. If its context window is filling up, something is wrong.

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

### With Dev Systems — Two Primitives

Human-system interaction rests on two primitives. Everything else is built on top.

**1. GitHub Issues (async coordination)**
- The dev system opens issues to the human (tagged `needs:human`) for access requests, approvals, clarifications, and progress reports
- The human opens issues to the dev system for new tasks, direction changes, feedback
- Issues are the shared language — both sides read and write them
- Labels, assignments, and milestones provide structure without ceremony
- Email notifications are built in — the human gets pinged without any setup

**2. Ad-hoc Claude Code sessions (interactive steering)**
- The human opens a CC session in the dev repo whenever they want
- The human interaction agent responds — knows project state, can answer questions, takes direction
- Session outcomes become GitHub issues: new tasks for agents, action items for the human, updated milestones
- This is the "steering wheel" — the human can course-correct at any time

### Claude Code Channels (real-time communication)

[Claude Code Channels](https://code.claude.com/docs/en/channels) (v2.1.80+, March 2026) are MCP servers that push events into a running CC session. They give the human interaction agent a real-time, bidirectional communication layer without building custom infrastructure.

**How it works:**
1. An external message arrives (Telegram DM, Discord mention, iMessage, webhook, etc.)
2. The channel plugin injects it into the active CC session as a `<channel source="...">` event
3. The human interaction agent reads the event, processes it with full project context
4. The agent calls the channel's `reply` tool
5. The reply appears on the human's phone/desktop

**Built-in channel plugins:** Telegram, Discord, iMessage, plus a `fakechat` plugin for testing.

**Custom channels:** Any MCP server can implement the channel protocol. This means the dev system can build custom channel plugins for any platform — Slack, email, SMS, web dashboard with live chat, etc.

**How genesis uses channels:**

The human interaction agent is the channel endpoint. When channels are configured:

- **Proactive notifications** — milestone completions, blockers, access requests arrive on the human's phone via Telegram/Discord/iMessage instead of (or in addition to) GitHub issue emails
- **Remote steering** — the human sends instructions from their phone ("pause security scanning, focus on dependency updates"); the agent executes with full project context
- **Permission relay** — when the system needs approval for a tool call, the prompt is forwarded to the human's device; they approve/deny remotely
- **Escalation with context** — when the system is stuck, the human interaction agent sends a rich message explaining the situation, not just a GitHub issue title

**Key constraint:** Channels require an active CC session. For genesis dev systems running via GitHub Actions, this means either:
- The orchestrator workflow keeps a session alive with channels enabled (consumes a runner)
- Channels are used only during ad-hoc human sessions (human opens CC with `--channels`)
- A lightweight always-on session runs on the human's machine or a persistent server, receiving channel events and managing issues

The right approach depends on the project. The human interaction agent configures this during onboarding based on the human's preferences.

**Relation to GitHub Issues:** Channels don't replace issues. Issues remain the persistent, searchable, structured coordination layer. Channels provide the real-time notification and conversational layer on top. The human interaction agent uses both — issues for tracking, channels for communication.

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

Local mode (`genesis serve`) is a subcommand of the genesis CLI itself, not a script seeded into the dev repo. See **Execution Model > Local Mode**.

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

The dev system uses Claude Code's native memory mechanisms — CLAUDE.md files and `.claude/rules/` — as its persistent memory. Memory is how the system gets smarter across sessions. GitHub Actions runners are ephemeral; without committed memory, the system starts from scratch every time.

### Memory is shared, not per-agent

All agents read and write to the same memory system. No silos. Agents tag their memories with author context so you can filter by source. The evolver agent curates the memory: decides what's worth persisting, prunes stale entries, prevents duplication.

### Where memory lives

- **Project-level `CLAUDE.md`** — evolving understanding of the project: conventions, architecture decisions, human preferences
- **Directory-level `CLAUDE.md` files** — context specific to subsystems (e.g., `src/auth/CLAUDE.md` for auth-related learnings)
- **`.claude/rules/`** — modular, path-scoped instruction files for specific subsystems or concerns

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
- **Human interaction agent** — all communication with the human: interactive CC sessions, Claude Code Channels (Telegram/Discord/iMessage), GitHub issues, and any custom notification layer. Onboarding is its first task. Owns and evolves the comms infrastructure. See "Human Interaction" section below.
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
| Claude Code Channels | Human interaction |
| Build/evolve comms infra | Human interaction |
| Improve the system | Evolver |
| Curate memory | Evolver |
| Detect stuck/spinning | Health (created later) |
| Quality control / PR review | Health (created later) |

## Human Interaction (Detailed)

The human interaction agent is the dev system's voice. All communication with the human — in any direction, through any medium — goes through it. The orchestrator dispatches it as a sub-agent when the system needs to communicate, and it responds directly when the human initiates contact.

### When the human interaction agent runs

| Trigger | How | What happens |
|---|---|---|
| Onboarding (issue #1 open) | Orchestrator dispatches it | Runs interactive onboarding flow |
| Human opens CC session | CLAUDE.md routes to it | Answers questions, takes feedback, opens issues |
| Human sends a channel message | Channel event injected into session | Processes request with project context, replies |
| Milestone completed | Orchestrator dispatches it | Reports completion via configured channels + issue update |
| System blocked on human | Orchestrator dispatches it | Opens `needs:human` issue, sends channel notification |
| Escalation (stuck >2 cycles) | Orchestrator dispatches it | Rich escalation via channels with context |

### Interactive mode (human starts CC session or sends channel message)
The human interaction agent responds with full project awareness:
- Summarizes current state, recent progress, blockers
- Answers questions about decisions, progress, architecture
- Takes feedback and translates it into issue updates
- Accepts direction changes and re-prioritizes via new issues
- Runs onboarding (its first interactive task)

This works the same whether the human opens a terminal CC session or sends a Telegram/Discord message via channels — the agent has the same context and tools either way.

### Async mode (system reaches out to human)
When the system needs something from the human:
- **Always:** opens a GitHub issue tagged `needs:human` with clear context
- **If channels configured:** sends a notification via Telegram/Discord/iMessage with a summary and link to the issue
- **If digest configured:** includes it in the next daily digest

### Onboarding configures comms
During onboarding, the human interaction agent asks:

> How do you want me to communicate with you?
> - GitHub issues + email notifications (default, works out of the box)
> - Claude Code Channels — I'll send notifications to your phone via Telegram, Discord, or iMessage. You can also send me instructions from your phone.
> - Daily digest file committed to the repo
> - Something custom? I can build any notification system you want (Slack webhook, email via SMTP, web dashboard, CLI tool, etc.)
>
> You can change this anytime.

Then it **builds the comms infrastructure** based on the answer:
- For channels: configures the channel plugin, sets up pairing, tests the connection
- For digests: creates the generation script and cron workflow
- For custom: treats it as a task — designs, builds, and deploys the integration

### The human interaction agent builds its own tools

This is what distinguishes it from the orchestrator just opening issues. The human interaction agent owns the communication layer and evolves it:
- Starts with whatever the human chose during onboarding
- If the human later says "actually, add Slack too" — the agent builds it
- If notifications are too noisy — the agent adjusts batching and filtering
- If the human wants a status dashboard — the agent builds one
- Custom communication tools are just tasks the system executes, committed to the repo like any other code

### Principles
- Don't bother the human with hard choices — offer options with clear defaults
- The human can always override by opening issues, commenting, starting a CC session, or sending a channel message
- Batch communications when possible — don't spam
- One reminder for blocking requests. Don't nag.
- Issues are for tracking, channels are for talking. Use both.

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

- **[Harness Design for Long-Running Application Development](https://www.anthropic.com/engineering/harness-design-long-running-apps)** (Anthropic Engineering) — demonstrates that multi-agent architectures with separated evaluation dramatically improve performance on complex, long-running coding tasks. Key patterns directly relevant to genesis:
  - **Generator-evaluator separation** — separating code generation from evaluation (using Playwright to test running apps) catches stubbed-out features and quality issues that self-evaluation misses. Genesis's health/self-review agent fills the evaluator role, while worker agents are generators.
  - **Context degradation** — long sessions cause coherence loss and "context anxiety" (premature task completion as models approach perceived limits). Genesis mitigates this by design: each GitHub Actions trigger spawns a fresh session, and state persists in issues/memory rather than in-context.
  - **Structured handoffs via files** — agents communicate through file-based artifacts rather than shared context. Genesis uses the same pattern: committed files (digests, data, CLAUDE.md) and GitHub issues as the handoff medium between ephemeral sessions.
  - **Contract negotiation** — before implementation, evaluator and generator agree on sprint success criteria. Parallels how genesis's orchestrator creates issues with done criteria before dispatching workers.
  - **Continuous harness optimization** — the harness itself should evolve as models improve. This is exactly the evolver agent's job in genesis. Key principle: "every component in a harness encodes an assumption about what the model can't do on its own, and those assumptions are worth stress testing because they can quickly go stale as models improve."
  - **Evaluator training** — out-of-the-box evaluators exhibit positive bias (praising mediocre work). Effective evaluation requires iterative prompt tuning against observed judgment failures. Genesis's health agent should undergo the same calibration.
  - Results: solo agent ($9, 20 min) produced broken output; full harness ($200, 6 hours) produced functional full-stack apps. With Opus 4.6, simplified harness (dropped sprint decomposition, end-of-cycle evaluation only): $125, 3h50m. Validates both that orchestration pays for itself and that harness components should be regularly stress-tested for continued necessity.

- **[GSD (Get Shit Done)](https://github.com/gsd-build/get-shit-done)** (TACHES, ~48K stars) — a spec-driven development framework for AI coding agents. Two versions: v1 is prompt-only (markdown skills/slash commands), v2 is a TypeScript CLI on the Pi SDK with programmatic control over sessions. Key patterns relevant to genesis:
  - **Thin orchestrator** — orchestrator stays at 10-15% context usage, passes file paths (not contents) to sub-agents. Each worker gets a fresh ~200K token context window. Genesis's orchestrator should follow the same discipline: assess state, dispatch, don't do heavy lifting.
  - **Three-level work hierarchy** — milestone (shippable version) → slice (demoable vertical capability) → task (one context-window-sized unit). Genesis uses GitHub milestones and issues but doesn't enforce a strict hierarchy — the evolver agent should consider whether more structure helps.
  - **File-driven state machine** — `.gsd/` directory is the sole source of truth. No in-memory state survives across sessions. Auto mode reads disk, determines next work unit, spawns fresh agent, repeats. Directly analogous to genesis using GitHub issues as the state machine, though genesis chose issues over files for human visibility.
  - **Crash recovery** — if a session dies, next run reads surviving state from disk and synthesizes a recovery briefing. Genesis gets this for free from GitHub issues (persistent, external to runner).
  - **Plan → execute → verify loop** — each slice goes through planning, per-task execution in fresh context, verification, then roadmap reassessment. Genesis should ensure the orchestrator follows a similar discipline rather than jumping straight to execution.
  - **Context rot as first-class concern** — GSD's entire architecture is motivated by quality degradation in long sessions. Genesis mitigates this by design (ephemeral GHA sessions), but the evolver should watch for signs of context degradation in long-running sub-agent work.
  - Key difference from genesis: GSD is human-interactive (user drives phases), genesis is autonomous. GSD externalizes state to local files, genesis to GitHub issues. GSD's v1 "prompt-and-hope" approach vs v2 programmatic control parallels the distinction between genesis seeding good patterns vs having a harness that enforces them.

## Permission Architecture

**One GitHub App per user or org**, scoped tokens per project. Each genesis adopter creates their own App (the App's private key cannot be safely shared between humans — it's a single root credential that signs JWTs for every installation). Within an adopter's scope, one App backs unlimited projects, avoiding GitHub's 100-App-registration limit per user/org while maintaining least-privilege per project.

### Design: Single App, scoped installation tokens

GitHub's `POST /app/installations/{id}/access_tokens` endpoint accepts both `repositories` and `permissions` parameters. The token's permissions must be **equal to or less than** the App's registered permissions. So each adopter registers one App with the union of all permissions any of their projects could need, and each project mints a narrowly scoped 1-hour token at runtime.

**Genesis App registration (broad — the ceiling):**
- Installed on **all orgs and repos** the adopter wants genesis to manage (can be "all repositories" or "selected repositories" — the adopter's call).
- Registered with the union of all permissions: `contents:write`, `pull_requests:write`, `issues:write`, `actions:write`, `workflows:write`, `metadata:read`, plus `secrets:write` (needed only by the seeding workflow on the adopter's source repo — see below).
- Private key + App ID are seeded onto each genesis-managed dev repo as encrypted **repo secrets** (`GENESIS_APP_ID`, `GENESIS_APP_PRIVATE_KEY`). Per-repo storage rather than org-level because dev repos may span multiple orgs (including the adopter's personal account), and org-level secrets don't propagate across orgs.

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

### Secret seeding
Each new dev repo needs `GENESIS_APP_ID`, `GENESIS_APP_PRIVATE_KEY`, and `ANTHROPIC_API_KEY` set as repo secrets before its orchestrator workflows can run. The canonical pattern:

- The adopter's local genesis install (`~/.config/genesis/` or equivalent) holds the source-of-truth values for their App and Anthropic key. **Genesis is built around one GitHub App per user/org backing _every_ project that adopter bootstraps**, so the credentials are entered exactly once, centrally - not per project. On the first bootstrap genesis writes a placeholder `~/.config/genesis/.env` (only if one isn't already there - a later project reuses the same file untouched) holding `ANTHROPIC_API_KEY`, `GENESIS_GITHUB_APP_ID`, and `GENESIS_GITHUB_APP_SECRET` (the App's PEM); the human populates it once. The file is never copied into a dev repo. A dev repo's `.genesis/scripts/activate.sh` is the single human-run command that wakes it: it sources that `.env`, verifies the App is installed on the repo (minting a short App JWT from the ID + PEM), `gh secret set`s the three values onto the repo, and enables the workflows - the local CLI counterpart to the seeding workflow below.
- Their genesis "source" repo (e.g., `<org>/genesis`) holds the same values as repo secrets — enabling cross-repo seeding from inside Actions.
- A `genesis-seed-secrets.yml` workflow on the source repo, triggered with a `target_repo` input, mints an App token with `permission-secrets: write` scoped to the target repo, then `gh secret set`s the three values onto it. This means `genesis-new` (and equivalents) can call the seeding workflow as the last step of repo creation and the new dev system is immediately operational.
- Same workflow handles **rotation**: regenerate the App's private key once in GitHub, update the source repo's secret, re-run the seeding workflow across all managed dev repos.

### Risk: private key leak
If the App's private key leaks, an attacker could mint a max-permission token across all installed repos. Mitigations:
- Private key is held in the adopter's local secret store and replicated to dev repos as encrypted GitHub secrets — never stored in plaintext, never committed.
- Rotate the key periodically (GitHub UI: generate new key, revoke old; then re-run the seeding workflow to propagate).
- App installation acts as a second gate — must be installed on target repo/org.
- The rotation cost is real: an N-repo blast radius rather than a single org-level secret. The seeding workflow makes this a one-command batch update rather than per-repo manual work.
- Same trust model used by all major GitHub Actions (e.g., `actions/create-github-app-token`).

### Setup flow
1. Adopter creates **their own** genesis GitHub App with the broad permission set (once, manual via App-manifest flow ideally). Generates a private key and notes the App ID.
2. Adopter installs the App on the orgs/accounts they want genesis to manage (once per org, manual).
3. Adopter stores `GENESIS_APP_ID`, `GENESIS_APP_PRIVATE_KEY`, and `ANTHROPIC_API_KEY` locally and on their genesis source repo.
4. Adopter runs `genesis-new` / equivalent. Genesis scaffolds the dev repo and triggers the seeding workflow on the source repo, which propagates the three secrets onto the new dev repo.
5. Generated workflows use `actions/create-github-app-token` with scoped `permission-*` inputs per workflow (least-privilege at use-time).
6. No per-project App setup — just per-project secret seeding, automated by the seeding workflow.

### Migration path (existing projects using PATs)
1. Ensure genesis App has all needed permissions registered
2. Ensure genesis App is installed on all target repos/orgs
3. Update workflows to use `actions/create-github-app-token` with scoped permissions
4. Remove PAT secrets

## Open Questions

- Machine user provisioning: how does genesis set up GitHub machine users for agents?
- Cost management: autonomous agents can burn through API credits. Should there be budget awareness?
- State management: where does the orchestrator keep its state between runs? GitHub issues alone, or a lightweight state file in the repo?
