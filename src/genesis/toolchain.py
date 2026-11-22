"""Warn when the toolchain a repo declares isn't on the PATH sessions get.

Genesis issue #139. `serve` checked that the engine's own binary was on PATH and
nothing else. On sagrin/sweatless the plane's PATH had Homebrew's `node` but no
`pnpm`, although `package.json` declared it, so every session fell back to
`npx -y pnpm@...` on every command. Each session recovered and finished green,
so nothing failed and nothing reported it. A person found it in a transcript.

This is a warning, not a stop. A project can build without its declared tool on
the plane's PATH (a session may install it, or the project may not need it for
the work at hand), and a plane that refuses to start over that would be worse
than the slowdown. What it fixes is the silence: the operator sees, once, at
startup, which tool is missing and which PATH it was looked up on.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path


def declared_tools(root: Path) -> list[tuple[str, str]]:
    """The tools `root` declares it builds with, as `(binary, where declared)`.

    Only `package.json`'s `packageManager` today, because that's the one that
    has cost sessions time. It reads like `pnpm@10.18.0+sha512.abc`, and the
    binary is the part before the first `@`.
    """
    try:
        data = json.loads((root / "package.json").read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    declared = data.get("packageManager")
    if not isinstance(declared, str):
        return []
    binary = declared.split("@", 1)[0].strip()
    if not binary:
        return []
    return [(binary, f"package.json packageManager {declared!r}")]


def missing_tool_warnings(root: Path, path: str | None) -> list[str]:
    """One warning per declared tool that doesn't resolve on `path`.

    `path` is the PATH a session will get, not the caller's, so the check
    answers the question that matters: will a session find it.
    """
    warnings = []
    for binary, source in declared_tools(root):
        if shutil.which(binary, path=path) is None:
            warnings.append(
                f"WARNING: {source} declares {binary}, but {binary} isn't on the "
                f"PATH sessions get, so every session will fall back to something "
                f"slower or fail. Install it (for pnpm or yarn, `corepack enable`) "
                f"and restart the plane. Session PATH: {path or '(empty)'}"
            )
    return warnings
