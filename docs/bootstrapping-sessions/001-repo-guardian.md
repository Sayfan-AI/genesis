# Bootstrapping Session: repo-guardian

**Date:** 2026-03-26/27
**Goal:** Scan all GitHub repos under the-gigi and Sayfan-AI. Identify security vulnerabilities and outdated dependencies. Merge existing Dependabot PRs that have passing CI. For repos without Dependabot, enable it. For issues that can't be auto-resolved, create a tracking issue. Build a dashboard showing the status of all repos.
**Topology:** Multi-repo (separate dev repo managing work across target repos)
**Dev repo:** [Sayfan-AI/repo-guardian](https://github.com/Sayfan-AI/repo-guardian)

## What happened

### 1. Scaffold

Used `scaffold_external_dev_repo()` with target repos `the-gigi/*` and `Sayfan-AI/*`. The dev system discovers individual repos dynamically at runtime via the GitHub API rather than listing them statically.

### 2. GitHub repo creation

Created `Sayfan-AI/repo-guardian` (private). Required using the `the-gigi` GitHub account since `the-gigi-pplx` (active account) doesn't have permission to create repos under Sayfan-AI.

**Multi-account trick:**
```bash
GITHUB_TOKEN=$(gh auth token --user the-gigi) gh <command>
```
This runs a `gh` command as a different user without switching the active account.

### 3. Workflow fixes discovered during first run

**`id-token: write` permission missing.** The `claude-code-action` needs OIDC token exchange, which requires `id-token: write` in the workflow permissions block. Without it, the action fails with "Could not fetch an OIDC token."

**`push` trigger doesn't work with claude-code-action.** Initially added `push: branches: [main]` to the events workflow so pushes would trigger the orchestrator. However, `claude-code-action` doesn't support the `push` event type — it only supports issue/PR/comment events and `workflow_dispatch`/`schedule`. Removed the push trigger. Pushes are picked up by the 10-minute cron instead.

**Cron too infrequent.** Default was every 6 hours. Changed to every 10 minutes (`*/10 * * * *`) for faster feedback loops. Added "don't re-notify" guideline to the orchestrator so it doesn't spam the user on every cycle.

**Workflow registration timing.** Workflows pushed in the initial commit aren't always immediately recognized by GitHub Actions. A second push or manual `workflow_dispatch` may be needed to kick-start them.

### 4. GitHub App for bot identity

Created a GitHub App (`genesis-dev-bot`) so agent activity is distinguishable from human activity. All issues/PRs/comments created by the dev system show as `genesis-dev-bot[bot]`.

**Why:** Without a bot identity, agent-created issues and human-created issues are indistinguishable. The bot identity also enables email notification filtering — the human sets GitHub notifications to "Participating only" and only gets emails when explicitly @mentioned or assigned.

**Name collision:** GitHub App names are globally unique across the entire GitHub namespace (users, orgs, and apps). `genesis-bot` was taken by an existing GitHub user account (unrelated, created 2018). Settled on `genesis-dev-bot`.

**Setup steps:**
1. Create app at `github.com/organizations/<org>/settings/apps/new`
   - Permissions: Contents (R/W), Issues (R/W), Pull requests (R/W), Metadata (Read)
   - Webhook: disabled (not receiving events, only acting)
   - Visibility: "Any account" (so it works across multiple orgs)
2. Generate private key (downloads `.pem` file)
3. Install on target orgs/accounts (Sayfan-AI, the-gigi)
4. Store `GENESIS_APP_ID` and `GENESIS_APP_PRIVATE_KEY` as GitHub Actions secrets
5. Update workflows to use `actions/create-github-app-token@v1` and pass the token to `claude-code-action` via `github_token`
6. Update bot loop filter from `github-actions[bot]` to `genesis-dev-bot[bot]`

**Browser automation:** Used Vivaldi + AppleScript (`osascript`) to drive the GitHub App creation UI. Requires enabling "Allow JavaScript from Apple Events" in Vivaldi Settings > Privacy > Apple Events. Key learnings:
- React-style forms need `Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set` to trigger state updates
- Permission dropdowns are custom components — need to click the button, then click the `menuitemradio` option
- GitHub's CSRF tokens go stale if the page has been open too long — reload before submitting

### 5. Template fixes applied back to genesis

All workflow and agent fixes were applied to both the repo-guardian instance and the genesis templates:
- `id-token: write` permission
- `push` trigger on events workflow
- 10-minute cron schedule
- No-renotify guideline in orchestrator agent

## Commits in repo-guardian

1. `Initial scaffold by genesis`
2. `add id-token:write permission for claude-code-action OIDC`
3. `add push trigger to events workflow`
4. `10min cron schedule, no-renotify guideline`
5. `use genesis-bot app token for agent identity`
6. `switch to genesis-dev-bot app identity`

## Lessons for future bootstraps

- Always use `id-token: write` in workflow permissions
- Include `push` trigger in events workflow from the start
- 10-minute cron is a good default for active development
- Set up the GitHub App early — it's needed for clean notification management
- The `GITHUB_TOKEN` multi-account trick is essential when the active `gh` account differs from the org owner
- GitHub App names are globally unique — check availability before committing to a name
- Browser automation via AppleScript/Vivaldi works but is fragile — consider building a CLI tool for GitHub App setup if this becomes a repeated task
