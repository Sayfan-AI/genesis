"""Tests for workflow template content."""

import re
from pathlib import Path

import pytest

from genesis.scaffold import (
    ORCHESTRATOR_TURN_FLOOR,
    SEED_WORKFLOWS,
    TEMPLATES_DIR,
    WORKFLOW_TURN_CLASSES,
    scaffold_new_repo,
)

REPO_ROOT = TEMPLATES_DIR.parent
MAX_TURNS_RE = re.compile(r"--max-turns\s+(\d+)")
MERGE_WORKFLOW = TEMPLATES_DIR / "workflows" / "genesis-merge.yml"


def _claude_workflows(directory: Path) -> list[Path]:
    """Workflow files in `directory` that actually invoke Claude."""
    return [
        wf
        for wf in sorted(directory.glob("*.yml"))
        if "anthropics/claude-code-action" in wf.read_text()
    ]


def _step(content: str, name: str) -> str:
    """The YAML of one named step, up to the next step or end of file.

    Cheaper than a YAML parser and it keeps this file dependency-free, which is
    the same reason ci.yml can run on every pull request for nothing.
    """
    start = content.index(f"- name: {name}")
    rest = content[start + 1 :]
    end = rest.find("\n      - ")
    return rest if end == -1 else rest[:end]


def test_orchestrator_workflow_uses_claude_action() -> None:
    content = (TEMPLATES_DIR / "workflows" / "genesis-orchestrator.yml").read_text()
    assert "anthropics/claude-code-action@v1" in content
    assert "ANTHROPIC_API_KEY" in content
    assert "cron:" in content
    assert "workflow_dispatch" in content


def test_events_workflow_uses_claude_action() -> None:
    content = (TEMPLATES_DIR / "workflows" / "genesis-events.yml").read_text()
    assert "anthropics/claude-code-action@v1" in content
    assert "ANTHROPIC_API_KEY" in content
    assert "issues:" in content
    assert "pull_request:" in content
    assert "issue_comment:" in content


def test_evolver_workflow_uses_claude_action() -> None:
    content = (TEMPLATES_DIR / "workflows" / "genesis-evolver.yml").read_text()
    assert "anthropics/claude-code-action@v1" in content
    assert "ANTHROPIC_API_KEY" in content
    assert "cron:" in content
    assert "workflow_dispatch" in content
    assert "evolver" in content


def test_events_workflow_skips_bot_events() -> None:
    """Asserted on the rule, not on one bot's name.

    This used to check for the literal `github-actions[bot]`, which passed the
    whole time the filter was an enumerated list of three names that a fourth bot
    walked straight past. See the suffix test further down for the reasoning.
    """
    content = (TEMPLATES_DIR / "workflows" / "genesis-events.yml").read_text()
    assert "endsWith(github.actor, '[bot]')" in content


def test_merge_workflow_calls_no_model_at_all() -> None:
    """Merging a green bot pull request is a predicate, not a judgement.

    The prompt this replaced ran on a 10-turn budget and had to find the pull
    requests, merge them, close the task issue and re-dispatch the orchestrator.
    On MaKlaude it ran out of turns holding the last of those, so every merge
    succeeded and the loop went quiet anyway. Shell steps have no budget to
    exhaust, which is the whole point — putting Claude back here would restore
    the failure mode, not the capability.
    """
    content = MERGE_WORKFLOW.read_text()
    assert "anthropics/claude-code-action" not in content
    assert "ANTHROPIC_API_KEY" not in content
    assert "prompt:" not in content
    assert "gh pr merge" in content and "--squash" in content


def test_merge_workflow_still_fires_before_a_ci_workflow_exists() -> None:
    """`workflow_run: workflows: ["CI"]` matches a workflow's `name:`, and a
    freshly scaffolded repo has no CI workflow to match.

    Seeded with only that trigger, auto-merge is dead until the dev system
    happens to pick the exact name — the state-derived sweep is what makes it
    work on day one, and it's the shape `genesis serve` already converged on.
    """
    content = MERGE_WORKFLOW.read_text()
    assert "workflow_run:" in content, "the low-latency path is still worth having"
    assert "schedule:" in content and "cron:" in content, (
        "auto-merge must not depend on a workflow named CI existing"
    )
    assert "workflow_dispatch:" in content, "an operator needs a way to force a sweep"


def test_merge_workflow_re_triggers_the_orchestrator_in_its_own_step() -> None:
    """All three halves of the re-trigger, each measured on MaKlaude.

    The dispatch lived in the prompt and died with the run; the App token asked
    for `workflows: write`, which governs editing workflow files rather than
    dispatching runs, so it 403'd; and `gh workflow run` without `--ref` resolves
    the default branch over the API first, a lookup that failed.
    """
    content = MERGE_WORKFLOW.read_text()
    assert "permission-actions: write" in content, (
        "gh workflow run needs actions: write - workflows: write is a different "
        "permission and was the bug"
    )
    step = _step(content, "Re-trigger the orchestrator")
    assert "if: always()" in step, (
        "a merge step that dies partway must still dispatch for what it landed"
    )
    assert "gh workflow run genesis-orchestrator.yml --ref main" in step, (
        "--ref isn't optional; without it gh does a default-branch lookup that failed"
    )
    assert "steps.merge.outputs.merged != ''" in step, (
        "an hourly sweep that merged nothing must not wake a 40-turn orchestrator"
    )


def test_merge_workflow_does_not_share_a_concurrency_group() -> None:
    """A triage run holding the shared orchestrator group cancelled the merge run
    a green pull request had just triggered, and the milestone stalled with the
    work finished. Merges only need to serialize against merges.

    Asserted against the orchestrator's actual group rather than a spelling, so
    that folding auto-merge back into the shared group fails here even if someone
    renames the group on the way.
    """
    group_re = re.compile(r"^concurrency:\n\s+group:\s*(.+)$", re.M)
    content = MERGE_WORKFLOW.read_text()
    merge_group = group_re.search(content)
    assert merge_group, "auto-merge needs a concurrency group of its own"

    for sibling in ("genesis-orchestrator.yml", "genesis-events.yml"):
        other = group_re.search((TEMPLATES_DIR / "workflows" / sibling).read_text())
        assert other, f"{sibling} lost its concurrency group"
        assert merge_group.group(1) != other.group(1), (
            f"auto-merge shares a group with {sibling}; an orchestrator run will "
            "cancel a merge that a green pull request already earned"
        )

    assert "cancel-in-progress: false" in content, (
        "a cancelled merge is a lost merge and the next sweep is an hour out"
    )


def test_merge_workflow_leaves_room_for_a_pre_merge_gate() -> None:
    """Vetoes the shared predicate can't see (genesis issue #41: don't merge over
    an unaddressed human comment) need somewhere to stand.

    Finding candidates and merging them are separate steps wired through an
    output precisely so a gate can sit between them without being edited into
    the jq filter, which has to stay identical to automerge.py.
    """
    content = MERGE_WORKFLOW.read_text()
    assert "PRE-MERGE GATE SEAM" in content
    assert "id: candidates" in content and "id: merge" in content
    assert "steps.candidates.outputs.numbers" in content


def test_auto_merge_is_not_keyed_to_which_gating_workflow_finishes_last() -> None:
    """MaKlaude issue #33, measured on its PR #30.

    The repo grew a second gating workflow — `E2E (kind)` alongside `CI` — and
    the merge workflow's `workflow_run` list named only the first. `CI` finished
    first, fired the merge attempt, and was correctly turned away because e2e was
    still pending. `E2E (kind)` then went green and fired nothing, because it
    wasn't in the list. The pull request sat MERGEABLE and CLEAN and unmerged
    until a human noticed. PR #32 was queued up behind it to hit the same trap.

    The list can't be the fix: it's a guess at names a dev system hasn't chosen
    yet, and the trap reappears the day it adds a third gate. What removes the
    trap is the sweep being derived from repository state rather than from
    whatever woke it — so any trigger at all reaches every mergeable pull
    request, and the hourly cron bounds how long a missed one waits.
    """
    body = "\n".join(
        line
        for line in MERGE_WORKFLOW.read_text().splitlines()
        if not line.strip().startswith("#")
    )
    assert "schedule:" in body and "cron:" in body, (
        "with no cron, a gating workflow missing from the workflow_run list means "
        "a green pull request waits for a human"
    )
    assert "gh pr list --state open" in body, (
        "the sweep must ask what is open, not what the triggering event was about"
    )
    assert "github.event_name" not in body and "github.event.workflow_run" not in body, (
        "the job branches on which trigger woke it, so the trigger list is "
        "load-bearing again and the next unlisted gate reintroduces the trap"
    )


def test_every_orchestrator_workflow_sweeps_claims_before_the_agent() -> None:
    """The GitHub Actions answer to a silent `error_max_turns` death.

    A run that dies mid-agent decides nothing and therefore releases nothing:
    Actions has no continuation ladder, so the `in-progress` label `issues.sh
    claim` wrote at pickup outlives the run that wrote it, and every later run
    skips that issue. The sweep is the only thing that takes it back, and it has
    to be a shell step ahead of the agent — inside the prompt it would be the
    first casualty of the very budget death it exists to clean up after.

    Both orchestrator-class workflows need it, not just the cron. `genesis-events`
    fires far more often, claims work the same way, and dies the same way; it
    carried no sweep at all, so a claim stranded by an event-driven run waited for
    a scheduled run up to six hours later to notice it.
    """
    for name in ORCHESTRATOR_CLASS_TEMPLATES:
        content = _template(name)
        assert "issues.sh sweep-claims" in content, (
            f"{name} can strand a claim and never takes one back"
        )
        assert content.index("issues.sh sweep-claims") < content.index(
            "anthropics/claude-code-action"
        ), f"{name} sweeps after the agent step, so a run that dies never sweeps"
        sweep = _step(content, "Release claims nobody is working")
        assert "continue-on-error: true" in sweep, (
            f"{name} lets a failed backstop stop the work it is backing up"
        )


def test_workflows_have_correct_permissions() -> None:
    for name in ["genesis-orchestrator.yml", "genesis-events.yml", "genesis-evolver.yml"]:
        content = (TEMPLATES_DIR / "workflows" / name).read_text()
        assert "contents: write" in content
        assert "issues: write" in content
        assert "pull-requests: write" in content


def test_every_claude_workflow_template_is_classified() -> None:
    """A new Claude-invoking template must join a turn-budget class, or it
    silently escapes the floor guard below."""
    found = {wf.name for wf in _claude_workflows(TEMPLATES_DIR / "workflows")}
    assert found == set(WORKFLOW_TURN_CLASSES), (
        "templates/workflows and WORKFLOW_TURN_CLASSES disagree: "
        f"unclassified={sorted(found - set(WORKFLOW_TURN_CLASSES))}, "
        f"stale={sorted(set(WORKFLOW_TURN_CLASSES) - found)}"
    )


@pytest.mark.parametrize("name", sorted(WORKFLOW_TURN_CLASSES))
def test_workflow_template_declares_turn_budget_for_its_class(name: str) -> None:
    """Every Claude-invoking template declares an explicit `--max-turns`, at or
    above its class floor.

    A run that dies at `error_max_turns` produces no progress and no diagnosis,
    and the next run redoes the work from scratch. Orchestrator-class agents get
    headroom; narrow fixed-procedure agents stay small on purpose so needing more
    turns fails fast instead of wandering.
    """
    content = (TEMPLATES_DIR / "workflows" / name).read_text()
    match = MAX_TURNS_RE.search(content)
    assert match, f"{name} invokes Claude with no explicit --max-turns"
    turns = int(match.group(1))

    if WORKFLOW_TURN_CLASSES[name] == "orchestrator":
        assert turns >= ORCHESTRATOR_TURN_FLOOR, (
            f"{name} is orchestrator-class but budgets only {turns} turns "
            f"(floor {ORCHESTRATOR_TURN_FLOOR})"
        )
    else:
        assert turns < ORCHESTRATOR_TURN_FLOOR, (
            f"{name} is narrow-class but budgets {turns} turns. The fix for a "
            "starved fixed-procedure run is a tighter procedure, not a bigger "
            "budget — reclassify it if it genuinely became open-ended."
        )


def test_genesis_own_claude_workflows_meet_orchestrator_floor() -> None:
    """Genesis's own agent workflows are orchestrator-class too — they were the
    tightest-budgeted agents in the system while doing the broadest work."""
    workflows = _claude_workflows(REPO_ROOT / ".github" / "workflows")
    assert workflows, "no Claude-invoking workflows found in .github/workflows"
    for wf in workflows:
        match = MAX_TURNS_RE.search(wf.read_text())
        assert match, f"{wf.name} invokes Claude with no explicit --max-turns"
        turns = int(match.group(1))
        assert turns >= ORCHESTRATOR_TURN_FLOOR, (
            f"{wf.name} budgets only {turns} turns (floor {ORCHESTRATOR_TURN_FLOOR})"
        )


def test_ci_workflow_runs_the_suite_on_every_pr_without_secrets() -> None:
    """The guards in this file are only guards if something runs them.

    Before ci.yml existed, the whole suite ran only when a human remembered to,
    which made every assertion here advisory. Two properties keep it that way:
    it must fire on `pull_request`, and it must need no secrets — a CI job that
    costs money or an API key is one someone eventually disables.
    """
    content = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "pull_request:" in content
    # Match the commands, not the file. The comments in ci.yml discuss --frozen
    # at length to explain why it is *not* used, and scanning raw text flags
    # that prose — same reason the secrets check below matches on interpolation.
    commands = "\n".join(
        line
        for line in content.splitlines()
        if line.strip().lstrip("- ").startswith("run:")
    )
    # --locked, not --frozen: --frozen installs a stale lock without complaint,
    # so a dependency added to pyproject.toml without a re-lock would pass CI.
    assert "uv sync --locked" in commands, (
        "CI must install with --locked so a stale uv.lock fails the run"
    )
    assert "uv run --no-sync pytest" in commands, (
        "CI must run tests with --no-sync so the test step cannot touch the lock"
    )
    assert "--frozen" not in commands, (
        "--frozen is not the guard it looks like: it accepts a stale lock, and "
        "it does not keep uv off the configured index (build-system.requires is "
        "resolved outside the lock). Use --locked + --no-sync."
    )
    # A workflow can only consume a secret through this interpolation, so match
    # on it rather than the bare word — prose in a comment is not a secret.
    assert "${{ secrets." not in content, (
        "CI must not consume secrets — it has to run on every PR for free"
    )


def test_every_workflow_template_is_actually_seeded() -> None:
    """A template the scaffolder never copies is a file nobody runs.

    `genesis-merge.yml` sat in templates/workflows for months without being in
    SEED_WORKFLOWS, so every dev system was born unable to merge its own pull
    requests: a worker's PR went green and stopped, and no bot-authored event
    could wake the orchestrator to notice. The class of bug is "written but not
    shipped", so assert the directory and the manifest agree rather than
    trusting the next person to update both.
    """
    on_disk = {wf.name for wf in (TEMPLATES_DIR / "workflows").glob("*.yml")}
    assert on_disk == set(SEED_WORKFLOWS), (
        "templates/workflows and SEED_WORKFLOWS disagree: "
        f"never seeded={sorted(on_disk - set(SEED_WORKFLOWS))}, "
        f"seeded but missing={sorted(set(SEED_WORKFLOWS) - on_disk)}"
    )


def test_scaffolded_workflows_match_templates(tmp_dir: Path) -> None:
    repo = tmp_dir / "test-project"
    scaffold_new_repo(repo, "test goal", "test-project")

    for name in SEED_WORKFLOWS:
        template = (TEMPLATES_DIR / "workflows" / name).read_text()
        scaffolded = (repo / ".github" / "workflows" / name).read_text()
        assert scaffolded == template


def test_template_action_pins_match_genesis_own_workflows() -> None:
    """Templates must not rot behind the actions genesis runs on itself.

    Dependabot's `github-actions` ecosystem only scans `.github/workflows/`.
    `templates/workflows/` is invisible to it, so every scaffolded dev repo kept
    getting `actions/checkout@v4` — pinned to a Node 20 runtime GitHub retired
    in June 2026 — while genesis's own workflows were bumped to v7 by a
    Dependabot PR nobody realised didn't cover the templates.

    Pinning templates to whatever genesis runs on itself makes the Dependabot PR
    that bumps `.github/workflows/` fail this test until the templates move too,
    which is the only automated pressure the template directory gets.
    """
    action_re = re.compile(r"uses:\s*([\w.-]+/[\w.-]+)@(\S+)")

    def pins(directory: Path) -> dict[str, set[str]]:
        found: dict[str, set[str]] = {}
        for wf in sorted(directory.glob("*.yml")):
            for action, version in action_re.findall(wf.read_text()):
                found.setdefault(action, set()).add(version)
        return found

    own = pins(REPO_ROOT / ".github" / "workflows")
    template = pins(TEMPLATES_DIR / "workflows")

    for action, versions in sorted(template.items()):
        if action not in own:
            # Only actions genesis also uses can be cross-checked; the rest are
            # covered by the single-version assertion below.
            continue
        assert versions == own[action], (
            f"templates/workflows pins {action} at {sorted(versions)} but "
            f".github/workflows pins it at {sorted(own[action])}. Dependabot "
            f"does not see templates/workflows — bump it by hand in the same PR."
        )

    for action, versions in sorted(template.items()):
        assert len(versions) == 1, (
            f"templates/workflows pins {action} at more than one version "
            f"({sorted(versions)}); a scaffolded repo should be internally consistent"
        )


def test_every_claude_workflow_grants_the_tools_that_actually_get_granted() -> None:
    """Tool grants have to come from `claude_args`, not from `permissions.allow`.

    Issue #7 proposed adding `Edit(*)` / `Write(*)` to the scaffolded
    `.claude/settings.json`. Probed against a real session, that would be inert
    where it matters: an untrusted workspace drops the repo's allow-list outright,
    reporting

        Ignoring N permissions.allow entries from .claude/settings.json:
        this workspace has not been trusted

    and every GitHub Actions checkout is untrusted. So the entries would appear to
    grant something, do nothing on the runner, and quietly start working once a
    developer accepted a trust dialog locally - the worst kind of config, one that
    behaves differently depending on who is looking at it.

    What does grant tools in both modes is `--allowedTools`, passed here through
    `claude_args` and in local mode through `server.ALLOWED_TOOLS`. `Write` is
    called out separately because without it an agent can edit files that exist
    and create none, so any task needing a new test, script or agent definition is
    impossible to satisfy - and the symptom is a confused agent, not an error.
    """
    for workflow in _claude_workflows(TEMPLATES_DIR / "workflows"):
        content = workflow.read_text()
        match = re.search(r"claude_args:\s*\"([^\"]*)\"", content)
        assert match, f"{workflow.name} invokes Claude with no claude_args"
        args = match.group(1)
        assert "--allowedTools" in args, (
            f"{workflow.name} grants no tools; a permissions.allow entry in "
            "settings.json will not do it, because an untrusted workspace drops it"
        )
        granted = args.split("--allowedTools", 1)[1].split()[0].split(",")
        assert "Read" in granted and "Bash" in granted, (
            f"{workflow.name} cannot read the repo or run a command: {granted}"
        )


def test_the_orchestrator_class_can_create_files_not_only_edit_them() -> None:
    """`Write` is the one that's easy to leave out and hard to diagnose.

    Without it the agent edits existing files fine and silently cannot create any,
    so a task needing a new test or a new agent definition fails in a way that
    reads as the model being unhelpful. The narrow merge runner is exempt: its
    fixed procedure writes nothing.
    """
    from genesis.scaffold import WORKFLOW_TURN_CLASSES

    for workflow in _claude_workflows(TEMPLATES_DIR / "workflows"):
        if WORKFLOW_TURN_CLASSES.get(workflow.name) != "orchestrator":
            continue
        args = re.search(r"claude_args:\s*\"([^\"]*)\"", workflow.read_text()).group(1)
        granted = args.split("--allowedTools", 1)[1].split()[0].split(",")
        assert {"Write", "Edit"} <= set(granted), (
            f"{workflow.name} is orchestrator-class but cannot create files: {granted}"
        )


def test_local_mode_grants_the_same_tools_as_the_workflows() -> None:
    """A rule that holds in one execution mode and not the other is worse than no
    rule: it makes a project's behaviour depend on how it happens to be driven."""
    from genesis import server

    granted = set(server.ALLOWED_TOOLS.split(","))
    assert {"Read", "Write", "Edit", "Bash"} <= granted, (
        f"genesis serve grants fewer tools than the workflows do: {sorted(granted)}"
    )


ORCHESTRATOR_CLASS_TEMPLATES = ("genesis-orchestrator.yml", "genesis-events.yml")


def _template(name: str) -> str:
    return (TEMPLATES_DIR / "workflows" / name).read_text()


def test_scheduled_and_event_orchestrators_share_one_concurrency_group() -> None:
    """The races that hurt are between the two workflows, not inside either.

    Two `workflow_dispatch` runs 4 seconds apart each filed a "Milestone 1 plan"
    issue; separately, a human closing a milestone issue fired `issues:closed`
    and `issue_comment:created` together and produced 10 task issues for 5 tasks.
    Both runs passed their own duplicate check before the other had written
    anything, which is a race an in-prompt rule cannot win. Per-workflow groups
    would not have stopped either one — hence the assertion that the group string
    is identical across both.
    """
    groups = set()
    for name in ORCHESTRATOR_CLASS_TEMPLATES:
        content = _template(name)
        match = re.search(r"^concurrency:\n  group: (.+)$", content, re.MULTILINE)
        assert match, f"{name} has no concurrency group"
        groups.add(match.group(1).strip())
        assert "cancel-in-progress: false" in content, (
            f"{name} cancels queued runs; an event carries state that only exists "
            "in that event (an approval, a human's comment), so dropping the run "
            "drops the signal"
        )
    assert len(groups) == 1, (
        f"the orchestrator workflows do not share a group ({groups}), so a "
        "scheduled run and an event run can still collide"
    )


def test_the_bot_filter_matches_the_suffix_not_a_list_of_names() -> None:
    """The list shipped with three names and the loop that burned 30+ concurrent
    runs was started by a bot identity nobody had added.

    Every GitHub App actor ends in `[bot]` and no human login can, so the suffix
    is the invariant the list was approximating.
    """
    for name in ("genesis-events.yml", "genesis-push-trigger.yml"):
        content = _template(name)
        assert "endsWith(github.actor, '[bot]')" in content, (
            f"{name} does not filter bots by suffix"
        )
        assert "github.actor != 'github-actions[bot]'" not in content, (
            f"{name} still carries the enumerated bot list the suffix replaced"
        )


def test_the_two_execution_modes_agree_on_what_a_bot_is() -> None:
    """A project's loop-breaking must not depend on how it happens to be driven."""
    from genesis.server import is_bot_actor

    assert is_bot_actor("github-actions[bot]")
    assert is_bot_actor("some-app-nobody-listed[bot]")
    assert not is_bot_actor("the-gigi")
    assert "endsWith(github.actor, '[bot]')" in _template("genesis-events.yml")


def test_orchestrator_class_tokens_can_read_workflow_runs() -> None:
    """Without `permission-actions: read` every `gh run list` returns 403.

    That silently removes failed workflow runs from the evolver's signal set —
    one of its primary inputs — and leaves it reviewing a system it can't see
    failing. Read-only, so there's nothing to weigh against it.
    """
    for name in (*ORCHESTRATOR_CLASS_TEMPLATES, "genesis-evolver.yml"):
        assert "permission-actions: read" in _template(name), (
            f"{name} mints a token that cannot read Actions"
        )


def test_the_human_gate_skips_the_schedule_and_never_the_events() -> None:
    """The gate saves idle spend; putting it on the events workflow would wedge
    the project.

    A scheduled run while a `needs:human` issue is open reads the state, confirms
    the gate, and exits — one gate sat open 4+ days for ~17 idle runs. But a human
    closing or commenting on that issue arrives through the events workflow, and
    that IS the signal to advance. Gate it there and clearing the gate becomes
    impossible, which is also what keeps a stale label from deadlocking the loop.
    """
    scheduled = _template("genesis-orchestrator.yml")
    assert 'gh issue list --label "needs:human"' in scheduled
    assert "if: steps.gate.outputs.skip != 'true'" in scheduled, (
        "the gate computes a skip nothing acts on"
    )

    events = _template("genesis-events.yml")
    assert "needs:human" not in events, (
        "the events workflow must always run — it is how a human clears the gate"
    )


def test_no_workflow_uses_the_deprecated_app_id_input() -> None:
    """`app-id` still works and warns; a warning is a countdown.

    This sits on the auth path of a loop whose whole point is not needing a human,
    so the input going away is the one deprecation it can't absorb. The rename is
    free — the secret keeps holding the numeric App ID, because the action funnels
    `client-id` and `app-id` into the same `appId` and GitHub takes either value
    there.
    """
    for directory in (TEMPLATES_DIR / "workflows", REPO_ROOT / ".github" / "workflows"):
        for workflow in sorted(directory.glob("*.yml")):
            body = "\n".join(
                line
                for line in workflow.read_text().splitlines()
                if not line.strip().startswith("#")
            )
            assert "app-id:" not in body, (
                f"{workflow.name} uses the deprecated `app-id` input; use "
                "`client-id` with the same secret"
            )
            if "create-github-app-token" in body:
                assert "client-id:" in body, (
                    f"{workflow.name} mints an App token with no client-id"
                )


# ---------- genesis's own auto-merge (#39) ----------

GENESIS_MERGE = REPO_ROOT / ".github" / "workflows" / "genesis-merge.yml"


def test_genesis_can_land_its_own_bot_pull_requests() -> None:
    """Genesis was the only repo in the family that couldn't self-advance past a
    pull request, so every framework fix sat open until a human noticed."""
    assert GENESIS_MERGE.is_file(), "genesis has no auto-merge workflow of its own"


def test_genesis_auto_merge_runs_the_same_predicate_as_the_template() -> None:
    """The *rule* is shared; the wiring around it isn't.

    Genesis's copy legitimately differs in what wakes it and what it dispatches
    afterwards — it has an evolver, not an orchestrator. What must never drift is
    which pull requests are eligible, because that's the same predicate
    `automerge.py` implements and the same one every seeded repo gets. Comparing
    the jq program rather than the whole file is what lets the intended
    differences stay legal while the unintended one fails.
    """
    template = (TEMPLATES_DIR / "workflows" / "genesis-merge.yml").read_text()
    own = GENESIS_MERGE.read_text()
    pattern = re.compile(r"<<'JQ'\n(.*?)\n\s*JQ\n", re.S)
    template_jq = pattern.search(template)
    own_jq = pattern.search(own)
    assert template_jq and own_jq, "one of the merge workflows lost its jq predicate"
    assert template_jq.group(1) == own_jq.group(1), (
        "genesis's auto-merge predicate has drifted from the one it seeds; the "
        "framework must not land its own work on a looser rule than it ships"
    )


def test_only_bot_authored_pull_requests_are_eligible_in_genesis() -> None:
    """The whole reason auto-merging genesis is acceptable.

    A dev repo auto-merging its own work is contained; genesis auto-merging a
    change to `templates/` propagates to every repo it seeds afterwards. The
    author predicate is what keeps that bounded — a person's change to the
    templates still stops at a person.
    """
    jq = re.search(r"<<'JQ'\n(.*?)\n\s*JQ\n", GENESIS_MERGE.read_text(), re.S).group(1)
    assert 'endswith("[bot]")' in jq, (
        "genesis's auto-merge would land human-authored pull requests, which is "
        "the case the decision in issue #39 deliberately excluded"
    )


def test_every_dispatch_target_is_a_workflow_that_exists_beside_it() -> None:
    """`gh workflow run` on a filename that isn't there fails at run time only.

    Genesis's copy dispatches `genesis-evolver.yml` because genesis has no
    orchestrator; copying the template's `genesis-orchestrator.yml` across would
    have produced a merge that lands the work and then quietly fails to wake
    anything — the same silent-stall shape the dispatch exists to prevent.
    """
    dispatch = re.compile(r"gh workflow run (\S+\.yml)")
    for directory in (TEMPLATES_DIR / "workflows", REPO_ROOT / ".github" / "workflows"):
        present = {p.name for p in directory.glob("*.yml")}
        for workflow in sorted(directory.glob("*.yml")):
            for target in dispatch.findall(workflow.read_text()):
                assert target in present, (
                    f"{workflow.name} dispatches {target}, which does not exist in "
                    f"{directory.name}/ — the dispatch would 404 at run time"
                )
