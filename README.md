# Genesis

A minimal meta-factory that bootstraps autonomous, self-improving AI dev systems.

You give genesis a goal. Genesis creates an AI system — built on GitHub and a coding agent, [Claude Code](https://docs.anthropic.com/en/docs/claude-code) or [OpenAI Codex](https://github.com/openai/codex) — that works toward that goal autonomously. The system designs its own agents, tools, and workflows. It monitors its own progress. It improves how it improves.

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

## Getting Started

### One-time setup (per adopter)

Genesis uses **one GitHub App per user/org, shared by every project you bootstrap** - so this happens once, not per project, and your credentials live in a single central place.

1. **Create your genesis GitHub App** (once). Permissions: Contents R/W, Issues R/W, Pull requests R/W, Workflows R/W, Actions R/W, Metadata R; webhook disabled. (Actions R/W is what lets a merge re-dispatch the orchestrator and lets an agent read failed runs; Workflows R/W is a different permission and doesn't cover it.) **Pages R/W is optional** — add it only if a project needs to manage Pages settings; publishing to Pages works off the built-in `GITHUB_TOKEN` without it. Install it on the orgs/accounts you want genesis to manage, and generate a private key (downloads a `.pem`). Full walkthrough: [docs/bootstrapping-sessions/001-repo-guardian.md](docs/bootstrapping-sessions/001-repo-guardian.md).

2. **Populate `~/.config/genesis/.env`.** Genesis writes this file with placeholders the first time you bootstrap a project (and never overwrites an existing one). Fill in the three values - they're shared across all your projects:

   ```bash
   ANTHROPIC_API_KEY=          # your Anthropic API key
   GENESIS_GITHUB_APP_ID=      # your App's numeric ID
   GENESIS_GITHUB_APP_SECRET=  # the App's private key (full PEM, BEGIN/END lines included)
   ```

### Bootstrap a project

1. **Scaffold + publish.** In a Claude Code or Codex session in this repo, run the `genesis-new` skill with your goal. Genesis asks which engine the system should run on, picks a topology, scaffolds the dev system, creates the GitHub repo, opens issue #1, and **publishes the workflows disabled** so they don't fail before credentials exist.

2. **Activate.** From a clone of the new dev repo, run one command:

   ```bash
   .genesis/scripts/activate.sh
   ```

   It reads the three values from `~/.config/genesis/.env`, verifies the App is installed on the repo, sets them as the repo's Actions secrets, records the App's bot login (`<your-app-slug>[bot]`) as the `GENESIS_APP_BOT` Actions variable, and enables the workflows. The workflows pass that variable to `claude-code-action`'s `allowed_bots`, which is what lets your App's own dispatches (like the orchestrator wake after an auto-merge) run. Nothing in the seeded files names a particular App, so any App name works. It refuses to run if any value is missing/placeholder or the App isn't installed.

The next trigger (an issue/PR/comment event, a push, or the cron) wakes the orchestrator, and onboarding begins on issue #1. Every dev repo ships this same `activate.sh` and a **Setup** section in its own README for whoever operates it.

## What Genesis Seeds

Every dev system gets a scaffold that it can evolve:

- **Seed agents** — three to start: orchestrator, human interaction, and evolver. The orchestrator also plays project manager, onboarder, and health-checker until the system earns the evidence to split those into their own agents.
- **Orchestrator workflows** — GitHub Actions (cron + event-triggered) launching Claude Agent SDK sessions
- **Either engine** — the rules live in `AGENTS.md`, which both Claude Code and Codex read, and `CLAUDE.md` imports it. The system gets hooks for both (`.claude/settings.json` and `.codex/hooks.json`), and `.genesis/config.toml` names the engine `genesis serve` runs, so switching is one line. `genesis-new` asks which one you want. GitHub Actions mode runs Claude Code either way.
- **Observability** — Claude Code hooks that log all agent activity to Grafana Loki (opt-in — without the Loki secrets, Actions runs leave no activity trail)
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
│    (also plays PM · onboarder · health-checker)     │
│       │                                             │
│       ├──> Human Interaction (A2H protocol)         │
│       ├──> Evolver (evolves the whole system)       │
│       └──> Worker agents & specialists —            │
│            grown from evidence, not seeded          │
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

## Local Control Plane

By default, every dev system runs its orchestrator on GitHub Actions (zero setup, always on). For long sessions, interactive steering, local resource access, or to avoid burning GHA minutes, the orchestrator can also run locally via the `genesis` CLI.

```bash
# From inside any dev repo:
genesis serve              # Run orchestrator locally; auto-disables GHA workflows
genesis workflows disable  # Manually disable all active workflows
genesis workflows enable   # Re-enable manually-disabled workflows
```

`genesis serve`:
- Runs its sessions on the dev system's engine: `--engine`, then `GENESIS_ENGINE`, then `[engine] name` in `.genesis/config.toml`, then `claude`. A Codex system runs only in local mode.
- Preflight-checks that the engine's binary (`claude` or `codex`) is on PATH before touching workflows, so the repo never ends up with GHA disabled and no working local orchestrator.
- Disables the active `genesis-*` GHA workflows on start (re-enables on graceful shutdown — Ctrl+C), leaving CI and other gates running. `--all-workflows` disables every workflow instead. Prevents the "two cooks in the kitchen" problem. Tracks the set it disabled in `.genesis/.disabled-by-genesis` and only re-enables that set, so workflows the user had paused before `genesis serve` stay paused.
- Polls the GitHub repo events API (`/repos/{owner}/{repo}/events`) with ETags. One call covers issues, comments, PRs, pushes — no rate-limit cost when nothing changed. The events endpoint returns at most 100 events per page; if a poll sees more activity than that, genesis logs a warning suggesting a shorter `--poll-interval`.
- Launches a session against the orchestrator agent on each relevant event: `claude -p` for Claude Code, `codex exec --json` for Codex. Same agent definition as GHA mode.
- Keeps each session within its budget. Claude Code enforces its own `--max-turns`; Codex has none, so the plane counts a Codex session's tool calls and stops it at the budget. A session that reaches its budget is resumed where it stopped, up to six times.
- Bounds what a task and a whole run may spend, in the engine's own unit: dollars for Claude Code, tokens for Codex, settled from Codex's session log after each session. The Codex defaults come from the first dev system run on Codex, whose whole first milestone took about 640,000 tokens.
- PID lock at `.genesis/.orchestrator.lock` prevents concurrent local instances. State (ETag, high-water event id) persists in `.genesis/`.

Config (env vars or CLI flags):

| Var | Flag | Default |
|---|---|---|
| `GENESIS_REPO` | `--repo` | detected from git remote |
| `GENESIS_POLL_INTERVAL` | `--poll-interval` | 60 (seconds) |
| `GENESIS_SESSION_TIMEOUT` | `--session-timeout` | 3600 (seconds) |
| `GENESIS_ENGINE` | `--engine` | `[engine] name` in `.genesis/config.toml`, else `claude` |
| `GENESIS_AGENT` | `--agent` | `.claude/agents/orchestrator.md` |
| | `--all-workflows` | off: only `genesis-*` workflows are disabled |
| `GENESIS_TASK_COST_CEILING` | | $50 per task (Claude Code) |
| `GENESIS_RUN_COST_BUDGET` | | $150 per run (Claude Code) |
| `GENESIS_TASK_TOKEN_CEILING` | | 1,000,000 tokens per task (Codex) |
| `GENESIS_RUN_TOKEN_BUDGET` | | 3,000,000 tokens per run (Codex) |

Auth uses the user's existing `gh` CLI (`gh auth token`), plus `ANTHROPIC_API_KEY` from the environment for Claude Code, or a Codex login (`codex login`, or `CODEX_API_KEY`) for Codex.

If `genesis serve` exits non-gracefully, GHA workflows stay disabled. Recover with `genesis workflows enable` or by re-running `genesis serve`.

### Fleet Supervision and Stall Watchdog (macOS `launchd`)

When running several dev systems locally, pin a dedicated detached-HEAD runtime clone with `scripts/update-plane.sh` (`~/git/genesis-plane`, checked out at `--detach origin/main` so active agent checkouts never mutate the running control-plane code), list your repos in `~/.config/genesis/fleet.toml`, and let `launchd` supervise them:

```toml
[runtime]
plane_dir = "~/git/genesis-plane"
watch_interval = 180

[[repos]]
name = "my-project"
path = "~/git/my-project"
enabled = true
```

```bash
genesis fleet sync             # Install/update LaunchAgents for enabled repos + genesis watch
genesis fleet status           # Show PID, state, engine, spend, and last activity across repos
genesis fleet resume my-project # Clear .genesis/.stopped (e.g. after a run-budget stop) and start
genesis fleet restart all      # Restart all enabled planes (e.g. after scripts/update-plane.sh)
genesis fleet stop my-project  # Gracefully stop a plane and keep launchd from respawning it
genesis watch                  # Run one board-vs-execution stall check across the fleet
```

`genesis serve` writes `.genesis/.plane-state.json` on lifecycle transitions and `.genesis/.stopped` only on non-restartable stops (`run_budget`, `gh_auth_401`, `missing_binary`, or `genesis fleet stop`), so `launchd` automatically restarts crashed or rebooted planes without respawning budget-stopped ones. `genesis watch` cross-checks `.plane-state.json` against each repo's GitHub issues, pull requests, and Actions runs to auto-release stranded `in-progress` claims, nudge idle planes via `.genesis/.nudge`, and send one consolidated alert when any repo needs attention.

## Project Structure

```
genesis/
├── src/genesis/          # Core Python package
│   ├── cli.py            # `genesis` CLI entry point (serve, workflows)
│   ├── server.py         # Local control plane (poll loop, orchestrator launch)
│   ├── engines.py        # Claude Code and Codex: session commands, output parsing, budgets
│   ├── workflows.py      # Enable/disable GHA workflows via `gh`
│   ├── scaffold.py       # Create/augment repos with dev system scaffolding
│   └── github.py         # GitHub integration (repo creation, issue #1)
├── templates/            # Templates for scaffolded dev systems
│   ├── agents/           # Seed agent definitions
│   ├── scripts/          # log.sh, issues.sh, activate.sh
│   ├── workflows/        # GitHub Actions orchestrator workflows
│   ├── agents_md.md.j2   # AGENTS.md template: the rules both engines read
│   ├── config.toml.j2    # .genesis/config.toml, including the [engine] name
│   ├── codex_hooks.json  # Codex hooks (.codex/hooks.json)
│   ├── gitignore         # Dev repo .gitignore (dotted on the way out; a
│   │                     #   dotted name here would ignore templates/ itself)
│   └── settings.json     # Claude Code hooks (.claude/settings.json)
├── tests/
│   ├── unit/             # Unit tests for cli/server/workflows
│   └── e2e/              # End-to-end tests for all topologies
├── docs/                 # design.md, evaluations.md
└── CLAUDE.md             # Project instructions
```

## Status

Early development. Scaffolding engine, `genesis-new` skill, and local control plane (`genesis serve`) are functional with passing unit and e2e tests, on Claude Code and on Codex. The first dev system run on Codex, [genesis-codex-trial](https://github.com/Sayfan-AI/genesis-codex-trial), finished its milestone end to end. See [docs/design.md](docs/design.md) for architecture and [docs/evaluations.md](docs/evaluations.md) for technology decisions.

## Related Work

- **[Hyperagents](https://arxiv.org/abs/2603.19461)** (Zhang et al., 2026) — formalizes self-referential agents with modifiable modification procedures. Genesis's evolver agent is a practical implementation of this concept. The paper was publicly announced exactly one day after the genesis repo was created — independent convergence on the same idea.
- **[A2H Protocol](https://github.com/twilio-labs/Agent2Human)** (Twilio) — open-source agent-to-human communication protocol used by genesis dev systems.
- **[Poetiq - Recursive Self-Improvement](https://poetiq.ai/posts/recursive_self_improvement_coding/) Delivers new SOTA Coding performance

## License

MIT
