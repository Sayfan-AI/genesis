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

from genesis import server, triggers


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


def _record_prompts(plane, monkeypatch) -> list[str]:
    """The prompt each fresh session in the run was launched with."""
    prompts: list[str] = []
    real = plane._run_session

    def recording(prompt, *args, **kwargs):
        if prompt is not None:  # a resume carries no prompt of its own
            prompts.append(prompt)
        return real(prompt, *args, **kwargs)

    monkeypatch.setattr(plane, "_run_session", recording)
    return prompts


def test_the_merge_wake_says_what_it_is_following_up_on(
    plane, script, loop, monkeypatch
):
    """Issue #114. The follow-up pass used to get the startup prompt, "This is a
    local control plane initial run", so the session queued because PR #113 had
    just merged was told it was a fresh start, re-surveyed the org from scratch,
    and couldn't tell which of four reasons had launched it."""
    script([{"tools": 2, "subtype": "success", "turns": 3, "cost": 0.2}] * 4)
    prompts = _record_prompts(plane, monkeypatch)

    loop([[], []], merges=[[7, 9], []])

    assert len(prompts) == 2, prompts
    startup, followup = prompts
    assert "initial run" in startup
    assert "initial run" not in followup
    assert "follow-up pass" in followup
    assert "the merge sweep landed PR #7, PR #9" in followup


def test_a_scheduled_run_answers_a_merge_wake_without_a_second_session(
    plane, script, loop, sessions_run, monkeypatch, capsys
):
    """The merge wake, when the schedule falls due on the same tick (issue #109).

    The merge sweep queues a follow-up pass, then `run_due_triggers` launches the
    scheduled session before the pass gets its turn. The two are the same session:
    the follow-up's prompt is "assess project state and advance work", and so is
    the cron's, for the same agent. Measured on genesis's own plane: PR #108
    merged, the scheduled session surveyed the board and found nothing, and the
    follow-up pass then surveyed the same board a minute later.

    One session answers both. If that session lands work or dies mid-task, it
    queues a pass of its own on the way out, which is the case the pass is for.
    """
    real_scheduled_due = triggers.scheduled_due
    fired = []

    def due_once(state, now, agent=triggers.ORCHESTRATOR_AGENT):
        if fired:
            return real_scheduled_due(state, now, agent)
        fired.append(True)
        return real_scheduled_due({}, now, agent)

    monkeypatch.setattr(triggers, "scheduled_due", due_once)
    monkeypatch.setattr(triggers, "open_human_gates", lambda *a, **k: [])
    script([{"tools": 2, "subtype": "success", "turns": 3, "cost": 0.2}] * 4)

    run = loop([[], []], merges=[[7], []])

    assert run["polls"] == 2
    out = capsys.readouterr().out
    assert "scheduled trigger" in out, "the scheduled trigger never fired"
    assert sessions_run() == 2, (
        f"ran {sessions_run()} sessions; expected the startup run and one "
        "scheduled run, with no follow-up pass repeating the scheduled one"
    )
    assert "follow-up pass" not in out


def test_a_scheduled_run_that_lands_work_still_queues_its_own_follow_up(
    plane, script, loop, sessions_run, monkeypatch, capsys
):
    """The other half of issue #109's fix.

    Clearing the flag before the scheduled session must not swallow the pass that
    session earns by landing work itself - that pass is what picks up the next
    unit, since the session's own bot-authored output wakes nothing.
    """
    real_scheduled_due = triggers.scheduled_due
    fired = []

    def due_once(state, now, agent=triggers.ORCHESTRATOR_AGENT):
        if fired:
            return real_scheduled_due(state, now, agent)
        fired.append(True)
        return real_scheduled_due({}, now, agent)

    monkeypatch.setattr(triggers, "scheduled_due", due_once)
    monkeypatch.setattr(triggers, "open_human_gates", lambda *a, **k: [])
    script([
        {"tools": 2, "subtype": "success", "turns": 3, "cost": 0.2},
        {"tools": 3, "subtype": "success", "turns": 4, "cost": 0.5, "commit": "work.py"},
        {"tools": 2, "subtype": "success", "turns": 3, "cost": 0.2},
    ])

    prompts = _record_prompts(plane, monkeypatch)

    run = loop([[], []], merges=[[7], []])

    assert run["polls"] == 2
    assert "merge sweep" not in prompts[-1] and "changed the repo" in prompts[-1], (
        "the scheduled session answered the merge wake, so the pass it queued "
        f"must not still carry the merge as a reason: {prompts[-1]!r}"
    )
    assert sessions_run() == 3, (
        f"ran {sessions_run()} sessions; a scheduled session that landed work "
        "should have queued one follow-up pass of its own"
    )
    assert "queueing a follow-up pass" in capsys.readouterr().out


def test_a_follow_up_pass_waits_for_ci_on_the_pull_request_its_session_opened(
    plane, script, loop, sessions_run, monkeypatch, capsys
):
    """Issue #116, the 09:23 sequence on genesis's own plane.

    A scheduled session opened PR #113 and its follow-up pass launched the same
    second, while CI was still running. That pass found nothing to do (14 turns,
    $0.46), and when the sweep landed PR #113 three minutes later it queued a
    second pass that did the real work. Held until CI finishes, the two reasons
    land in one pass.
    """
    real_scheduled_due = triggers.scheduled_due
    fired = []

    def due_once(state, now, agent=triggers.ORCHESTRATOR_AGENT):
        if fired:
            return real_scheduled_due(state, now, agent)
        fired.append(True)
        return real_scheduled_due({}, now, agent)

    monkeypatch.setattr(triggers, "scheduled_due", due_once)
    monkeypatch.setattr(triggers, "open_human_gates", lambda *a, **k: [])
    script([
        {"tools": 2, "subtype": "success", "turns": 3, "cost": 0.2},
        {"tools": 3, "subtype": "success", "turns": 4, "cost": 0.5, "commit": "work.py"},
        {"tools": 2, "subtype": "success", "turns": 3, "cost": 0.2},
        {"tools": 2, "subtype": "success", "turns": 3, "cost": 0.2},
    ])
    prompts = _record_prompts(plane, monkeypatch)

    # Poll 1: the scheduled session opens the pull request, CI is running.
    # Poll 2: still running. Poll 3: green, and the sweep lands it.
    run = loop([[], [], []], merges=[[], [], [113]], awaiting=[[113], [113], []])

    assert run["polls"] == 3
    assert sessions_run() == 3, (
        f"ran {sessions_run()} sessions; expected startup, scheduled, and one "
        "follow-up pass after the merge - not one pass during CI and another after"
    )
    assert "changed the repo" in prompts[-1] and "PR #113" in prompts[-1], prompts[-1]
    assert "holding the follow-up pass until CI finishes on PR #113" in capsys.readouterr().out


def test_a_held_follow_up_pass_runs_anyway_once_the_wait_is_up(
    plane, script, loop, sessions_run, monkeypatch, capsys
):
    """The hold is bounded: a check that never reports must not starve the loop."""
    monkeypatch.setattr(server, "FOLLOWUP_CHECKS_WAIT_SECONDS", 0)
    script([
        {"tools": 3, "subtype": "success", "turns": 4, "cost": 0.5, "commit": "work.py"},
        {"tools": 2, "subtype": "success", "turns": 3, "cost": 0.2},
    ])

    run = loop([[], []], awaiting=[[7], [7]])

    assert run["polls"] == 2
    assert sessions_run() == 2, "the follow-up pass was held past its bound"
    assert "running the follow-up pass anyway" in capsys.readouterr().out


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


def test_a_dropped_connection_on_a_poll_does_not_stop_the_plane(plane, script, loop, monkeypatch, capsys):
    """Measured on genesis-dashboard: `RemoteDisconnected` from the events poll
    reached the top-level handler and ended the plane. A transient transport
    error is a skipped tick, not a shutdown."""
    import http.client
    import json

    script([{"tools": 2, "subtype": "success", "turns": 3, "cost": 0.2}] * 4)
    failures = iter([
        http.client.RemoteDisconnected("Remote end closed connection without response"),
        http.client.IncompleteRead(b"[{"),
        TimeoutError("The read operation timed out"),
        json.JSONDecodeError("Unterminated string", "[{", 1),
    ])
    real = plane.poll_once

    def flaky(token):
        failure = next(failures, None)
        if failure is not None:
            raise failure
        return real(token)

    monkeypatch.setattr(plane, "poll_once", flaky)

    run = loop([[], [], [], [], []])

    assert run["polls"] == 5, "the plane stopped on a transient network error"
    assert run["rc"] == 0
    out = capsys.readouterr().out
    assert "RemoteDisconnected" in out and "Unexpected error" not in out


def test_a_gated_restart_waits_for_the_person_and_then_wakes(
    plane, script, loop, sessions_run, capsys
):
    """Issue #125, across the loop rather than at the call site.

    A restart behind an open needs:human gate launches no startup session, but
    the plane still enters the poll loop, and the person answering the gate is
    what wakes it. Skipping the startup run must not skip the loop.
    """
    script([{"tools": 2, "subtype": "success", "turns": 3, "cost": 0.2}] * 2)

    run = loop([[], ["a-human"]], gates=[{"number": 25, "title": "Sign-off"}])

    assert run["polls"] == 2
    assert sessions_run() == 1, (
        f"ran {sessions_run()} sessions; expected none at startup and one for "
        "the human's event"
    )
    assert "Skipping the initial run" in capsys.readouterr().out
