"""The engine layer: how each coding agent's commands and output map onto what the
control plane acts on. The plane's policy is tested in test_server.py; this pins
the translation, so a second engine can be checked against the same contract."""

import json
import os

import pytest

from genesis import engines
from genesis.engines import ClaudeEngine, SessionResult, SessionStarted, ToolCall


def feed_all(parser, events):
    out = []
    for e in events:
        out += parser.feed(e)
    return out


def test_the_default_engine_is_claude() -> None:
    assert isinstance(engines.get_engine(), ClaudeEngine)
    assert isinstance(engines.get_engine("Claude"), ClaudeEngine)


def test_an_unknown_engine_is_refused_with_the_choices() -> None:
    with pytest.raises(ValueError, match="claude"):
        engines.get_engine("gpt-in-a-trenchcoat")


def test_claude_session_and_resume_commands() -> None:
    eng = ClaudeEngine()
    fresh = eng.session_command("do the thing", resume=None, max_turns=40, tools="Read,Bash")
    assert fresh[:3] == ["claude", "-p", "do the thing"]
    assert fresh[fresh.index("--max-turns") + 1] == "40"
    assert fresh[fresh.index("--allowedTools") + 1] == "Read,Bash"
    assert "stream-json" in fresh

    resumed = eng.session_command("do the thing", resume="sess-1", max_turns=40, tools="Read")
    assert resumed[resumed.index("--resume") + 1] == "sess-1"
    # A resumed session gets the continuation prompt, never the original one.
    assert "do the thing" not in resumed
    assert eng.resume_prompt in resumed


def test_the_claude_judge_gets_no_tools_and_reports_json() -> None:
    cmd = ClaudeEngine().judge_command("judge this", max_turns=2)
    assert cmd[cmd.index("--allowedTools") + 1] == ""
    assert cmd[cmd.index("--output-format") + 1] == "json"


def test_the_claude_parser_reports_start_once_then_tools_then_result() -> None:
    sid = "sess-9"
    events = [
        {"type": "system", "subtype": "init", "session_id": sid},
        {"type": "assistant", "session_id": sid, "message": {"content": [
            {"type": "text", "text": "thinking"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "git status"}},
        ]}},
        {"type": "result", "subtype": "error_max_turns", "session_id": sid, "num_turns": 40,
         "total_cost_usd": 1.25, "duration_ms": 61500, "is_error": True},
    ]
    out = feed_all(ClaudeEngine().stream_parser(), events)
    assert out == [
        SessionStarted(sid),
        ToolCall("Bash git status"),
        SessionResult("error_max_turns", is_error=True, turns=40, cost=1.25, duration_s=62, session_id=sid),
    ]


def test_each_claude_result_carries_only_its_own_cost() -> None:
    """A process can emit two results (#78); each must report its own cost so the
    plane's `+=` totals them rather than double counting a running figure."""
    parser = ClaudeEngine().stream_parser()
    a = parser.feed({"type": "result", "subtype": "success", "total_cost_usd": 0.5})
    b = parser.feed({"type": "result", "subtype": "success", "total_cost_usd": 2.0})
    assert [r.cost for r in a + b if isinstance(r, SessionResult)] == [0.5, 2.0]


def test_the_claude_judge_reply_is_read_from_the_envelope() -> None:
    reply = ClaudeEngine().read_judge_output(json.dumps({"result": "STOP thrashing", "total_cost_usd": 0.03}))
    assert (reply.text, reply.cost, reply.cost_known) == ("STOP thrashing", 0.03, True)


def test_unreadable_judge_output_keeps_the_text_and_flags_the_cost() -> None:
    reply = ClaudeEngine().read_judge_output("CONTINUE it's nearly done")
    assert reply.text == "CONTINUE it's nearly done"
    assert reply.cost_known is False


def test_the_claude_engine_finds_a_declared_session_start_hook(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    eng = ClaudeEngine()
    assert eng.pre_session_hook_declared("pre-session.sh") is False
    eng.settings_path.parent.mkdir(parents=True)
    eng.settings_path.write_text(json.dumps({"hooks": {"SessionStart": [
        {"matcher": "", "hooks": [{"type": "command", "command": ".genesis/scripts/pre-session.sh"}]}
    ]}}))
    assert eng.pre_session_hook_declared("pre-session.sh") is True
    eng.settings_path.write_text("{not json")
    assert eng.pre_session_hook_declared("pre-session.sh") is False


# ---------- Codex ----------
#
# The event shapes below are the ones codex-cli 0.166.0 was measured emitting
# (genesis issue #146), including the warning `error` items it prints first.

from genesis.engines import CodexEngine, codex_tokens  # noqa: E402


def test_codex_runs_unattended_with_stdin_closed_and_no_turn_flag() -> None:
    eng = CodexEngine()
    cmd = eng.session_command("do it", resume=None, max_turns=40, tools="Read")
    assert cmd[:2] == ["codex", "exec"] and cmd[-1] == "do it"
    for flag in (
        "--json",
        "--trust",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
    ):
        assert flag in cmd
    assert "--max-turns" not in cmd, "codex has no such flag; the plane enforces the budget"
    assert eng.close_stdin is True and eng.enforces_turn_budget is False
    assert ClaudeEngine().close_stdin is False and ClaudeEngine().enforces_turn_budget is True


def test_codex_resume_continues_the_session_with_the_continuation_prompt() -> None:
    eng = CodexEngine()
    cmd = eng.session_command("do it", resume="t-1", max_turns=40, tools="Read")
    assert cmd[:4] == ["codex", "exec", "resume", "t-1"]
    assert "--trust" in cmd and "--dangerously-bypass-hook-trust" in cmd
    assert cmd[-1] == eng.resume_prompt and "do it" not in cmd


def test_the_codex_judge_is_read_only_and_leaves_no_session() -> None:
    cmd = CodexEngine().judge_command("judge", max_turns=2)
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert "--ephemeral" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    assert "--trust" not in cmd


def test_the_codex_parser_maps_measured_events() -> None:
    events = [
        {"type": "thread.started", "thread_id": "t-9"},
        {"type": "item.completed", "item": {"type": "error", "message": "ignoring features.rmcp_client"}},
        {"type": "turn.started"},
        {"type": "item.started", "item": {"type": "command_execution", "command": "/bin/zsh -lc 'git status'"}},
        {"type": "item.completed", "item": {"type": "command_execution", "command": "/bin/zsh -lc 'git status'",
                                            "exit_code": 0, "status": "completed"}},
        {"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "a.py", "kind": "update"}]}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {"input_tokens": 9000, "cached_input_tokens": 4000, "output_tokens": 50}},
    ]
    out = feed_all(CodexEngine().stream_parser(), events)
    assert out == [
        SessionStarted("t-9"),
        ToolCall("Bash git status"),
        ToolCall("Edit a.py"),
        # Cost is settled from the session log, not this running total.
        SessionResult("success"),
    ], "warning items and item.started must not count; the shell wrapper is unwrapped"


def test_a_failed_codex_turn_is_an_abnormal_ending() -> None:
    out = CodexEngine().stream_parser().feed({"type": "turn.failed", "error": {"message": "x"}})
    assert out == [SessionResult("error_during_execution", is_error=True)]


def test_codex_tokens_count_fresh_input_and_output() -> None:
    assert codex_tokens({"input_tokens": 109245, "cached_input_tokens": 76032, "output_tokens": 530}) == 33743
    assert codex_tokens(None) == 0


def test_the_codex_judge_reply_reads_the_last_message_and_its_usage() -> None:
    stdout = "\n".join(json.dumps(e) for e in [
        {"type": "thread.started", "thread_id": "j"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "STOP it is thrashing"}},
        {"type": "turn.completed", "usage": {"input_tokens": 800, "cached_input_tokens": 0, "output_tokens": 20}},
    ])
    reply = CodexEngine().read_judge_output(stdout)
    assert (reply.text, reply.cost, reply.cost_known) == ("STOP it is thrashing", 820, True)
    assert CodexEngine().read_judge_output("not json at all").cost_known is False


def _write_session_log(home, thread, totals, parent=None):
    """A session log shaped like the real ones: `session_meta` first, carrying the
    id and, for a subagent, its parent's id."""
    path = home / "sessions" / "2026" / "11" / "15" / f"rollout-2026-11-15T00-00-00-{thread}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"type": "session_meta", "payload": {"id": thread, "parent_thread_id": parent}}
    lines = [json.dumps(meta)] + [json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
        "total_token_usage": {"input_tokens": t, "cached_input_tokens": 0, "output_tokens": 0}}}}) for t in totals]
    path.write_text("\n".join(lines) + "\n")


def test_codex_cost_is_settled_per_process_from_the_session_log(tmp_path, monkeypatch) -> None:
    """The log holds the session's running total. A resume must be charged only
    what it added: measured, a resumed turn reported 142,552 cumulative input
    tokens of which 33,307 were its own (#146)."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    eng = CodexEngine()
    _write_session_log(tmp_path, "t-1", [17273, 109245])
    assert eng.settle_cost("t-1") == 109245
    _write_session_log(tmp_path, "t-1", [17273, 109245, 142552])
    assert eng.settle_cost("t-1") == 142552 - 109245
    assert eng.settle_cost("no-such-session") == 0
    assert eng.settle_cost(None) == 0


def test_codex_cost_includes_the_subagents_a_session_spawned(tmp_path, monkeypatch) -> None:
    """A subagent writes its own log, linked only by parent_thread_id, and its
    tokens aren't in the parent's total. Measured on the trial: parent 68,301,
    its human-interaction subagent 15,721 more (#146)."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    eng = CodexEngine()
    _write_session_log(tmp_path, "parent", [68301])
    _write_session_log(tmp_path, "child", [15721], parent="parent")
    _write_session_log(tmp_path, "grandchild", [1000], parent="child")
    _write_session_log(tmp_path, "unrelated", [99999])
    assert eng.settle_cost("parent") == 68301 + 15721 + 1000


def test_codex_hooks_are_found_in_codex_hooks_json(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    eng = CodexEngine()
    assert eng.pre_session_hook_declared("pre-session.sh") is False
    eng.hooks_path.parent.mkdir(parents=True)
    eng.hooks_path.write_text(json.dumps({"hooks": {"SessionStart": [
        {"matcher": "", "hooks": [{"type": "command", "command": ".genesis/scripts/pre-session.sh"}]}]}}))
    assert eng.pre_session_hook_declared("pre-session.sh") is True


def test_codex_costs_read_in_tokens() -> None:
    assert CodexEngine().format_cost(3_000_000) == "3,000,000 tokens"
    assert ClaudeEngine().format_cost(1.5) == "$1.50"


# ---------- choosing the engine ----------


def test_the_engine_comes_from_env_then_config_then_default(tmp_path, monkeypatch) -> None:
    from genesis import server

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GENESIS_ENGINE", raising=False)
    assert server.configured_engine() is None
    (tmp_path / ".genesis").mkdir()
    (tmp_path / ".genesis" / "config.toml").write_text('[project]\nname = "x"\n\n[engine]\nname = "codex"\n')
    assert server.configured_engine() == "codex"
    monkeypatch.setenv("GENESIS_ENGINE", "claude")
    assert server.configured_engine() == "claude"


def test_a_broken_config_file_falls_back_to_the_default_engine(tmp_path, monkeypatch) -> None:
    from genesis import server

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GENESIS_ENGINE", raising=False)
    (tmp_path / ".genesis").mkdir()
    (tmp_path / ".genesis" / "config.toml").write_text("[engine\nname = ")
    assert server.configured_engine() is None


def test_the_cli_engine_flag_sets_the_env_var(monkeypatch) -> None:
    from genesis import cli

    # setenv, not delenv: pytest then restores the original state after the CLI
    # writes the variable itself, so `codex` can't leak into later tests.
    monkeypatch.setenv("GENESIS_ENGINE", "placeholder")
    monkeypatch.setattr(cli, "serve", lambda: 0)
    assert cli.main(["serve", "--engine", "codex"]) == 0
    assert os.environ["GENESIS_ENGINE"] == "codex"


def test_the_token_bounds_are_read_from_codex_env_vars(monkeypatch) -> None:
    from genesis import server

    monkeypatch.setenv("GENESIS_TASK_TOKEN_CEILING", "1234")
    monkeypatch.setenv("GENESIS_TASK_COST_CEILING", "7")
    assert server._task_cost_ceiling(CodexEngine()) == 1234
    assert server._task_cost_ceiling(ClaudeEngine()) == 7
    monkeypatch.setenv("GENESIS_RUN_TOKEN_BUDGET", "not a number")
    assert server._run_cost_budget(CodexEngine()) == CodexEngine.run_budget_default


@pytest.mark.parametrize(("engine", "stdin"), [("codex", "devnull"), ("claude", None)])
def test_the_plane_closes_stdin_only_for_engines_that_need_it(engine, stdin, tmp_path, monkeypatch) -> None:
    """Measured: `codex exec` with stdin inherited sat for five minutes waiting for
    EOF. Claude's launch must stay exactly as it was."""
    import subprocess
    from unittest.mock import MagicMock

    from genesis import server

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(server, "loki_push", lambda *a, **k: True)
    seen = {}

    def fake_popen(cmd, **kwargs):
        seen.update(kwargs)
        proc = MagicMock()
        proc.pid = 4242
        proc.wait.return_value = 0
        proc.stdout = iter([])
        return proc

    plane = server.LocalControlPlane(repo="o/r", engine=engines.get_engine(engine))
    monkeypatch.setattr(plane, "run_pre_session_steps", lambda: None)
    monkeypatch.setattr(plane, "_session_env", lambda: {})
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    plane._run_session("hi", deadline=10**12)
    assert seen["stdin"] == (subprocess.DEVNULL if stdin == "devnull" else None)


def test_a_session_without_a_reported_turn_count_logs_the_counted_tool_calls(tmp_path, monkeypatch) -> None:
    """Codex reports no turn count. The trial run logged `turns=None`."""
    from genesis import server

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(server, "loki_push", lambda *a, **k: True)
    lines = []
    monkeypatch.setattr(server, "log", lines.append)
    plane = server.LocalControlPlane(repo="o/r", engine=CodexEngine())
    stream = [json.dumps(e) for e in [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "item.completed", "item": {"type": "command_execution", "command": "ls"}},
        {"type": "item.completed", "item": {"type": "command_execution", "command": "pwd"}},
        {"type": "turn.completed", "usage": {}},
    ]]
    plane._stream_progress(iter(stream))
    ended = [line for line in lines if "session ended" in line]
    assert ended and "turns=2" in ended[0]


def test_codex_settling_survives_every_session_meta_shape(tmp_path, monkeypatch) -> None:
    """Review subagents record `{"subagent": "review"}`. Assuming a dict there
    crashed the trial's plane (#146). Odd first lines are skipped, not fatal."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _write_session_log(tmp_path, "parent", [1000])
    _write_session_log(tmp_path, "spawned", [200], parent="parent")
    day = tmp_path / "sessions" / "2026" / "11" / "15"
    shapes = {
        "review": {"type": "session_meta", "payload": {"id": "review", "source": {"subagent": "review"}}},
        "exec": {"type": "session_meta", "payload": {"id": "exec", "source": "exec"}},
        "nested-str": {"type": "session_meta", "payload": {"id": "nested-str", "source": {"subagent": {"thread_spawn": "x"}}}},
        "payload-str": {"type": "session_meta", "payload": "not a dict"},
        "list": ["not", "a", "dict"],
    }
    for name, first in shapes.items():
        (day / f"rollout-2026-11-15T00-00-00-{name}.jsonl").write_text(json.dumps(first) + "\n")
    (day / "rollout-2026-11-15T00-00-00-broken.jsonl").write_text("{not json\n")
    assert CodexEngine().settle_cost("parent") == 1200


def test_a_failing_settle_warns_instead_of_stopping_the_plane(tmp_path, monkeypatch) -> None:
    from genesis import server

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(server, "loki_push", lambda *a, **k: True)
    lines = []
    monkeypatch.setattr(server, "log", lines.append)
    eng = CodexEngine()

    def boom(_sid):
        raise AttributeError("'str' object has no attribute 'get'")

    monkeypatch.setattr(eng, "settle_cost", boom)
    plane = server.LocalControlPlane(repo="o/r", engine=eng)
    plane.last_session_id = "t"
    plane._settle_session()
    assert plane.cost_is_lower_bound is True
    assert any("could not settle" in line for line in lines)


def test_claude_session_env_disables_background_tasks_while_codex_stays_empty() -> None:
    """Claude Code's unattended `-p` mode spawns background `Agent` tasks by
    default and kills them ten minutes after the parent flushes `success`
    (sweatless issue #103). The env belongs on ClaudeEngine, not CodexEngine."""
    assert ClaudeEngine().session_env == {
        "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
        "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0",
    }
    assert CodexEngine().session_env == {}


def test_claude_stderr_failure_catches_stopped_background_tasks() -> None:
    notice = (
        "1 background task(s) still running after 600s ceiling — stopping them "
        "and exiting (adjust via CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS)\n"
    )
    assert ClaudeEngine().stderr_failure(notice) == "error_background_tasks_stopped"
    assert ClaudeEngine().stderr_failure("ordinary warning line\n") is None
    assert CodexEngine().stderr_failure(notice) is None

