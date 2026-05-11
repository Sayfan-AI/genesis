"""Manual end-to-end test for genesis.

Bootstraps a fresh dev repo with a tic-tac-toe + GitHub Pages goal, waits for
the dev system to ship something, then exercises the deployed app with
Playwright. Not a pytest test — invoke explicitly to validate the whole loop:

    uv run python scripts/manual_e2e.py bootstrap
    # ... wait for the orchestrator to do its thing, watch issues/PRs ...
    uv run python scripts/manual_e2e.py verify
    uv run python scripts/manual_e2e.py cleanup

State is persisted to scripts/.manual_e2e_state.json so the subcommands chain
without arguments. Override with --repo owner/name if you want.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from genesis.github import create_github_repo, open_onboarding_issue, push_to_github  # noqa: E402
from genesis.scaffold import scaffold_new_repo  # noqa: E402

STATE_PATH = Path(__file__).parent / ".manual_e2e_state.json"
DEFAULT_OWNER = "Sayfan-AI"
REPO_NAME = "genesis-e2e-tictactoe"
WORKDIR = Path("/tmp/genesis-manual-e2e")

GOAL = """\
Build a two-player tic-tac-toe web app deployed to GitHub Pages.

Requirements:
- A single static page (HTML + CSS + inline or co-located JS) served from \
GitHub Pages on the `main` branch (either root or `/docs`).
- Two players alternate clicks: X goes first, then O. After each click, check \
for a win or draw and display the result.
- A "reset" control restores an empty board.

Testability contract (must be honored — an external Playwright e2e test \
depends on these selectors):
- Each cell is an interactive element (button or div) with attribute \
`data-cell="N"` where N is 0..8, ordered left-to-right then top-to-bottom.
- After a move, the cell's text content is exactly "X" or "O" (uppercase, no \
extra whitespace).
- A status element with `id="status"` displays the current game state. Its \
text contains the word "win" (case-insensitive) when a player wins, "draw" \
when the game ends in a draw, and neither otherwise.
- A reset element with `id="reset"` clears the board when clicked.

Quality: no frameworks needed, but the code should be clean and tested if \
practical. Deploy via GitHub Actions or the standard Pages-from-branch \
mechanism — whichever you prefer.
"""


@dataclass
class State:
    repo: str
    repo_url: str
    pages_url: str
    issue_url: str
    created_at: str


def save_state(state: State) -> None:
    STATE_PATH.write_text(json.dumps(asdict(state), indent=2) + "\n")


def load_state() -> State | None:
    if not STATE_PATH.exists():
        return None
    return State(**json.loads(STATE_PATH.read_text()))


def gh_user(account: str) -> str:
    """Return the login for the given gh-stored account."""
    token = subprocess.run(
        ["gh", "auth", "token", "--user", account],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    result = subprocess.run(
        ["gh", "api", "user", "--jq", ".login"],
        check=True, capture_output=True, text=True,
        env={"GH_TOKEN": token, "PATH": __import__("os").environ["PATH"]},
    )
    return result.stdout.strip()


def repo_exists(repo: str) -> bool:
    result = subprocess.run(
        ["gh", "repo", "view", repo, "--json", "name"],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    owner = args.owner
    name = args.name or REPO_NAME
    full = f"{owner}/{name}"

    if repo_exists(full):
        print(f"Found leftover repo {full} from a previous run — deleting.")
        result = subprocess.run(["gh", "repo", "delete", full, "--yes"])
        if result.returncode != 0:
            print(
                "Pre-cleanup delete failed. The active gh token likely lacks "
                "'delete_repo' scope.\n"
                "Run: gh auth refresh -h github.com -u the-gigi -s delete_repo",
                file=sys.stderr,
            )
            return result.returncode

    repo_path = WORKDIR / name
    if repo_path.exists():
        print(f"Removing stale local workdir {repo_path}")
        import shutil
        shutil.rmtree(repo_path)
    WORKDIR.mkdir(parents=True, exist_ok=True)

    STATE_PATH.unlink(missing_ok=True)

    print(f"Scaffolding {repo_path} ...")
    scaffold_new_repo(repo_path, GOAL, name)

    print(f"Creating GitHub repo {full} (public, required for Pages) ...")
    repo_url = create_github_repo(name, org=owner, private=False)

    subprocess.run(
        ["git", "-C", str(repo_path), "branch", "-M", "main"],
        check=True, capture_output=True,
    )
    push_to_github(repo_path, repo_url)

    print("Ensuring the genesis:onboarding label exists ...")
    subprocess.run(
        ["gh", "label", "create", "genesis:onboarding",
         "--description", "Genesis onboarding issue", "--color", "0E8A16"],
        cwd=repo_path, capture_output=True,
    )

    print("Opening onboarding issue ...")
    issue_url = open_onboarding_issue(repo_path)

    pages_url = f"https://{owner}.github.io/{name}/"
    state = State(
        repo=f"{owner}/{name}",
        repo_url=repo_url,
        pages_url=pages_url,
        issue_url=issue_url,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    save_state(state)

    print()
    print(f"Repo:    {repo_url}")
    print(f"Issue:   {issue_url}")
    print(f"Pages:   {pages_url} (won't resolve until the system enables Pages and ships a build)")
    print(f"Local:   {repo_path}")
    print(f"State:   {STATE_PATH}")
    print()
    print("Next: monitor issues/PRs/Actions. When the app is deployed, run:")
    print("  uv run python scripts/manual_e2e.py verify")
    return 0


def http_status(url: str, timeout: int = 10) -> int | None:
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "genesis-e2e"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, TimeoutError):
        return None


def wait_for_pages(pages_url: str, timeout: int) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = http_status(pages_url)
        if status == 200:
            return True
        remaining = int(deadline - time.time())
        print(f"  Pages URL not ready (status={status}); {remaining}s left, sleeping 30s ...")
        time.sleep(30)
    return False


def play_game(pages_url: str, headless: bool) -> tuple[bool, str]:
    """Open the deployed app and play a deterministic winning game for X.

    Move order (cell indices): X=0, O=3, X=1, O=4, X=2 → X wins top row.
    Returns (passed, detail).
    """
    from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            page = browser.new_page()
            page.goto(pages_url, wait_until="domcontentloaded", timeout=30_000)

            # Sanity-check the contract before playing.
            for i in range(9):
                if page.locator(f'[data-cell="{i}"]').count() == 0:
                    return False, f"cell {i} missing (selector [data-cell=\"{i}\"] not found)"
            if page.locator("#status").count() == 0:
                return False, "no #status element on page"

            moves = [(0, "X"), (3, "O"), (1, "X"), (4, "O"), (2, "X")]
            for idx, expected_mark in moves:
                cell = page.locator(f'[data-cell="{idx}"]').first
                cell.click()
                page.wait_for_timeout(100)
                text = (cell.text_content() or "").strip()
                if text != expected_mark:
                    return False, (
                        f"after click on cell {idx}, expected '{expected_mark}' "
                        f"but cell shows '{text}'"
                    )

            status_text = (page.locator("#status").first.text_content() or "").lower()
            if "win" not in status_text:
                return False, f"X completed top row but status says: {status_text!r}"

            # Reset and confirm cells empty.
            if page.locator("#reset").count() > 0:
                page.locator("#reset").first.click()
                page.wait_for_timeout(100)
                cell0 = (page.locator('[data-cell="0"]').first.text_content() or "").strip()
                if cell0 not in ("", " "):
                    return False, f"after reset, cell 0 still shows {cell0!r}"

            return True, "X won the top row; status acknowledged; reset works"
        finally:
            browser.close()


def cmd_verify(args: argparse.Namespace) -> int:
    state = load_state()
    pages_url = args.pages_url or (state.pages_url if state else None)
    if not pages_url:
        print("No state and no --pages-url; bootstrap first or pass --pages-url.", file=sys.stderr)
        return 2

    print(f"Verifying {pages_url}")
    print(f"Waiting up to {args.wait}s for the Pages URL to return 200 ...")
    if not wait_for_pages(pages_url, timeout=args.wait):
        print("Pages URL never returned 200. The dev system hasn't deployed yet.")
        return 1

    print("Page is up. Launching Playwright to play a game ...")
    passed, detail = play_game(pages_url, headless=not args.headed)
    print(f"  {'PASS' if passed else 'FAIL'}: {detail}")
    return 0 if passed else 1


def cmd_cleanup(args: argparse.Namespace) -> int:
    state = load_state()
    repo = args.repo or (state.repo if state else None)
    if not repo:
        print("No state and no --repo; nothing to clean up.", file=sys.stderr)
        return 2

    print(f"Deleting GitHub repo {repo} ...")
    result = subprocess.run(["gh", "repo", "delete", repo, "--yes"])
    if result.returncode != 0:
        print(
            "gh repo delete failed. The token likely lacks 'delete_repo' scope.\n"
            "Run: gh auth refresh -h github.com -u the-gigi -s delete_repo",
            file=sys.stderr,
        )
        return result.returncode

    if state and state.repo == repo:
        STATE_PATH.unlink(missing_ok=True)
    print("Deleted.")
    return 0


ENHANCE_TITLE = "Add a human-vs-computer mode"
ENHANCE_BODY = """\
## Goal
Add a single-player mode where the human plays X against the computer (O). \
The existing two-player mode must remain available; the human picks the mode \
via a control on the page.

## Approach
Pick whichever — random move, minimax (perfect play, ~30 lines of JS for \
tic-tac-toe), or a small AI call. The choice doesn't matter for this test; \
just make the computer respond to the human's move on a previously-empty \
cell within ~1s.

## Hard requirements
- The existing two-player mode keeps working unchanged.
- The existing testability contract stays intact in **both** modes:
  - Cells still have `data-cell="0"`..`data-cell="8"` (left-to-right, \
top-to-bottom).
  - After a click, cell text is exactly "X" or "O".
  - `#status` text contains "win" (case-insensitive) on a win, "draw" on a \
draw, neither otherwise.
  - `#reset` clears the board.
- An external Playwright test depends on these selectors continuing to work; \
breaking them is a regression.

## Done criteria
- A visible control on the page toggles between "two players" and \
"vs computer" modes.
- In vs-computer mode, after the human's move the computer plays a move on an \
empty cell.
- Win/draw/reset behavior works in both modes.
- Deployed to GitHub Pages.
- Comment on this issue with the live URL once shipped.

## Notes for the orchestrator
- Not a `needs:human` issue — design, implement, and deploy autonomously. \
Only escalate if structurally blocked (the way Pages enablement was last \
time).
- File sub-tasks or ship in one PR, your call.
"""


def cmd_enhance(args: argparse.Namespace) -> int:
    state = load_state()
    repo = args.repo or (state.repo if state else None)
    if not repo:
        print("No state and no --repo; bootstrap first or pass --repo.", file=sys.stderr)
        return 2

    print(f"Filing feature-request issue on {repo} ...")
    result = subprocess.run(
        ["gh", "issue", "create",
         "--repo", repo,
         "--title", ENHANCE_TITLE,
         "--body", ENHANCE_BODY],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"gh issue create failed: {result.stderr.strip()}", file=sys.stderr)
        return result.returncode
    print(result.stdout.strip())
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Run `genesis serve` from the local scaffold dir (local-control-plane mode).

    Blocks in the foreground until Ctrl+C. Use a second terminal for `verify` /
    `enhance`. `genesis serve` auto-disables GHA workflows on start and
    re-enables them on graceful shutdown.
    """
    import shutil
    state = load_state()
    if state is None:
        print("No state file. Run `bootstrap` first.", file=sys.stderr)
        return 2
    # Derive local scaffold dir from state.repo (owner/name → name).
    repo_short = state.repo.split("/", 1)[1]
    local_path = WORKDIR / repo_short
    if not local_path.is_dir():
        print(f"Local scaffold missing at {local_path}. Re-run `bootstrap`.", file=sys.stderr)
        return 2
    if shutil.which("genesis") is None:
        print(
            "'genesis' command not on PATH. Run from the genesis repo via "
            "`direnv exec . uv run python scripts/manual_e2e.py serve`, or "
            "install with `uv pip install -e .` so the entry point is exposed.",
            file=sys.stderr,
        )
        return 127
    if shutil.which("claude") is None:
        print("'claude' command not on PATH. Install Claude Code first.", file=sys.stderr)
        return 127

    print(f"Starting `genesis serve` in {local_path}")
    print("  - disables GHA workflows on this repo")
    print("  - runs the orchestrator locally on each relevant event")
    print("  - polls until Ctrl+C; then re-enables GHA workflows")
    print(f"  - run `verify` / `enhance` from another terminal against {state.repo}")
    print()
    return subprocess.call(["genesis", "serve"], cwd=str(local_path))


def cmd_show(_: argparse.Namespace) -> int:
    state = load_state()
    if not state:
        print("No state file. Run `bootstrap` first.")
        return 1
    print(json.dumps(asdict(state), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_boot = sub.add_parser("bootstrap", help="Create + push a fresh tic-tac-toe dev repo.")
    p_boot.add_argument("--owner", default=DEFAULT_OWNER, help=f"GitHub owner (default: {DEFAULT_OWNER})")
    p_boot.add_argument("--name", default=None, help=f"Repo name override (default: {REPO_NAME})")
    p_boot.set_defaults(func=cmd_bootstrap)

    p_verify = sub.add_parser("verify", help="Drive the deployed app with Playwright and assert it works.")
    p_verify.add_argument("--pages-url", help="Override the Pages URL.")
    p_verify.add_argument("--wait", type=int, default=600, help="Seconds to wait for Pages to return 200 (default: 600).")
    p_verify.add_argument("--headed", action="store_true", help="Show the browser window.")
    p_verify.set_defaults(func=cmd_verify)

    p_clean = sub.add_parser("cleanup", help="Delete the GitHub repo (needs delete_repo scope).")
    p_clean.add_argument("--repo", help="Override owner/name.")
    p_clean.set_defaults(func=cmd_cleanup)

    p_enhance = sub.add_parser(
        "enhance",
        help="File a follow-up issue asking the dev system to add a human-vs-computer mode.",
    )
    p_enhance.add_argument("--repo", help="Override owner/name.")
    p_enhance.set_defaults(func=cmd_enhance)

    p_serve = sub.add_parser(
        "serve",
        help="Run `genesis serve` from the local scaffold (local control plane mode). Blocks.",
    )
    p_serve.set_defaults(func=cmd_serve)

    p_show = sub.add_parser("show", help="Print the saved state.")
    p_show.set_defaults(func=cmd_show)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
