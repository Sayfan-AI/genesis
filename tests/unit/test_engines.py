"""The engine layer: how each coding agent's commands and output map onto what the
control plane acts on. The plane's policy is tested in test_server.py; this pins
the translation, so a second engine can be checked against the same contract."""

import json

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
