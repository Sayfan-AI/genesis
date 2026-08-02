"""Behavior tests for templates/scripts/log.sh — the activity logger that CC
hooks call on every tool use.

These run the real script against a throwaway HTTP server standing in for Loki,
because the failure this guards against is invisible from the outside: the push
succeeds with HTTP 204 and Loki silently drops the entry.
"""

import json
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest


LOG_SH = Path(__file__).parents[2] / "templates" / "scripts" / "log.sh"
PROJECT = "TestProject"


class _Handler(BaseHTTPRequestHandler):
    pushes: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).pushes.append(json.loads(body))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        """Silence the default stderr access log."""


@pytest.fixture
def fake_loki() -> Iterator[tuple[str, list[dict]]]:
    _Handler.pushes = []
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", _Handler.pushes
    server.shutdown()
    server.server_close()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A minimal directory tree with the .genesis/config.toml log.sh looks for."""
    (tmp_path / ".genesis").mkdir()
    (tmp_path / ".genesis" / "config.toml").write_text(f'[project]\nname = "{PROJECT}"\n')
    return tmp_path


def run_hook(repo: Path, url: str, event: str, ctx: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(LOG_SH), event],
        cwd=repo,
        input=json.dumps(ctx) if ctx is not None else "",
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "GENESIS_LOKI_URL": url,
            "GENESIS_LOKI_USER": "u",
            "GENESIS_LOKI_TOKEN": "t",
        },
    )


def entry(push: dict) -> tuple[str, str]:
    """Return (timestamp_ns, line) from a single-entry push payload."""
    ts, line = push["streams"][0]["values"][0]
    return ts, line


def test_rapid_hooks_get_distinct_nanosecond_timestamps(
    repo: Path, fake_loki: tuple[str, list[dict]]
) -> None:
    """The bug this replaces: a second-resolution timestamp made identical lines
    fired within the same second byte-identical, and Loki drops duplicate
    (timestamp, line) pairs per stream — losing every tool call after the first."""
    url, pushes = fake_loki
    ctx = {"session_id": "s1", "tool_name": "Bash"}
    for _ in range(6):
        assert run_hook(repo, url, "pre-tool-use", ctx).returncode == 0

    assert len(pushes) == 6
    stamps = [entry(p)[0] for p in pushes]
    assert len(set(stamps)) == 6, f"duplicate timestamps would be dropped by Loki: {stamps}"
    assert all(len(s) == 19 for s in stamps), f"expected nanosecond precision: {stamps}"


def test_payload_shape_and_low_cardinality_labels(
    repo: Path, fake_loki: tuple[str, list[dict]]
) -> None:
    url, pushes = fake_loki
    run_hook(repo, url, "pre-tool-use", {"session_id": "abc", "tool_name": "Edit", "agent_type": "worker"})

    stream = pushes[0]["streams"][0]["stream"]
    assert stream == {"project": PROJECT, "hook_event": "pre-tool-use", "service_name": PROJECT}
    # session/tool/agent belong in the line, not the labels — `| logfmt` promotes
    # them at query time without one stream per session.
    line = entry(pushes[0])[1]
    for expected in ("level=info", "hook=pre-tool-use", "session=abc", "tool=Edit", "agent=worker"):
        assert expected in line


def test_failure_hooks_are_error_level(repo: Path, fake_loki: tuple[str, list[dict]]) -> None:
    url, pushes = fake_loki
    run_hook(repo, url, "post-tool-use-failure", {"tool_name": "Bash"})
    assert "level=error" in entry(pushes[0])[1]


def test_values_with_quotes_stay_valid_json(repo: Path, fake_loki: tuple[str, list[dict]]) -> None:
    """String-concatenated JSON broke on any value containing a quote or newline."""
    url, pushes = fake_loki
    run_hook(repo, url, "pre-tool-use", {"tool_name": 'weird "tool" name', "session_id": "a b"})

    line = entry(pushes[0])[1]  # already proves the server parsed the JSON
    assert '"weird \\"tool\\" name"' in line or "weird" in line
    assert 'session="a b"' in line


def test_never_exits_nonzero_when_loki_is_unreachable(repo: Path) -> None:
    """A PreToolUse hook that exits non-zero can block the agent's tool call."""
    result = run_hook(repo, "http://127.0.0.1:1", "pre-tool-use", {"tool_name": "Bash"})
    assert result.returncode == 0
    assert "loki push failed" in result.stderr  # loud, not swallowed
    assert "[genesis] ts=" in result.stderr


def test_works_with_no_stdin_context(repo: Path, fake_loki: tuple[str, list[dict]]) -> None:
    url, pushes = fake_loki
    assert run_hook(repo, url, "session-start").returncode == 0
    line = entry(pushes[0])[1]
    assert f"project={PROJECT}" in line
    assert "session=" not in line


# ---------- what a tool call actually did ----------


def test_line_records_the_command_not_just_the_tool_name(
    repo: Path, fake_loki: tuple[str, list[dict]]
) -> None:
    """`tool=Bash` alone tells you nothing. The command is the whole point."""
    url, pushes = fake_loki
    run_hook(repo, url, "pre-tool-use", {"tool_name": "Bash", "tool_input": {"command": "go test ./..."}})
    assert 'target="go test ./..."' in entry(pushes[0])[1]


def test_file_tools_record_the_path(repo: Path, fake_loki: tuple[str, list[dict]]) -> None:
    url, pushes = fake_loki
    run_hook(repo, url, "post-tool-use", {"tool_name": "Edit", "tool_input": {"file_path": "/repo/plan.go"}})
    assert "target=/repo/plan.go" in entry(pushes[0])[1]


def test_failed_call_records_status_and_reason(repo: Path, fake_loki: tuple[str, list[dict]]) -> None:
    url, pushes = fake_loki
    run_hook(
        repo,
        url,
        "post-tool-use-failure",
        {
            "tool_name": "Bash",
            "tool_input": {"command": "go build ./..."},
            "tool_response": {"is_error": True, "error": "exit status 2"},
        },
    )
    line = entry(pushes[0])[1]
    assert "status=error" in line and 'error="exit status 2"' in line


def test_successful_call_records_ok(repo: Path, fake_loki: tuple[str, list[dict]]) -> None:
    url, pushes = fake_loki
    run_hook(repo, url, "post-tool-use", {"tool_name": "Read", "tool_input": {"file_path": "/a"}, "tool_response": {"is_error": False}})
    assert "status=ok" in entry(pushes[0])[1]


@pytest.mark.parametrize(
    "command",
    [
        "curl -u 1694942:glc_livetokenvalue https://logs-prod-021.grafana.net",
        "export ANTHROPIC_API_KEY=sk-ant-api03-abcdefghijk && go run .",
        "gh auth login --with-token ghp_aBcDeFgHiJkLmNoPqRsT",
        "curl https://user:hunter2@example.com/api",
        "aws configure set aws_access_key_id AKIAIOSFODNN7EXAMPLE",
    ],
)
def test_credentials_never_reach_the_log(
    repo: Path, fake_loki: tuple[str, list[dict]], command: str
) -> None:
    """Commands are the most useful field and the most likely to carry a secret.
    Loki has no delete, so anything that lands here is permanent."""
    url, pushes = fake_loki
    run_hook(repo, url, "pre-tool-use", {"tool_name": "Bash", "tool_input": {"command": command}})
    line = entry(pushes[0])[1]
    for secret in ("glc_livetokenvalue", "sk-ant-api03-abcdefghijk", "ghp_aBcDeFgHiJkLmNoPqRsT", "hunter2", "AKIAIOSFODNN7EXAMPLE"):
        assert secret not in line, f"leaked {secret}"
    assert "<redacted>" in line


def test_long_input_is_truncated(repo: Path, fake_loki: tuple[str, list[dict]]) -> None:
    """A Write tool's input is an entire file; logging it verbatim would balloon
    both the bill and the blast radius."""
    url, pushes = fake_loki
    run_hook(repo, url, "pre-tool-use", {"tool_name": "Write", "tool_input": {"file_path": "/x", "content": "y" * 5000}})
    assert len(entry(pushes[0])[1]) < 400
