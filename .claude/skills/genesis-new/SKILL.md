---
name: genesis-new
description: Bootstrap a new autonomous dev system from a goal. Use when the user wants to create a new genesis project.
argument-hint: [goal]
---

# Genesis: Bootstrap a New Dev System

You are helping the user bootstrap a new autonomous, self-improving AI dev system using genesis.

## Step 1: Understand the Goal

If `$ARGUMENTS` is provided, use it as the goal. Otherwise, ask:

> What is the goal for this dev system? Describe what you want accomplished.

## Step 2: Determine Topology

Ask the user:

> How should this dev system be set up?
>
> 1. **New repo** — create a fresh repo with the dev system embedded (default for new projects)
> 2. **Existing repo** — embed the dev system in an existing repo (default for single-repo goals)
> 3. **Multi-repo** — create a separate dev repo that manages work across multiple target repos (default for multi-repo goals)

If the user's goal clearly implies a topology, suggest it. For example:
- "Scan all my repos" → multi-repo
- "Finish blog2video" → existing repo
- "Build a CLI tool" → new repo

## Step 3: Gather Details

Based on topology:

**New repo:**
- Suggest a project name derived from the goal. Ask user to confirm or change.
- Ask: should this be under a GitHub org? Which one? Or personal?

**Existing repo:**
- Ask for the repo path (local) or GitHub URL
- Verify it exists and is a git repo

**Multi-repo:**
- Ask for the list of target repos (paths or GitHub URLs)
- Ask for a project name for the dev repo

## Step 4: Scaffold

Run the appropriate scaffold command:

**New repo:**
```bash
cd /Users/gigi/git/genesis && uv run python -c "
from pathlib import Path
from genesis.scaffold import scaffold_new_repo
scaffold_new_repo(Path('<repo_path>'), '<goal>', '<project_name>')
"
```

**Existing repo:**
```bash
cd /Users/gigi/git/genesis && uv run python -c "
from pathlib import Path
from genesis.scaffold import scaffold_existing_repo
scaffold_existing_repo(Path('<repo_path>'), '<goal>', '<project_name>')
"
```

**Multi-repo:**
```bash
cd /Users/gigi/git/genesis && uv run python -c "
from pathlib import Path
from genesis.scaffold import scaffold_external_dev_repo
scaffold_external_dev_repo(Path('<dev_repo_path>'), [<target_repos>], '<goal>', '<project_name>')
"
```

## Step 5: GitHub Setup

Ask the user:

> Do you want to push this to GitHub and create the onboarding issue?
>
> This will:
> - Create a GitHub repo (private by default)
> - Push the scaffold
> - Open issue #1 (onboarding) with your goal

If yes, run:

```bash
cd /Users/gigi/git/genesis && uv run python -c "
from pathlib import Path
from genesis.github import publish_to_github
url = publish_to_github(Path('<repo_path>'), '<project_name>', '<goal>', org='<org_or_none>', private=True)
print(url)
"
```

For existing repos that are already on GitHub, skip repo creation and just open the issue:

```bash
cd /Users/gigi/git/genesis && uv run python -c "
from pathlib import Path
from genesis.github import open_onboarding_issue
open_onboarding_issue(Path('<repo_path>'))
"
```

## Step 6: Summary

Tell the user what was created:

- Local repo path
- GitHub URL (if published)
- Issue #1 URL (if created)
- What happens next: the orchestrator workflows will trigger, the onboarding agent will start refining the goal

Remind them:
- Set `ANTHROPIC_API_KEY` as a GitHub Actions secret for the orchestrator to work
- Configure `.genesis/config.toml` with Loki credentials for observability
- The dev system will communicate via A2H — configure the gateway if they want Slack/email notifications
