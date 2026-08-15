"""Chaos scenarios against one unit of work: `run_orchestrator`.

Each test induces a failure the dev system has actually suffered, or one the
hardening claims to handle, and asserts the loop recovers or stops rather than
spinning. The catalog mirrors the failure modes from Chapter 4: brain death by
max-turns, a session that does nothing, runaway spend, and a hang.
"""

from __future__ import annotations

import json

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
