---
name: genesis-e2e
description: Run the manual end-to-end test for genesis — bootstrap a tic-tac-toe dev repo, wait for the dev system to ship, drive the deployed page with Playwright, then tear the repo down. Use when the user wants to validate the full genesis loop on real GitHub.
argument-hint: [bootstrap | verify | cleanup | full]
---

# Genesis: Manual End-to-End Test

You are running the manual e2e test for genesis. It exercises the full loop: scaffold → push to GitHub → orchestrator runs on Actions → dev system ships a tic-tac-toe page to GitHub Pages → Playwright plays a game → repo is torn down.

The driver is `scripts/manual_e2e.py` at the genesis repo root. State persists in `scripts/.manual_e2e_state.json` so subcommands chain without arguments. The target repo name is the fixed `Sayfan-AI/genesis-e2e-tictactoe`; any leftover instance is deleted by `bootstrap` before scaffolding a fresh one.

## Step 1: Parse the user's intent

`$ARGUMENTS` (if given) selects the phase:

- `bootstrap` — create the repo and open issue #1
- `serve` — run `genesis serve` from the local scaffold dir (local control plane mode). Blocks in the foreground until Ctrl+C. Use a second terminal for `verify`/`enhance`.
- `verify` — wait for the Pages deployment and drive Playwright
- `enhance` — file a follow-up issue asking the dev system to add a human-vs-computer mode. Useful after a passing `verify` if you want to keep iterating on the same repo (skip `cleanup`).
- `cleanup` — delete the GitHub repo
- `full` — run bootstrap → verify → cleanup in sequence (long-running; the `verify` wait may need raising). GHA mode only; skips `enhance` and `serve`.

## Two modes — pick one after `bootstrap`

| Mode | How the orchestrator runs | Secret setup needed |
|---|---|---|
| GHA (default) | GitHub Actions workflows on the test repo | Yes — seed `GENESIS_APP_ID`, `GENESIS_APP_PRIVATE_KEY`, `ANTHROPIC_API_KEY` on the test repo before triggering, then `gh workflow run genesis-orchestrator.yml --repo <r>` to kick off |
| Local | `genesis serve` polls events and launches `claude -p` locally; the orchestrator agent acts as the local user | No — `genesis serve` disables GHA workflows for the duration. Just `direnv exec . uv run python scripts/manual_e2e.py serve` after bootstrap |

Local mode is closer to what an OSS adopter sees first time — no App, no secret seeding, just `genesis serve` and Claude Code. GHA mode tests the autonomous-on-Actions path.

If `$ARGUMENTS` is empty, ask the user which phase. Default suggestion: `bootstrap`, since `verify` and `cleanup` only make sense after a prior `bootstrap`.

## Step 2: Preflight (run before any phase)

Check that the prerequisites are satisfied. **Do not paper over failures by assuming a default — surface the missing piece and let the user fix it.**

```bash
cd /Users/gigi/git/genesis
test -f .envrc && echo ".envrc present"
direnv status | grep -E "Loaded|allowed" || echo "direnv NOT loaded — run: direnv allow"
gh auth status --active 2>&1 | head -5
# delete_repo scope is required for cleanup and for bootstrap's pre-cleanup of a leftover repo:
gh auth status -t 2>&1 | grep -A1 "the-gigi$" | grep -q delete_repo \
  || echo "the-gigi token missing delete_repo scope — run: gh auth refresh -h github.com -u the-gigi -s delete_repo"
```

If `.envrc` isn't loaded, `gh` will run as the active account (likely `the-gigi-pplx`), which is wrong for this OSS project. Stop and tell the user to run `direnv allow`.

If the `delete_repo` scope is missing, tell the user to run `gh auth refresh -h github.com -u the-gigi -s delete_repo` before continuing.

## Step 3: Run the requested phase

### `bootstrap`

```bash
cd /Users/gigi/git/genesis && uv run python scripts/manual_e2e.py bootstrap
```

This will (in order):
1. Delete `Sayfan-AI/genesis-e2e-tictactoe` if it exists from a prior run
2. Remove `/tmp/genesis-manual-e2e/genesis-e2e-tictactoe` if present
3. Scaffold a fresh new-repo dev system with the tic-tac-toe goal (selector contract is in the goal text — the dev system has to honor `[data-cell]`, `#status`, `#reset`)
4. Create the public GitHub repo, push, open issue #1
5. Save state to `scripts/.manual_e2e_state.json`

Report back to the user: repo URL, issue URL, expected Pages URL. Tell them the orchestrator workflows will start firing — they can watch progress at the repo's Actions tab, issues, and PRs.

### `verify`

```bash
cd /Users/gigi/git/genesis && uv run python scripts/manual_e2e.py verify
```

Default wait is 600s (10 min) for the Pages URL to return 200. If the user knows the dev system is still mid-flight, raise it: `--wait 1800` for 30 min, `--wait 3600` for an hour. Use `--headed` to watch the browser drive the game.

The Playwright script plays a deterministic game: X clicks cell 0, O clicks 3, X clicks 1, O clicks 4, X clicks 2 → X wins the top row. It asserts cell text becomes `X`/`O`, `#status` contains "win", and `#reset` clears the board.

**A `verify` failure has two possible meanings:**
- The dev system produced a broken or undeployed app (loop-level failure — the real signal we wanted).
- The dev system shipped a working app but ignored the selector contract (autonomy worked; spec discipline didn't). The error message says which selector is missing — relay that verbatim.

### `cleanup`

```bash
cd /Users/gigi/git/genesis && uv run python scripts/manual_e2e.py cleanup
```

Deletes the GitHub repo and clears state. Fails clearly if `delete_repo` scope is missing.

### `full`

Run `bootstrap`, then wait for the dev system to finish (this can take a long time — many minutes to hours depending on what the orchestrator does and how many cycles it takes). Don't poll `verify` aggressively — `verify` itself has a built-in wait loop. A reasonable pattern: kick off bootstrap, then run `verify --wait 3600`. Run `cleanup` only after the user has reviewed the result.

## Step 4: Report

After each phase, summarize:

- **bootstrap:** repo URL, issue URL, Pages URL (note it won't resolve yet)
- **verify:** PASS/FAIL with the detail line from the script verbatim
- **cleanup:** confirmation that the repo was deleted

Do not invent results — read them from the script's stdout/exit code.
