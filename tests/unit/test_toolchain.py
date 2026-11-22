"""The startup warning for a declared toolchain the session PATH can't find.

Genesis issue #139: sagrin/sweatless declared pnpm, the plane's PATH had none,
and every session paid an `npx -y pnpm@...` fallback on every command without
anything saying so.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from genesis.toolchain import declared_tools, missing_tool_warnings


def _bin_dir(tmp_path: Path, *names: str) -> str:
    """A directory holding executable stubs, usable as a PATH."""
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    for name in names:
        stub = d / name
        stub.write_text("#!/bin/sh\n")
        stub.chmod(0o755)
    return str(d)


def _package(root: Path, **fields: object) -> None:
    (root / "package.json").write_text(json.dumps(fields))


def test_the_binary_is_the_name_before_the_version(tmp_path: Path) -> None:
    _package(tmp_path, packageManager="pnpm@10.18.0+sha512.abc123")
    assert [b for b, _ in declared_tools(tmp_path)] == ["pnpm"]


@pytest.mark.parametrize(
    "contents",
    [None, "{not json", json.dumps([1, 2]), json.dumps({"name": "x"}),
     json.dumps({"packageManager": 7}), json.dumps({"packageManager": "@1.0"})],
    ids=["no-file", "bad-json", "not-an-object", "undeclared", "not-a-string", "no-name"],
)
def test_nothing_declared_means_nothing_to_check(tmp_path: Path, contents: str | None) -> None:
    if contents is not None:
        (tmp_path / "package.json").write_text(contents)
    assert declared_tools(tmp_path) == []
    assert missing_tool_warnings(tmp_path, "") == []


def test_a_declared_tool_missing_from_the_session_path_warns(tmp_path: Path) -> None:
    _package(tmp_path, packageManager="pnpm@10.18.0")
    path = _bin_dir(tmp_path, "node")
    [warning] = missing_tool_warnings(tmp_path, path)
    assert "pnpm" in warning
    assert "pnpm@10.18.0" in warning, "the warning should say where it was declared"
    assert path in warning, "the warning should name the PATH it searched"


def test_a_declared_tool_on_the_session_path_is_quiet(tmp_path: Path) -> None:
    _package(tmp_path, packageManager="pnpm@10.18.0")
    assert missing_tool_warnings(tmp_path, _bin_dir(tmp_path, "node", "pnpm")) == []


def test_the_lookup_uses_the_path_it_is_given_not_the_callers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plane's own PATH might have the tool when the session's doesn't, or
    the other way round. Only the session's answers the question."""
    _package(tmp_path, packageManager="pnpm@10.18.0")
    monkeypatch.setenv("PATH", _bin_dir(tmp_path, "pnpm"))
    empty = tmp_path / "empty"
    empty.mkdir()
    assert missing_tool_warnings(tmp_path, str(empty)) != []


def test_serve_warns_at_startup_and_keeps_going(monkeypatch, capsys, tmp_path) -> None:
    """Through `serve()` itself: the warning reaches the operator's log, and the
    plane carries on to its next step rather than stopping."""
    import subprocess
    from unittest.mock import MagicMock

    from genesis import server

    monkeypatch.chdir(tmp_path)
    _package(tmp_path, packageManager="pnpm@10.18.0")
    claude_dir = tmp_path / "claude-bin"
    claude_dir.mkdir()
    (claude_dir / "claude").write_text("#!/bin/sh\n")
    (claude_dir / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", str(claude_dir))
    monkeypatch.setattr(server, "enable_workflows", MagicMock())
    disable = MagicMock()
    monkeypatch.setattr(server, "disable_workflows", disable)
    monkeypatch.setattr(
        server, "_gh_token", MagicMock(side_effect=subprocess.CalledProcessError(1, "gh"))
    )

    plane = server.LocalControlPlane(repo="alice/test", poll_interval=1, session_timeout=5)
    plane.serve()

    out = capsys.readouterr().out
    assert "packageManager 'pnpm@10.18.0' declares pnpm" in out
    assert disable.called, "a missing project tool must not stop the plane"
