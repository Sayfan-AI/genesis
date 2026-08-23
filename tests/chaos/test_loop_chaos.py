"""Chaos scenarios against the poll loop rather than against one unit of work.

`test_session_chaos.py` covers everything inside `run_orchestrator`: the
continuation ladder, the bounds, the claim release. All of it stops at one chain.

The four failures genesis issue #33 named are one level up. They are emergent
from a *run* — several tasks in sequence, a merge landing between them, a second
plane starting on top of the first — and none of them are reachable from a single
chain, which is why a happy-path e2e and a single-chain chaos suite both miss
them. These drive `serve()` itself, with GitHub stubbed at the same boundary the
rest of the suite uses and the loop's own machinery left real.
"""

from __future__ import annotations

import os

from genesis import server


def test_every_task_in_a_run_gets_its_own_follow_up_budget(
    plane, script, loop, sessions_run, capsys
):
    """The self-advance stall that only appears at scale (genesis issue #33).

    The loop's own output does not wake it: the agent authenticates as the App,
    so a merge or a close emits a *bot* event and the feedback-loop filter drops
    it. The follow-up pass is the replacement — a session that changed the repo
    queues one more pass so the work it just landed gets picked up.

    That pass is bounded at `MAX_FOLLOWUP_CHAIN`, and the bound is per task, not
    per run: `serve` resets the counter before each event. Without the reset the
    bound becomes a run-wide quota, so the fourth task in a night lands its work
    to complete silence and the project idles until the six-hour cron — the same
    16-minute gap that got the follow-up pass written in the first place, except
    it no longer closes on its own.

    Six productive units of work here (the startup run plus five events), each
    one committing, and each one entitled to the same first rung of the budget.
    """
    script([
        {"tools": 3, "subtype": "success", "turns": 4, "cost": 0.5, "commit": f"task{i}.py"}
        for i in range(6)
    ])

    run = loop([["a-human"]] * 5)

    assert run["polls"] == 5, "the loop stopped before the run finished"
    assert sessions_run() == 6, f"one session per unit of work, ran {sessions_run()}"
    queued = capsys.readouterr().out.count("queueing a follow-up pass")
    assert queued == 6, (
        f"only {queued} of 6 tasks that landed work woke the loop again; the "
        "follow-up budget is being spent run-wide instead of per task"
    )


def test_a_stalled_task_is_picked_back_up_and_its_claim_is_back_first(
    plane, script, loop, issues_script, timeline
):
    """The other half of self-advance: a chain that stopped without finishing.

    `pending_followup` is set in three places and acted on in exactly one - the
    poll loop. Every existing test asserts the flag and stops there, so the rescue
    pass itself, the thing that actually recovers a stalled task, was never
    observed running.

    The ordering is the second half and it is not cosmetic. The rescue pass is a
    *fresh* session with none of the stalled chain's context: it re-selects work
    through `issues.sh next`, and `next` skips anything still labelled
    `in-progress`. A claim still held when that pass launches makes it walk past
    the exact task it was queued to rescue, which is what MaKlaude issue #195
    measured - 18 minutes of a different issue being worked while the stalled one
    sat labelled and idle.
    """
    script([
        # The startup run does nothing durable, so it queues no pass of its own
        # and the only follow-up in the timeline is the one under test.
        {"tools": 2, "subtype": "success", "turns": 3, "cost": 0.1},
        {"tools": 4, "subtype": "error_max_turns", "turns": 41, "cost": 1.0},
        {"tools": 3, "subtype": "success", "turns": 5, "cost": 0.5},
    ], judge="STOP nothing durable")

    run = loop([["a-human"], []])

    assert run["polls"] == 2
    entries = timeline()
    assert entries.count("issues release") == 1, (
        f"the stalled chain did not hand its issue back: {entries}"
    )
    released_at = entries.index("issues release")
    assert "session" in entries[released_at + 1:], (
        "nothing ran after the stall; the task is stranded until an unrelated "
        f"repo event happens along: {entries}"
    )
    assert "release" in issues_script()[-1] and "--session serve-" in issues_script()[-1]


def test_a_merged_pull_request_wakes_the_loop(plane, script, loop, sessions_run, capsys):
    """Auto-merge starvation's downstream twin, in local mode.

    In GitHub Actions `genesis-merge.yml` ends by dispatching the orchestrator,
    because the merge is bot-authored end to end and every workflow that would
    otherwise notice screens bots out. Local mode disables that workflow and does
    the merge itself, so it owes the same dispatch - and the merge is invisible to
    the poller for exactly the same reason, plus one more: a pull request going
    green is not in the repo events feed at all.

    Without the wake, `serve` merges a green pull request and then sits there. The
    milestone stalls with the work finished and landed, which is the most
    expensive shape of stall - everything is done and nobody is told.
    """
    script([{"tools": 2, "subtype": "success", "turns": 3, "cost": 0.2}] * 4)

    run = loop([[], []], merges=[[7], []])

    assert run["polls"] == 2
    assert sessions_run() == 2, (
        f"the merge landed and woke nothing; ran {sessions_run()} sessions "
        "(the startup run alone)"
    )
    assert "follow-up pass" in capsys.readouterr().out


def test_a_second_control_plane_does_not_start_on_top_of_the_first(
    plane, script, loop, sessions_run
):
    """The local half of "concurrency races between orchestrator runs".

    The GitHub Actions half is a shared concurrency group, asserted in
    tests/e2e/test_workflows.py. Local mode's equivalent is the lock file, and
    `test_acquire_lock_blocks_when_live_pid` covers the primitive - but the defect
    shape here is *ordering*, not the predicate. `serve` disables every `genesis-*`
    workflow, and it returns straight out on a failed lock without going through
    the shutdown path that restores them. So a second plane that disabled before
    it checked would leave the repo with GitHub Actions off and only one plane
    running: strictly worse than the duplicate run the lock exists to prevent, and
    invisible until somebody wonders why the cron stopped.
    """
    script([{"tools": 2, "subtype": "success", "turns": 3, "cost": 0.1}])
    server.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    # This process, which is by construction alive - `acquire_lock` probes with
    # signal 0 and clears the lock if nobody answers.
    server.LOCK_PATH.write_text(str(os.getpid()))

    run = loop([])

    assert run["rc"] == 1, "the second plane started anyway"
    assert sessions_run() == 0, "two planes ran a session against one repo"
    assert run["disabled"] == 0, (
        "the second plane disabled the workflows before checking the lock, and it "
        "exits without re-enabling them"
    )
    assert run["enabled"] == 0, (
        "the second plane re-enabled workflows the first one is relying on being off"
    )
    assert server.LOCK_PATH.read_text() == str(os.getpid()), (
        "the second plane overwrote the first plane's lock"
    )
