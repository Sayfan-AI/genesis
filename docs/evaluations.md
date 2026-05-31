# Technology Evaluations

Evaluations of technologies, features, and approaches considered for genesis and genesis dev systems. Each entry captures the decision and rationale.

## Claude Code Agent Teams

**Evaluated:** 2026-04-04
**Source:** https://code.claude.com/docs/en/agent-teams
**Status:** experimental (disabled by default, `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`)

### What it is

Agent teams coordinate multiple Claude Code instances working together. One session acts as the team lead, spawning teammates that work independently in their own context windows. Teammates communicate directly with each other via a mailbox system and coordinate through a shared task list. The user can interact with any teammate directly.

Key features:
- Shared task list with self-claiming and dependency tracking
- Inter-agent messaging (direct + broadcast)
- Split-pane or in-process display modes
- Quality gate hooks (`TeammateIdle`, `TaskCreated`, `TaskCompleted`)
- Subagent definitions can be reused as teammate roles
- Plan approval mode (teammates plan first, lead approves before implementation)

### Decision: not a fit for genesis coordination layer

Agent teams solve a different problem than genesis needs. They're designed for **synchronous, interactive, multi-session collaboration** driven by a human at the terminal. Genesis dev systems are **autonomous and async** by design.

### Why it doesn't fit

**Ephemeral runners vs. persistent teams.** Agent team state lives locally at `~/.claude/teams/{name}/config.json`. GitHub Actions runners are ephemeral — when the runner dies, the team is gone. There's no persistence across workflow runs, and even within CC, `/resume` doesn't restore teammates.

**Genesis already has orchestration.** The orchestrator agent assesses project state from GitHub issues and dispatches sub-agents via the Claude Agent SDK. Agent teams would be a parallel coordination system solving the same problem differently and less durably.

**GitHub issues vs. local task list.** Genesis uses issues as the shared language between agents and humans — persistent, searchable, visible to both. Agent teams use an in-memory task list that's invisible to humans and doesn't survive the session.

**Agent SDK vs. CC CLI.** Genesis workflows launch Claude Agent SDK sessions, not `claude` CLI sessions. Agent teams are a CC CLI feature. The two execution models don't mix cleanly.

**Token cost.** Agent teams use significantly more tokens than a single session with subagents. Genesis already lists cost management as an open question.

### Where agent teams could be useful

**Within a single orchestrator run** — if the orchestrator needs to parallelize complex subtasks (e.g., research 5 repos simultaneously, debug from multiple angles). But subagents already cover this with less overhead and lower token cost.

**During ad-hoc human CC sessions** — when the human opens a CC session in the dev repo for interactive exploration. But that's the human's choice, not something genesis should seed or prescribe.

### What's worth borrowing

The **quality gate hooks pattern** (`TeammateIdle`, `TaskCreated`, `TaskCompleted`) — exit code 2 to reject and send feedback. Genesis already uses CC hooks for logging; similar hook-based quality gates could be useful for the health agent. These hooks work with subagents too, so agent teams aren't required to use them.

### Multi-agent coordination spectrum

For context, here's the spectrum of approaches for multiple agents working on the same project, from tightest to loosest coupling:

1. **One session + subagents** — subagents each have their own context window but only report back to the main agent. User interacts with the main agent only. Lowest overhead, best for focused parallel tasks where only the result matters.

2. **Agent teams** — lead spawns teammates that communicate directly with each other and coordinate via a shared task list. User can interact with any teammate in their own session. Higher token cost, best for work requiring discussion and collaboration between agents. Requires live sessions.

3. **Independent CC sessions** — user starts completely separate Claude Code sessions. No built-in coordination — each session is unaware of the others. Coordination happens out-of-band: through committed files, GitHub issues, or an external system the user introduces.

Genesis dev systems operate at level 3 by architecture: each GitHub Actions trigger spawns an independent session, and coordination happens through GitHub issues and committed state. This is the right choice for autonomous async systems. Level 1 (subagents) is used within a single orchestrator run for parallelism. Level 2 (agent teams) occupies a middle ground that doesn't align with genesis's execution model.

## Anthropic Harness Design Patterns

**Evaluated:** 2026-04-17
**Source:** https://www.anthropic.com/engineering/harness-design-long-running-apps

### What it is

Anthropic's engineering team published a detailed account of building multi-agent harnesses for long-running application development. The article covers a three-agent architecture (planner, generator, evaluator), sprint contracts, evaluator calibration, context management strategies, and harness simplification principles.

### Patterns worth considering

**Generator-evaluator separation.** Models exhibit strong positive bias when grading their own output — "agents tend to respond by confidently praising the work—even when quality is obviously mediocre." Dedicating a separate agent to evaluation makes skepticism tractable. Genesis's health agent fills this role, but the pattern should be enforced more explicitly: workers never self-approve, the health agent (or orchestrator) always reviews.

**Sprint contracts.** Before implementation, generator and evaluator agree on specific success criteria. Example: 27 testable criteria for a single sprint's work. This prevents scope creep and makes "done" unambiguous. Genesis should adopt this: the orchestrator creates issues with explicit done criteria, and workers are measured against those criteria — not their own assessment of completeness.

**Evaluator calibration.** Out-of-the-box evaluators "talk themselves into deciding issues aren't a big deal and approve work anyway." Effective evaluation requires iterative prompt tuning: review evaluator logs for judgment divergence, update QA prompt, repeat. The evolver agent should treat health agent prompt quality as a first-class concern and calibrate it against observed false-positive approvals.

**Interactive evaluation.** Using Playwright to test running apps catches issues that static code review misses (broken UI flows, API errors under real conditions). Genesis dev systems building web apps should have the health agent verify against the running application, not just review code.

**Harness simplification principle.** "Every component in a harness encodes an assumption about what the model can't do on its own, and those assumptions are worth stress testing because they can quickly go stale as models improve." The evolver agent should periodically stress-test each component of the dev system: remove one piece, evaluate impact, only retain what's load-bearing. Example from the article: sprint decomposition was dropped for Opus 4.6 because the model could handle 2+ hour continuous building.

### Patterns already aligned with genesis

**Context degradation mitigation.** Genesis already spawns fresh sessions per GHA trigger, avoiding the context rot problem. The article confirms this is the right approach — their Opus 4.5 harness required explicit context resets, while 4.6 handled it natively via SDK compaction.

**File-based handoffs.** Agents communicate through committed artifacts rather than shared context. Genesis uses GitHub issues and committed files for the same purpose.

**Continuous harness optimization.** The harness itself evolves as models improve. This is the evolver agent's core job.

### Cost reference points

- Solo agent (no harness): $9, 20 min → broken output
- Full harness (Opus 4.5): $200, 6 hours → functional full-stack app
- Simplified harness (Opus 4.6): $125, 3h50m → functional app with less orchestration overhead
- Planner phase is cheap (~$0.50, 5 min); evaluation is cheap (~$3-7); generation dominates cost

## GSD (Get Shit Done) Framework

**Evaluated:** 2026-04-17
**Source:** https://github.com/gsd-build/get-shit-done (v1), https://github.com/gsd-build/gsd-2 (v2)

### What it is

GSD is a spec-driven development framework for AI coding agents (~48K GitHub stars). Two versions: v1 is prompt-only (markdown skills injected as Claude Code slash commands), v2 is a TypeScript CLI on the Pi SDK with programmatic control over agent sessions. Supports 12 runtimes (Claude Code, Gemini CLI, Cursor, etc.).

GSD is human-interactive — the user drives phases. It solves context rot for long coding sessions by externalizing all state to disk and spawning fresh context windows per task.

### Patterns to adopt

**Thin orchestrator.** GSD's orchestrator stays at 10-15% context usage, passes file paths (not file contents) to sub-agents. Each worker gets a fresh context window for deep work. Genesis's orchestrator should follow the same discipline: read issue state, decide what to dispatch, dispatch it — never do heavy implementation work itself. If the orchestrator's context fills up, something is wrong.

**Structured work hierarchy.** GSD decomposes work into milestone → slice → task, where each task is sized to fit one context window. Genesis uses GitHub milestones and issues but doesn't enforce sizing. The orchestrator (or evolver) should watch for issues that are too large for a single agent session and break them down further.

**Plan → execute → verify loop.** Each slice goes through: plan (with research) → execute (per task, fresh context) → verify → reassess roadmap → next slice. Genesis should ensure the orchestrator follows a similar discipline. Currently the design says the orchestrator "dispatches work" but doesn't mandate a verification step before moving on.

**File-driven state machine.** GSD's `.gsd/` directory is the sole source of truth — no in-memory state survives across sessions. Auto mode reads disk state, determines next work unit, spawns fresh agent, repeats. Genesis uses GitHub issues for the same purpose, which is better for human visibility but the principle is the same: state must be external and durable.

**Crash recovery via persistent state.** If a GSD session dies, the next run reads surviving disk state and resumes. Genesis gets this for free from GitHub issues — if a GHA runner dies, the next orchestrator run sees the same issue state and picks up where things left off.

### Patterns less relevant to genesis

**Local-first state.** GSD stores everything in `.gsd/` on the local filesystem. This works for human-interactive sessions but not for ephemeral GHA runners. Genesis correctly chose GitHub issues as the durable state layer.

**Phase-gated workflow.** GSD requires explicit phase transitions (initialize → discuss → plan → execute → verify → complete). Genesis dev systems are autonomous — the orchestrator decides the phase implicitly based on project state. Forcing explicit phases would add ceremony without value in an autonomous system.

**Prompt-only control (v1).** GSD v1 injects markdown prompts and hopes the LLM follows them. This is fragile — the move to v2's programmatic control validates genesis's choice of the Claude Agent SDK for orchestration rather than relying solely on CLAUDE.md instructions.

### Key difference from genesis

GSD is a **framework for human-driven coding sessions**. Genesis is a **bootstrapper for autonomous dev systems**. GSD fights context rot within a single user's workflow; genesis avoids it by design (ephemeral sessions, external state). The patterns worth borrowing are architectural (thin orchestrator, work decomposition, verify-before-proceed), not the interaction model.

### GSD's evolution is informative

GSD v1 → v2 mirrors a tension genesis should watch: prompt-only systems (seeding good patterns in CLAUDE.md) work until they don't. When they fail, you need programmatic control (Agent SDK, hooks, deterministic scripts). Genesis already leans toward the programmatic side, which is the right call for autonomous operation.

## Harness Engineering as a Discipline

**Evaluated:** 2026-04-30
**Sources:**
- https://openai.com/index/harness-engineering/ (OpenAI, Feb 2026 — foundational)
- https://ai.gopubby.com/harness-engineering-what-every-ai-engineer-needs-to-know-in-2026-0ab649e5686a

### What it is

Within ~90 days of OpenAI's February 2026 announcement, "harness engineering" formalized as a named discipline: engineers stop writing application code directly and instead design the environment agents work inside — constraints, feedback loops, documentation structure, dependency rules, evaluation. Anthropic, ThoughtWorks, Red Hat, and Hugging Face all published frameworks within the same window; Hugging Face called it "the most important discipline of 2026." A reported milestone: a small team shipping ~1M lines of production code without writing any by hand.

### Core thesis

The engineer's role shifts from code author to system architect. The harness — not the model — is what makes autonomous coding reliable at scale. This generalizes the Anthropic-specific patterns above (planner/generator/evaluator, sprint contracts, evaluator calibration) into a broader claim: **every long-running agent system needs a deliberately engineered harness, and that harness is itself the artifact under continuous design.**

### Harness complexity is inversely correlated with model capability

The most actionable insight: each new model generation doesn't just raise capability ceilings — it makes parts of your existing harness obsolete. The Medium article's exact claim about Anthropic's Opus 4.7 release: "Components that were load-bearing in March became dead weight by April." A harness that was correct for Opus 4.5 is overengineered for Opus 4.7. This validates and sharpens the simplification principle from the Anthropic article.

### Implications for genesis

**For the evolver agent.** This is its central mandate, restated more strongly: the evolver isn't a janitor that occasionally tidies the dev system — it is doing harness engineering, continuously. Each model release is a trigger to re-evaluate every component. Maintain a list of harness assumptions ("we have a separate planner because the generator can't plan reliably"; "we decompose into sprints because context degrades after 2 hours") and stress-test them against the current model. Retain only what is still load-bearing.

**For onboarding.** When the dev system is bootstrapped, onboarding shouldn't pick a fixed starting harness — it should pick one calibrated to the *current* model and the project's complexity. A simple goal on Opus 4.7 may need almost no harness (single orchestrator + GitHub issues). A complex goal on a weaker model needs the full planner/generator/evaluator split. The onboarding agent should ask about (or infer) target model capability and project complexity, then seed the minimum viable harness rather than the maximal one. The evolver will add components later if reality demands it — that's cheaper than ripping out unneeded scaffolding.

**Direction of drift.** Default to *removing* harness components over time, not adding them. New components must justify themselves against current model capability; existing components must re-justify themselves on each model upgrade.

## Karpathy-Inspired Claude Code Guidelines

**Evaluated:** 2026-04-30
**Source:** https://github.com/forrestchang/andrej-karpathy-skills

### What it is

A single distilled `CLAUDE.md` file derived from Andrej Karpathy's observations on LLM coding pitfalls (silent wrong assumptions, overcomplication, bloated abstractions, side-effect edits to unrelated code). It encodes four principles meant to counteract these failure modes:

1. **Think Before Coding** — state assumptions explicitly, present tradeoffs, push back, stop when confused.
2. **Simplicity First** — minimum code that solves the problem; no speculative abstractions, no error handling for impossible scenarios.
3. **Surgical Changes** — touch only what the task requires; match existing style; clean up only your own orphans.
4. **Goal-Driven Execution** — tests-first, verifiable success criteria.

### What's worth borrowing

These principles align closely with — and sharpen — guidance already in genesis's own CLAUDE.md and seed agent prompts. The "Think Before Coding" principle (surface ambiguity rather than guess) directly addresses a failure mode autonomous dev systems are especially vulnerable to: a worker silently picking the wrong interpretation has no human in the loop to catch it. The "Surgical Changes" principle is equally important for autonomous agents committing PRs unsupervised — drift from over-eager edits compounds across iterations.

### Implications for genesis

**For onboarding.** When seeding the dev repo's `CLAUDE.md` and worker agent prompts, fold in these four principles as default behavior contracts. They're cheap to include and address well-known LLM failure modes that hurt autonomous operation more than interactive use. Consider them part of the minimum viable harness regardless of model capability.

**For the evolver.** When a worker produces work that gets rejected by the health agent, classify the failure: was it a Karpathy-style failure (silent assumption, overengineering, scope creep)? If yes, the fix is prompt-level — strengthen the relevant principle in the worker's CLAUDE.md or agent definition. Track which principles fire most often; that's a signal about where the harness is weakest.

**Caveat on freshness.** The four principles are model-agnostic and should age well, but the specific phrasing was tuned for a particular model generation. The evolver should re-evaluate phrasing on model upgrades — what reads as helpful constraint to one model can read as redundant ceremony to a more capable one.

## Six-Month Claude Code Tuning Report

**Evaluated:** 2026-05-03
**Source:** https://medium.com/data-science-collective/i-spent-6-months-tuning-claude-code-heres-the-exact-setup-that-finally-worked-b41c67628478

### What it is

A practitioner's account of iterating on a `.claude/` configuration over six months of daily Claude Code use. The published portion describes the final shape of the setup; specifics of the iteration path are behind a paywall.

### The setup

A modular `.claude/` layout where each component is short and purpose-built:

- **`CLAUDE.md`** kept deliberately small (author cites ~500 tokens) — high-signal project context, not a kitchen-sink prompt.
- **Rules folder** with path-scoped behavioral files (e.g. `langgraph.md`, `retrieval.md`, `tests.md`, `python-types.md`) that apply only when work touches the matching area, instead of one omnibus instructions file.
- **Agents folder** with narrow subagents for review/audit/eval roles (e.g. `retrieval-reviewer`, `prompt-auditor`, `eval-runner`) rather than generalist helpers.
- **Skills folder** where each skill has its own `SKILL.md`.
- **`settings.json` hooks** split between pre-tool gates (block bad calls before they happen) and post-tool formatters (normalize output after).
- **`.mcp.json`** with a small curated set of MCP servers — the framing is that each server has to "earn its place," not be added by default.

### Core thesis

Deliberate, modular configuration unlocks Claude Code's capability; sprawling prompts and unscoped rules leave most of the value on the floor. Brevity per file is the discipline — none of the components are long.

### What's worth borrowing

**Path-scoped rules over one big CLAUDE.md.** Genesis already keeps `CLAUDE.md` lean, but the path-scoped rules pattern is a natural next step for dev repos as their codebases grow. Worker agents touching `tests/` get the testing rules; workers touching the retrieval module get retrieval rules. Less context per turn, less cross-contamination of guidance.

**Pre-tool gates as a deterministic safety layer.** Aligns with genesis's "deterministic over agentic" principle: a pre-tool hook that blocks a bad command is cheaper and more reliable than relying on the agent to remember a rule. Good fit for things like "never push to main," "never edit generated files."

**Post-tool formatters as a normalization layer.** Removes a class of bikeshedding from agent output by making formatting non-negotiable and automatic. Useful seed pattern for any dev system where style consistency matters across many agent-authored PRs.

**MCP servers must earn their place.** Each MCP server expands the tool surface and the failure surface. Defaulting to "minimum viable MCP" rather than "everything that might be useful" keeps the harness tractable.

### Implications for genesis

**For seed templates.** Consider seeding dev repos with a `rules/` directory pattern (initially empty, with a short README explaining the path-scoping convention) so the evolver has an obvious place to add scoped guidance instead of growing `CLAUDE.md`. Don't pre-populate it — that's the dev system's job.

**For the evolver.** When the evolver identifies a recurring failure, the fix decision tree should now include "is this scoped to a specific path?" If yes, add a rules file rather than expanding `CLAUDE.md`. Track `CLAUDE.md` length over time as a health signal — unbounded growth is a smell.

**Caveat on the source.** The article's framing ("the exact setup that finally worked") is one practitioner's experience, not a measured study, and the iteration narrative is paywalled. Treat the patterns as plausible defaults to validate, not proven recipes. The structural ideas (path-scoped rules, pre/post hook split, curated MCP set) are the durable part; specific file lists are illustrative.

## Computer-Use Agent Infrastructure (cua)

**Evaluated:** 2026-04-30
**Source:** https://github.com/trycua/cua

### What it is

Open-source infrastructure for Computer-Use Agents: sandboxes, SDKs, and benchmarks for training and evaluating AI agents that control full desktops (macOS, Linux, Windows). Provides a reference for how to safely run agents that go beyond text/code into screen control, GUI automation, and full-OS interaction.

### Why this matters for genesis

Most genesis dev systems will be code-focused, but some goals will require agents that go beyond the GitHub/code surface — running browsers, driving GUIs, controlling local apps, automating non-CLI workflows. cua is the most credible open reference for that capability today.

### Implications for genesis

**For onboarding.** When the user's goal involves automating something outside the code/GitHub surface (e.g., "build me a system that monitors a vendor portal that has no API"), onboarding should recognize the computer-use shape of the goal and seed cua (or equivalent) into the dev system's tooling rather than trying to force a code-only solution. This is an architecture-level decision that belongs at onboarding, not later.

**For the evolver.** If a dev system repeatedly hits "we'd need to drive a browser/GUI to do this" walls, the evolver should consider introducing a cua-based worker rather than continuing to work around the limitation. Track these blockers as a signal that the harness is missing a capability tier.

**Sandboxing pattern.** Even for non-computer-use dev systems, cua's sandbox model is worth studying as a reference for isolating agents that need broader system access than a GHA runner provides — relevant if a dev system grows into local-mode operation with elevated privileges.

## Symphony (OpenAI's Codex Orchestration Spec)

**Evaluated:** 2026-04-30
**Sources:**
- https://openai.com/index/open-source-codex-orchestration-symphony/
- https://github.com/openai/symphony
- https://github.com/openai/symphony/blob/main/SPEC.md

### What it is

A language-agnostic spec (and Elixir reference implementation) for a long-running service that continuously reads issues from a tracker (Linear in v1), creates an isolated workspace per issue, and runs a coding agent session inside that workspace. Reported as "low-key engineering preview" — OpenAI does not plan to maintain it as a product, treating it as a reference for others to study, fork, or reimplement. Reported impact at OpenAI: ~500% increase in landed PRs in the first three weeks among adopting teams.

The spec explicitly notes: **"Symphony works best in codebases that have adopted harness engineering."** Orchestration only pays off if the underlying harness is sound.

### Architectural parallels with genesis

Symphony and genesis are converging on the same shape from different starting points:

| Concern | Symphony | Genesis |
|---|---|---|
| Control plane | Linear issues | GitHub issues |
| Per-task isolation | Per-issue workspace directory | Ephemeral GHA runner |
| In-repo workflow policy | `WORKFLOW.md` (YAML front matter + prompt) | `CLAUDE.md` + agent definitions |
| Trigger model | Polling daemon at fixed cadence | Event-triggered + cron GHA |
| State durability | Tracker + filesystem (no DB) | GitHub issues (no DB) |
| Ticket writes | Done by the agent, not the orchestrator | Done by the agent, not the orchestrator |

This is strong external validation that genesis's architectural bets (issues as state, agent does ticket writes, workflow policy in-repo, no DB) are the right ones — an independent OpenAI team arrived at the same answers.

### Component decomposition worth borrowing

Symphony's spec breaks the orchestrator into named components that genesis currently treats as one fuzzy "orchestrator agent":

1. **Workflow Loader** — parses `WORKFLOW.md` (front matter + prompt template)
2. **Config Layer** — typed getters with defaults + env var indirection + pre-dispatch validation
3. **Issue Tracker Client** — fetch candidates, reconcile state, normalize payloads
4. **Orchestrator** — owns poll tick, in-memory runtime state, dispatch/retry/stop decisions
5. **Workspace Manager** — issue ID → workspace path, lifecycle hooks, cleanup on terminal
6. **Agent Runner** — workspace + prompt build + agent launch + stream updates
7. **Status Surface** (optional) — operator-visible runtime status
8. **Logging** — structured logs

Genesis's orchestrator agent collapses 3, 4, 5, and 6 into a single Claude session. That's appropriate at genesis's current scale, but the Symphony decomposition is a useful reference target if a dev system outgrows the single-orchestrator model.

### Operational patterns worth adopting

**Reconciliation, not just dispatch.** The orchestrator must stop active runs when issue state changes make them ineligible (e.g., issue closed, label changed, priority dropped). Genesis's current design dispatches but doesn't explicitly reconcile.

**Bounded concurrency + retry queue with exponential backoff.** Practical operational concerns the orchestrator agent should explicitly handle, not punt to "the LLM will figure it out."

**Handoff states ≠ Done.** A successful run can terminate at a workflow-defined handoff state (e.g., `Human Review`), not just `Done`. Genesis should encode this — workers should be able to declare "ready for human" as a terminal state distinct from "complete," and the orchestrator should treat that as success, not as work-in-progress.

**Workspaces preserved across runs.** Symphony preserves per-issue workspaces across runs. Genesis's GHA model recreates them each time. This is a real capability gap — fine for stateless tasks, painful for tasks with expensive setup (large clones, build caches, model downloads). Genesis's local mode could adopt Symphony-style persistent workspaces; GHA mode would need cache-restore tricks to approximate it.

**WORKFLOW.md as a separate contract.** Symphony separates *agent-facing runtime policy* (WORKFLOW.md) from general project docs. Genesis currently bundles this into CLAUDE.md. Worth considering whether to split: a CLAUDE.md for "what this project is" and a WORKFLOW.md for "how agents should act on issues." Cleaner separation, easier for the evolver to tune workflow policy without touching project docs.

### Implications for genesis

**For onboarding.** Symphony is, in effect, what a fully-grown genesis dev system *becomes*. Onboarding can use Symphony's component breakdown as a target architecture and seed the dev system with placeholders for each role — even if early-stage they're collapsed into a single orchestrator agent. This gives the evolver clear seams to break apart along as the dev system scales.

**For the evolver.** When a dev system hits scale problems (concurrent issues, lost workspace state, retry storms, reconciliation bugs), the evolver should reach for Symphony's spec rather than reinventing — lift the relevant component (Workspace Manager, retry queue, reconciliation loop) and adapt it to the dev system's runtime.

**Validates "harness engineering as prerequisite."** Symphony's own caveat ("works best in codebases that have adopted harness engineering") reinforces the discipline section above: orchestration is the cap on the harness pyramid, not a substitute for it. Onboarding should resist the temptation to seed Symphony-style orchestration into projects that don't yet have a working harness — it'll just orchestrate broken work faster.



# TODO

Eval these. Are they useful as tools for Genesis?


https://github.com/colbymchenry/codegraph

https://github.com/Light-Heart-Labs/DreamServer

https://github.com/MinishLab/semble?utm_source=tldrdevops
