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

### Seed Agents

`SEED_AGENTS` in `src/genesis/scaffold.py` — three, and they're the loop rather
than the project's architecture:

- **orchestrator** — assesses state, plans, dispatches one unit of work per run
- **human-interaction** — all comms with the user (onboarding, escalations, reports)
- **evolver** — evolves the dev system itself, and escalates framework-level findings back here

Genesis seeds no worker roles, no coordinator, no project-manager or health
agent. Those are the dev system's architecture to discover, and its evolver
introduces them from its own run history (issue #30). This list used to name
three roles genesis has never shipped, and a scaffolded repo's CLAUDE.md
inherited them — a fresh system reading its own instructions was told it had a
project manager and a health agent when it had neither. The seeded three are
justified by being running code; the rest was a forecast.

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

### The branch ruleset

`ci.yml` is a **required status check on `main`**, enforced by the repository
ruleset "main: CI must pass" (id `21219925`). Before it, the guards in this suite
could be merged past red — a job that exists and creates the impression of a gate
without being one.

What it requires, and the two settings that are load-bearing:

- **Context `test`**, which is the *job* name in `ci.yml`, not the workflow name.
  There are two check runs on a commit here — `test` from `ci.yml` and `merge`
  from `genesis-merge.yml` via `workflow_run` — and requiring `merge` would
  deadlock every pull request, since it never runs on `pull_request` and would
  have to pass before the merge that produces it.
- **`strict_required_status_checks_policy: false`.** Requiring branches to be up
  to date would mean every merge invalidates every other open pull request, and
  nothing in this system rebases, so the auto-merge loop would stall on a queue
  only a human could unstick.
- **No `pull_request` rule**, so no approval requirement — a bot pull request has
  no reviewer, and requiring one deadlocks the loop.
- The Genesis App has **no bypass**, on purpose. It only merges pull requests
  where every check concluded `SUCCESS`, so it satisfies the rule on its own;
  a bypass would let auto-merge land red work.

The ruleset targets `~DEFAULT_BRANCH` only, so agent pushes to feature branches
are unaffected.

`tests/e2e/test_workflows.py` holds up this repo's half of the coupling: the job
must stay named `test`, must not carry a `name:` override that changes the check
run's name, and must stay the only job — a second one would run without being
required, which gates nothing. Read what's actually enforced with:

```
gh api repos/Sayfan-AI/genesis/rules/branches/main \
  --jq '.[] | select(.type=="required_status_checks")
        | .parameters.required_status_checks[] | .context'
```

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

## The Drift Between Genesis and Its Templates

The most productive bug in this repo's history, and it wore five hats: issues #4,
#11, #14, #15 and #22 were all *something got fixed in `.github/workflows/` and
never reached `templates/workflows/`*. `permission-actions: read` went that way.
So did the concurrency group and the `checkout` pin. Nobody was careless — the two
directories have no relationship a reader or a reviewer can see, Dependabot only
scans one of them, and the copy across is remembered or it isn't.

`tests/e2e/test_workflows.py` now checks the relationship instead of trusting it.
For any workflow name present in **both** directories, the `permission-*` set must
match exactly, and either both declare a concurrency group or neither does. Group
*names* may differ; whether a workflow serializes at all may not. Genesis-only
workflows (`ci.yml`) and template-only ones (the orchestrator) aren't paired and
are unaffected.

That guard caught a live instance on its first run: the seeded `genesis-evolver.yml`
had no concurrency group, while genesis's own got one after two 40-turn agents
raced on genesis issue #37. Issues #11 and #22 named the orchestrator and events
workflows, so those got fixed and the evolver template didn't — every dev system
seeded up to that point still had the race.

**When you fix something in genesis's own workflows, the question isn't whether to
port it — it's whether there's a reason not to.**

**And it runs backwards too, past workflows.** Issue #72: `templates/gitignore`
got the `.genesis/.*` rule for issue #40, and genesis's own `.gitignore` — which
needs it, because `genesis serve` runs against this repo and writes the same
runtime state here — never got it. Same class, opposite direction, different pair
of files, so neither the workflow guard nor the thorough `tests/e2e/test_gitignore.py`
suite asked the question: every case in that file interrogated a repo the scaffold
had just produced. `tests/e2e/test_gitignore.py` now also asks it of genesis's own
tree, and pins the two copies of the rule to the same `GITIGNORE_PATTERN`.

The generalisation worth carrying: **wherever genesis seeds something it also uses
itself, that's a pair, and a pair drifts unless something checks it.** These are
invisible to CI by construction — a fresh checkout has no runtime state, and a
template is never executed in the repo that stores it — so `main` stays green
across the whole drift. The guard has to be a test that names both halves.

### The pairs, and where each is checked

Genesis runs the control plane it ships, against its own repo, so several things it
seeds are also things it depends on. Each of those is a pair, and every pair that
went unchecked has drifted at least once:

| Pair | Guard |
| --- | --- |
| `.github/workflows/` and `templates/workflows/` | `tests/e2e/test_workflows.py` |
| `.gitignore` and `templates/gitignore` | `tests/e2e/test_gitignore.py` |
| `.genesis/scripts/` guards and `templates/scripts/` | `tests/e2e/test_seeded_pairs.py` |

The last one is parameterized over `SEEDED_GUARDS` rather than written per guard,
and it checks the list itself against what `templates/settings.json` declares - so
a guard added to the template and forgotten in the list fails rather than sitting
unpaired. That's the shape to copy for the next pair, because writing one bespoke
test per instance is what let five issues be filed for one bug.

## Genesis Merges Its Own Bot Pull Requests

`.github/workflows/genesis-merge.yml` is genesis's own copy of the template it
seeds, and it lands `[bot]`-authored pull requests once `ci.yml` is green
(issue #39). Before it, genesis was the only repo in the family that couldn't
self-advance past a pull request, and every framework fix — the turn-budget floor,
the concurrency guard, `ci.yml` itself — sat open until a human noticed.

**A human's pull request is still a human's decision.** The eligibility predicate
requires `endswith("[bot]")` on the author, and that's what makes this acceptable
at all: a dev repo auto-merging its own work is contained, while genesis
auto-merging a change to `templates/` propagates to every repo it seeds
afterwards. Bot work behind a green gate is bounded; a person's change to the
templates isn't, and doesn't qualify.

It's a copy, not a symlink, and differs from the template in exactly two places,
both commented in the file: it dispatches `genesis-evolver.yml` rather than an
orchestrator genesis doesn't have, and it says so. `tests/e2e/test_workflows.py`
holds the merge *predicate* identical across the two while letting the wiring
differ, and separately asserts that every `gh workflow run` target is a workflow
that exists beside it — copying the template's dispatch across verbatim would have
landed the work and then silently failed to wake anything.

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

## Genesis Also Has A `.claude/` Directory

Everything under *What a Scaffolded `settings.json` Can and Can't Grant* describes
what genesis **seeds**. It's true of this repo too, and for a long time nothing
here said so (issue #74). Genesis had the gate script in `templates/` and nowhere
else, no `hooks` key at all in its own `.claude/settings.json`, and
`.claude/agents/evolver.md` telling its own evolver — under **What You Can
Modify** — that it could edit `.claude/agents/`. It can't. Re-measured on a
trusted local workspace, the most permissive case there is: `Write
.claude/.evolver-write-probe` → refused.

That combination is worse than the one a seeded dev system gets, because here the
false promise sits in the agent's own definition. A run that acts on its charter
spends turns on a write that can never land and leaves no diagnosis behind —
`error_max_turns`, the worst failure shape in this system, and the one
`genesis-evolver.yml` has already died of twice.

So genesis carries the gate at the same repo-relative path a scaffolded repo uses,
`.genesis/scripts/claude-dir-guard.sh`, byte-identical to the template's. Identical
is what makes the template copy's unit tests cover this one, and the shared path is
what lets the hook declaration be compared as text.
`tests/e2e/test_claude_gate.py` pins both halves: the two scripts identical, both
`settings.json` files declaring the hook at that path, and the charter's **What You
Can Modify** section naming no path under `.claude/`.

**Arming it took a human once, and that's not a wart to be automated away.** The
hook declaration is itself a write under `.claude/`, so the gate can't arm itself
and the charter can't correct itself — genesis had to name both edits on the issue
and wait. An installer script a human runs would work, because the harness reads
the Bash command string and not what a child process does, and that's exactly the
reason there isn't one: a script that writes `.claude/` on request is a route
*around* the gate the moment an agent runs it, which is the failure
`host-guard.sh`'s own notes warn about. The only lever that genuinely works,
`bypassPermissions`, hands a session the ability to rewrite its own operating
rules. Two rare pastes beat a self-write tool.

**What this means for genesis's evolver in practice:** it evolves the framework,
not its own front matter. A prose rule goes in *this* file, which reaches sessions
in both execution modes. An edit under `.claude/` gets proposed on the task issue
as the exact file content — not a description of it — labeled `needs:human`. That's
the same protocol the gate's message names, so the charter and the hook that stops
it tell one story.

**A third half was missing, found by following that instruction:** the
`needs:human` label didn't exist in this repo, so `gh issue edit --add-label
needs:human` answered `'needs:human' not found` and the escape hatch dead-ended one
step from working. A dev repo never hits this because `templates/scripts/escalate.sh`
creates the label on demand, and genesis has no `escalate.sh`. The label now exists
here (`B60205`, "Waiting on a person", matching what `escalate.sh` creates). No test
pins it — it's repo state on GitHub, and this suite stays hermetic — so if it's ever
deleted, recreate it rather than working around it. The general shape is worth
noting: **porting a mechanism means porting everything it depends on, and a label is
a dependency.** Two of the three halves here were files, which is why the third went
unnoticed.

## Self-Improvement

This project opts in to self-improvement. Update this CLAUDE.md and project workflows as the design evolves. Keep `docs/` as the living design documents.
