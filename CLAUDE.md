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

## Observability

- Grafana Cloud stack: <https://bouncymillet382.grafana.net>. The "Genesis dev system activity" dashboard is at `/d/genesis-activity`, defined in `templates/dashboards/genesis-activity.json` and uploaded with `scripts/upload-dashboard.sh`.
- Loki push endpoint: `https://logs-prod-021.grafana.net` (instance `1694942`). Credentials live in `~/.config/genesis/.env`, never in the repo: `GRAFANA_TOKEN` is a `glsa_` service-account token for the dashboard API, the `GENESIS_LOKI_*` trio uses a `glc_` access-policy token for pushes. The two token kinds aren't interchangeable.

## Claude Code Hooks Format

The correct hooks format in `.claude/settings.json` requires a `matcher` + `hooks` array structure. Each hook event entry must look like:

```json
{"matcher": "", "hooks": [{"type": "command", "command": "..."}]}
```

NOT the flat format `{"type": "command", "command": "..."}` — that is invalid and causes Claude Code settings errors. The template at `templates/settings.json` must always use the correct format.

## Workflow Turn Budgets

Every workflow that invokes Claude — genesis's own and every template — must pass an
explicit `--max-turns`, sized by class. `error_max_turns` is the worst failure shape
in this system: no progress *and* no diagnosis, and the next run redoes the work.

- **Orchestrator class** — open-ended, and dispatched subagents spend from the same
  budget. **Floor: 30.** Templates seeded at 40.
- **Narrow class** — fixed procedure, deliberately small so a run that needs more
  turns fails fast instead of wandering. The fix for a starved narrow run is a
  tighter procedure, not a bigger budget. **No template is narrow-class today.**
  `genesis-merge.yml` was the only one until it stopped invoking Claude at all: a
  fixed procedure with no fuzzy step in it is a script, and the strongest form of
  "the budget is small enough" is having no budget to run out of. When a narrow
  workflow needs a *third* tightening, ask whether it needs a model.

Classes live in `WORKFLOW_TURN_CLASSES` in `src/genesis/scaffold.py`; the floor is
enforced by `tests/e2e/test_workflows.py`, which also fails if a Claude-invoking
template is unclassified, declares no `--max-turns` at all, or is still classified
after it stopped invoking Claude. Two separate dev-system workflows died at 20 turns
three weeks apart before this floor existed — when a run dies at max-turns, raise the
whole class and record why.

## Claims

`in-progress` is the dev system's concurrency protocol — `issues.sh next` skips any
issue carrying it — so the rules about taking it back are load-bearing:

- **A claim records the session that made it.** `claim` writes a marker comment
  naming `GENESIS_SESSION` (or the Actions run id). The label alone carries no
  identity and no timestamp, so a claim without the marker can't be matched to a
  session or dated, and `claim` gives the label back rather than hold one.
- **Release keys on the ladder's not-continuing decision, not on age.** When the
  continuation ladder in `server.py` declines to resume a chain, the plane releases
  that chain's claims. A chain that *is* resumed keeps them, and so does one that
  ends in success — it may have a pull request open.
- **`sweep-claims` is the backstop, and its window must clear the session cap.**
  The plane passes twice `session_timeout`, and the script refuses anything under
  an hour. A shorter window races a healthy session and puts two workers on one
  issue: two branches, a merge conflict, neither run aware of the other. That
  false positive is more expensive than the stuck label it would fix, which is why
  age is the second layer and never the first.

## CI

`.github/workflows/ci.yml` runs the suite on every push to `main` and every PR. It is
what turns the guards above from convention into enforcement — before it existed, the
suite ran only when a human remembered to, and several changes merged on the strength
of a hand-run result.

Two properties keep it usable, both asserted by `tests/e2e/test_workflows.py`:

- **No secrets.** CI must stay free and fast so it can gate every PR. The paid
  Claude-invoking workflows are separate; never gate a PR on them.
- **`uv sync --locked` then `uv run --no-sync`, never `--frozen`.** `--frozen` reads
  as the strict option and is not: it installs a stale lock without complaint, so a
  dependency added to `pyproject.toml` without a re-lock passes CI and breaks
  everyone else. `--locked` fails the run instead. `--frozen` also does not keep uv
  off the configured package index, because building this project resolves
  `build-system.requires` (hatchling), which no lockfile covers — the thing that
  actually stops a proxy registry from rewriting source URLs is the
  `[[tool.uv.index]]` pin in `pyproject.toml`, not a flag on the run command. A
  `git diff --exit-code uv.lock` step catches a regression in any of this.

The suite must stay hermetic: no network, no ambient config. `tests/conftest.py` sets
`GIT_AUTHOR_*`/`GIT_COMMITTER_*` because the scaffolding tests end in a real `git
commit`, which aborts on a runner with no global identity. If a new test needs
credentials or a live service, it does not belong in this suite.

## What a Scaffolded `settings.json` Can and Can't Grant

Measured against real sessions, not read from documentation (issues #49, #7):

- **`permissions.allow` in a repo's `.claude/settings.json` is ignored in an
  untrusted workspace**, with the session printing `Ignoring N permissions.allow
  entries ... this workspace has not been trusted`. Every GitHub Actions checkout
  is untrusted, so an allow-list seeded there does nothing on a runner and starts
  working only once a developer accepts a trust dialog locally. Don't seed tool
  grants that way. `--allowedTools` (via `claude_args`, and `ALLOWED_TOOLS` in
  `server.py`) is what actually grants, in both modes; `tests/e2e/test_workflows.py`
  holds the two in step.
- **Hooks from an untrusted workspace's `settings.json` do fire.** Only the
  permissions entries are dropped, so the seeded logging and guard layer works on
  a runner. Worth knowing before anyone "fixes" it.
- **Writes anywhere under `.claude/` are refused for every tool**, including a
  Bash redirect, and nothing relaxes it: not a repo allow-list, not the operator's
  `--settings`, not a trusted workspace, not `acceptEdits`, not `dontAsk`. Only
  `bypassPermissions` gets through, and that hands a session the ability to rewrite
  its own operating rules. So genesis seeds a *gate*, not a grant:
  `templates/scripts/claude-dir-guard.sh` intercepts the write and tells the agent
  to post the exact edit on the task issue under `needs:human` instead of stalling
  on a bare permission string.
- **`PreToolUse` runs before the permission decision**, which is the only reason
  that gate can say anything at all.

A corollary that generalises past permissions: **any behavioural rule seeded into a
workflow prompt silently does not apply under `genesis serve`**, because local mode
disables every `genesis-*` workflow. The mode-independent carriers are the repo's
`CLAUDE.md` and — for genesis to seed, not for the dev system to edit —
`.claude/agents/*.md`. Put rules there.

## GitHub App Token Input

Every token step uses `client-id: ${{ secrets.GENESIS_APP_ID }}`, which reads odd on
purpose. `app-id` is deprecated — every run printed `Input 'app-id' has been deprecated
with message: Use 'client-id' instead` — and a deprecated input is one that eventually
stops existing, which this loop can't ask a human to fix on its own auth path.

The **secret is unchanged and still holds the numeric App ID.** No adopter action, no
new secret, and no breakage on repos scaffolded before the rename. `create-github-app-token`
resolves `core.getInput("client-id") || core.getInput("app-id")` into one `appId` passed
to `createAppAuth`, so the value's journey is identical; only the input it arrives
through differs, and GitHub accepts either an App ID or a Client ID there. Renaming the
secret too would have cost every existing dev repo a manual step for nothing.

`tests/e2e/test_workflows.py` fails any template that goes back to `app-id`.

## Self-Improvement

This project opts in to self-improvement. Update this CLAUDE.md and project workflows as the design evolves. Keep `docs/` as the living design documents.
