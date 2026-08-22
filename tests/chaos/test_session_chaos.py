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

    Rung 3 continues on a changed repo fingerprint. When the fingerprint stops
    moving the chain has to end, or a task that dies the same way forever bills
    forever.
    """
    monkeypatch.setenv("GENESIS_COST_CEILING", "100")
    script([
        {"tools": 5, "subtype": "error_max_turns", "turns": 41, "cost": 1.0, "touch": "a.txt"},
        {"tools": 5, "subtype": "error_max_turns", "turns": 41, "cost": 1.0, "touch": "b.txt"},
        {"tools": 5, "subtype": "error_max_turns", "turns": 41, "cost": 1.0},
    ], judge="STOP")
    plane.run_orchestrator(None)
    assert plane.continuation_index >= 2, "should have resumed while work was landing"
    assert sessions_run() <= server.MAX_CONTINUATIONS + 2, "chain must be bounded"


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
