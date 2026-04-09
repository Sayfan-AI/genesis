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
