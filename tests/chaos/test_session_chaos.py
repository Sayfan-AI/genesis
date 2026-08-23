"""Chaos scenarios against one unit of work: `run_orchestrator`.

Each test induces a failure the dev system has actually suffered, or one the
hardening claims to handle, and asserts the loop recovers or stops rather than
spinning. The catalog mirrors the failure modes from Chapter 4: brain death by
max-turns, a session that does nothing, runaway spend, and a hang.
"""

from __future__ import annotations

import json

from unittest.mock import MagicMock

from genesis import server


def test_brain_death_resumes_while_work_lands_then_stops(plane, script, sessions_run, monkeypatch):
    """A session dying at max-turns should be resumed only while it makes progress.

    Rung 3 continues when the session itself changed the repo. When it stops
    changing anything the chain has to end, or a task that dies the same way
    forever bills forever.

    The landing sessions *commit*. They used to only `touch` an untracked file,
    which scored as progress back when the signal hashed `git status --porcelain`
    - the same leniency that let a stray temporary file stand in for work (#47).
    """
    monkeypatch.setenv("GENESIS_COST_CEILING", "100")
    script([
        {"tools": 5, "subtype": "error_max_turns", "turns": 41, "cost": 1.0, "commit": "a.txt"},
        {"tools": 5, "subtype": "error_max_turns", "turns": 41, "cost": 1.0, "commit": "b.txt"},
        {"tools": 5, "subtype": "error_max_turns", "turns": 41, "cost": 1.0},
    ], judge="STOP")
    plane.run_orchestrator(None)
    assert plane.continuation_index >= 2, "should have resumed while work was landing"
    assert sessions_run() <= server.MAX_CONTINUATIONS + 2, "chain must be bounded"


def test_a_commit_the_session_authored_is_progress(plane, script, monkeypatch):
    """The positive control for the two negatives below.

    Narrowing what counts as progress is only correct if it still counts the
    real thing - a fix that simply disabled rung 3 would pass both negative
    tests. This is a single session so the follow-up check, which compares
    against the state before the *last* attempt, sees the commit.
    """
    monkeypatch.setenv("GENESIS_COST_CEILING", "100")
    script([{"tools": 3, "subtype": "success", "turns": 5, "cost": 1.0, "commit": "feature.py"}])

    plane.run_orchestrator(None)

    assert plane.followup_chain == 1, "a session that committed should wake the loop again"


def test_somebody_elses_merge_is_not_this_sessions_progress(
    plane, script, outside_writer, monkeypatch
):
    """The measured false positive (#47), reproduced end to end.

    A human merged a pull request while a session ran; the session pulled it,
    then spent an hour on reads and greps and produced nothing. HEAD had moved,
    so rung 3 scored it as work landing and the loop queued a follow-up pass on
    the strength of somebody else's commit.

    The judge says STOP here on purpose: a chain that stops for the right reason
    and one that stops for the wrong reason look identical from the outside, so
    the assertion is on *which rung answered*, not merely on stopping.
    """
    monkeypatch.setenv("GENESIS_COST_CEILING", "100")
    outside_writer("a human merged PR #215")
    script([
        {"tools": 5, "subtype": "error_max_turns", "turns": 41, "cost": 1.0, "pull": True},
        {"tools": 5, "subtype": "error_max_turns", "turns": 41, "cost": 1.0},
    ], judge="STOP not converging")

    plane.run_orchestrator(None)

    assert server._git(["log", "--oneline", "-1"]).endswith("a human merged PR #215"), (
        "the scenario is only meaningful if HEAD actually moved"
    )
    assert plane.continuation_index == 0, (
        "rung 3 must not resume on somebody else's commit; the judge decides here"
    )
    assert plane.followup_chain == 0, (
        "pulling somebody else's merge must not queue a follow-up pass"
    )


def test_a_stray_untracked_file_is_not_progress(plane, script, monkeypatch):
    """`git status --porcelain` reports untracked files, so a temporary file left
    by a tool was indistinguishable from a new source file.

    It's genuinely ambiguous - a new test file looks the same until it's added -
    so it falls to the judge rather than being scored either way outright.
    """
    monkeypatch.setenv("GENESIS_COST_CEILING", "100")
    script([
        {"tools": 5, "subtype": "error_max_turns", "turns": 41, "cost": 1.0, "touch": "tmp.log"},
        {"tools": 5, "subtype": "error_max_turns", "turns": 41, "cost": 1.0},
    ], judge="STOP nothing durable")

    plane.run_orchestrator(None)

    assert (repo_tmp := server._git(["status", "--porcelain"])) and "tmp.log" in repo_tmp, (
        "the scenario is only meaningful if the stray file is actually there"
    )
    assert plane.continuation_index == 0, (
        "rung 3 must not resume on a stray file; the judge decides here"
    )
    assert plane.followup_chain == 0, "an untracked stray must not queue a follow-up pass"


def test_a_session_that_runs_no_tools_stops_the_chain(plane, script, sessions_run, monkeypatch):
    """The unauthenticated-profile failure: success, one turn, zero tools, $0.00.

    Fifteen of those ran for real and consumed the event backlog while reporting
    success. Rung 2 exists to make that stop after the first one.
    """
    monkeypatch.setenv("GENESIS_COST_CEILING", "100")
    # The judge says CONTINUE and the ceiling is far away, so rung 2 is the only
    # thing that can end this chain. An earlier version of this test let the judge
    # say STOP, which made it pass with rung 2 deleted: right assertion, wrong
    # reason, no coverage.
    script([{"tools": 0, "subtype": "error_max_turns", "turns": 1, "cost": 0.0}] * 8,
           judge="CONTINUE")
    plane.run_orchestrator(None)
    assert sessions_run() <= 2, f"a no-tool session must not be resumed, ran {sessions_run()}"


def test_runaway_spend_trips_the_ceiling(plane, script, sessions_run, monkeypatch):
    """Rung 1 is the only stop that does not care what any model thinks."""
    monkeypatch.setenv("GENESIS_COST_CEILING", "2.5")
    script([
        {"tools": 4, "subtype": "error_max_turns", "turns": 41, "cost": 2.0, "touch": f"f{i}.txt"}
        for i in range(8)
    ], judge="CONTINUE")
    plane.run_orchestrator(None)
    assert sessions_run() <= 3, f"ceiling should have stopped the chain, ran {sessions_run()}"


def test_a_hung_session_is_killed_by_the_deadline(plane, script, monkeypatch):
    """A session that never returns must not hold the loop open forever."""
    monkeypatch.setenv("GENESIS_COST_CEILING", "100")
    plane.session_timeout = 3
    script([{"hang": True}])
    rc = plane.run_orchestrator(None)
    assert rc != 0 or plane.last_result_subtype != "success"


def test_a_session_the_deadline_kills_hands_its_issue_back(
    plane, script, issues_script, monkeypatch
):
    """MaKlaude issue #195 in miniature (#48).

    A session goes quiet, the deadline terminates it, and the label it applied at
    pickup outlives it. For 18 minutes `next --milestone 6` skipped that issue and
    picked a different one; the tree was clean, there was no branch and no commit,
    so nothing at all was under way. A human removed the label by hand.

    Worth running here rather than only as a unit test because a killed process
    is the shape that produces *no* result event: the ladder's resume predicate
    reads that as "not mid-task" and never engages, so a release hung off the
    resume predicate alone would silently skip the very case that was measured.
    """
    monkeypatch.setenv("GENESIS_COST_CEILING", "100")
    plane.session_timeout = 3
    script([{"hang": True}])

    plane.run_orchestrator(None)

    assert plane.last_result_subtype is None, (
        "the scenario is only meaningful if the session died without reporting"
    )
    released = [c for c in issues_script() if "|release " in c]
    assert len(released) == 1, f"the killed session kept its claim: {issues_script()}"
    assert "--session serve-" in released[0]


def test_a_crashing_session_is_not_mistaken_for_success(plane, script, monkeypatch):
    """No result event at all is an abnormal ending, not a clean finish."""
    monkeypatch.setenv("GENESIS_COST_CEILING", "100")
    script([{"tools": 1, "crash": True}] * 4)
    plane.run_orchestrator(None)
    assert plane.last_result_subtype != "success"


def test_malformed_stream_output_does_not_crash_the_plane(plane, script, monkeypatch):
    """The parser sees whatever the harness prints, including garbage."""
    monkeypatch.setenv("GENESIS_COST_CEILING", "100")
    script([{"tools": 2, "garbage": True, "subtype": "success", "turns": 3, "cost": 0.1}])
    plane.run_orchestrator(None)
    assert plane.last_result_subtype == "success"


# ---------- the run-scoped budget (#46) ----------
#
# The per-task ceiling above is a tripwire on one continuation chain. These cover
# the bound that was missing entirely: a `serve` run that never trips the ceiling
# because no single chain comes close, and spends without limit anyway. Measured
# on MaKlaude at $52.11 against a $50 ceiling that never fired.


def test_independent_chains_sum_past_the_run_budget(plane, script, sessions_run, monkeypatch):
    """The case the per-chain ceiling cannot see.

    Every chain here is cheap, finishes cleanly, and is nowhere near the task
    ceiling. Before the run budget existed, the accumulator reset on each fresh
    chain and this loop launched forever.
    """
    monkeypatch.setenv("GENESIS_TASK_COST_CEILING", "50")
    monkeypatch.setenv("GENESIS_RUN_COST_BUDGET", "10")
    # subtype success, so no chain ever continues and no judge is consulted:
    # every dollar here is a *separate* unit of work, which is the point.
    script([{"tools": 4, "subtype": "success", "turns": 5, "cost": 3.0}] * 12)

    for _ in range(6):
        plane.run_orchestrator(None)

    # 0 -> 3 -> 6 -> 9 -> 12, and the fifth call refuses to launch at all.
    assert sessions_run() == 4, (
        f"the run budget should have stopped launching after 4 sessions, ran {sessions_run()}"
    )
    assert plane.run_spent == 12.0
    assert plane.shutdown is True, "reaching the run budget must shut the plane down, not idle it"


def test_the_task_ceiling_still_binds_with_the_run_budget_far_away(plane, script, sessions_run, monkeypatch):
    """The run budget is ADDED, not substituted.

    One expensive chain, a run budget it cannot reach, and the task ceiling as the
    only thing that can stop it. If a refactor ever replaced the per-chain bound
    with the run-scoped one, this is what would notice.
    """
    monkeypatch.setenv("GENESIS_TASK_COST_CEILING", "2.5")
    monkeypatch.setenv("GENESIS_RUN_COST_BUDGET", "10000")
    script([
        {"tools": 4, "subtype": "error_max_turns", "turns": 41, "cost": 2.0, "touch": f"g{i}.txt"}
        for i in range(8)
    ], judge="CONTINUE")

    plane.run_orchestrator(None)

    assert sessions_run() <= 3, f"the task ceiling should have stopped the chain, ran {sessions_run()}"
    assert plane.shutdown is False, "a task ceiling is a tripwire on one chain, not a reason to shut down"


def test_the_legacy_ceiling_env_var_is_still_honoured(plane, script, sessions_run, monkeypatch):
    """GENESIS_COST_CEILING is the name every existing operator config uses.

    Ignoring it after the rename would silently loosen a bound somebody had
    deliberately tightened, which is the worst direction for this class of change.
    """
    monkeypatch.delenv("GENESIS_TASK_COST_CEILING", raising=False)
    monkeypatch.setenv("GENESIS_COST_CEILING", "2.5")
    monkeypatch.setenv("GENESIS_RUN_COST_BUDGET", "10000")
    script([
        {"tools": 4, "subtype": "error_max_turns", "turns": 41, "cost": 2.0, "touch": f"h{i}.txt"}
        for i in range(8)
    ], judge="CONTINUE")

    plane.run_orchestrator(None)

    assert sessions_run() <= 3, f"the legacy env var stopped binding, ran {sessions_run()}"


def test_each_stop_reason_names_which_bound_fired(plane, monkeypatch):
    """"cost ceiling reached" said neither which bound nor what it bounded.

    A reader of the log or the Loki record has to be able to tell a task tripwire
    from a run-ending stop, because the two call for different actions: one is
    normal, the other means the night is over.
    """
    monkeypatch.setenv("GENESIS_TASK_COST_CEILING", "5")
    monkeypatch.setenv("GENESIS_RUN_COST_BUDGET", "20")
    plane.last_tool_calls = 3

    plane.run_spent = 0.0
    go, why = plane._should_continue("task", "fingerprint", spent=9.0)
    assert go is False
    assert "task cost ceiling reached" in why, why

    plane.run_spent = 25.0
    go, why = plane._should_continue("task", "fingerprint", spent=0.0)
    assert go is False
    assert "run cost budget reached" in why, why
    # The wider bound is reported even though the narrower one is also crossed,
    # because the wider one is what ends the run.
    go, why = plane._should_continue("task", "fingerprint", spent=9.0)
    assert "run cost budget reached" in why, why


def test_a_run_budget_stop_re_enables_the_workflows_it_disabled(plane, script, monkeypatch):
    """The stop path must take the same restore as SIGTERM.

    `serve` disables the `genesis-*` workflows for the duration of a local run. A
    budget stop that exited without re-enabling them would leave the repo with no
    orchestrator at all - GitHub Actions off and no local plane - which is worse
    than not stopping.
    """
    monkeypatch.setenv("GENESIS_TASK_COST_CEILING", "50")
    monkeypatch.setenv("GENESIS_RUN_COST_BUDGET", "4")
    # One session, over budget on its own, dying mid-task so the plane queues the
    # follow-up pass that then finds the budget spent.
    script([{"tools": 3, "subtype": "error_max_turns", "turns": 41, "cost": 5.0}], judge="STOP")

    enable = MagicMock()
    disable = MagicMock()
    monkeypatch.setattr(server, "enable_workflows", enable)
    monkeypatch.setattr(server, "disable_workflows", disable)
    monkeypatch.setattr(
        server, "fetch_events",
        lambda *a, **k: server.PollResult(events=[], etag=None, not_modified=True),
    )
    monkeypatch.setattr(plane, "_prime_high_water_if_needed", lambda *a, **k: None)

    # A safety valve on the poll loop, because `serve` is `while not shutdown` and
    # the thing under test is what sets that flag. Without this, deleting the fix
    # makes this test HANG rather than fail - discovered by trying it. A hang in CI
    # reads as a flaky runner; a red assertion reads as the defect it is.
    ticks = {"n": 0}

    def bounded_sleep(_seconds: float) -> None:
        ticks["n"] += 1
        if ticks["n"] > 5:
            plane.shutdown = True

    monkeypatch.setattr(plane, "_interruptible_sleep", bounded_sleep)

    rc = plane.serve()

    assert ticks["n"] <= 5, (
        "the run budget never stopped the loop - the safety valve did, after "
        f"{ticks['n']} polls"
    )
    assert rc == 0
    assert plane.shutdown is True
    assert disable.called, "serve must have disabled workflows to begin with"
    assert enable.called, "a budget stop must restore the workflows it disabled"


def test_a_resume_that_loads_nothing_does_not_end_the_chain(plane, script, sessions_run, monkeypatch):
    """The production near-miss (#43), end to end.

    A resume came back in six seconds as `success turns=0 cost=$0.00`, which is
    not an abnormal ending, so the chain ended holding real uncommitted work and
    $6.47 already spent. It survived only because the follow-up pass happened to
    launch a fresh session seconds later — luck, not mechanism.
    """
    monkeypatch.setenv("GENESIS_COST_CEILING", "100")
    empty = {"tools": 0, "subtype": "success", "turns": 0, "cost": 0.0}
    script([
        {"tools": 5, "subtype": "error_max_turns", "turns": 41, "cost": 1.0, "commit": "a.txt"},
        empty,
        empty,
    ], judge="STOP")

    plane.run_orchestrator(None)

    assert sessions_run() >= 3, "the empty resume should have been retried once"
    assert plane.pending_followup, (
        "a chain that could not be resumed must be handed on explicitly, not "
        "reported as a clean finish"
    )


def test_the_dev_repos_pre_agent_step_runs_under_serve(plane, script, repo, monkeypatch):
    """A net a dev system built to be deterministic stays deterministic across
    execution modes, or "we made this a script so nobody has to remember it" is
    only true in CI (#44)."""
    monkeypatch.setenv("GENESIS_COST_CEILING", "100")
    marker = repo / "pre-session-ran.txt"
    hook = repo / ".genesis" / "scripts" / "pre-session.sh"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f"#!/usr/bin/env bash\necho 'checked the gates'\ntouch '{marker}'\n")

    script([{"tools": 2, "subtype": "success", "turns": 3, "cost": 1.0}])
    plane.run_orchestrator(None)

    assert marker.exists(), "serve launched the agent without running the repo's pre-step"
