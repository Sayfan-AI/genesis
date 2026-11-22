"""The suite must pass inside the sessions `genesis serve` launches (issue #166).

CI can't see this class of failure on its own: a runner has none of the
plane's session variables set, so a test that reads the ambient environment
passes there and fails only for the evolver, which runs the suite from inside
a served session before every push. These tests put the session's environment
back in front of the suite.
"""

import os
import subprocess
import sys
from pathlib import Path

from genesis import engines

REPO_ROOT = Path(__file__).resolve().parents[2]


def _all_session_vars() -> dict[str, str]:
    merged: dict[str, str] = {}
    for engine in engines.ENGINES.values():
        merged.update(engine.session_env)
    return merged


def test_session_vars_exist_to_be_cleared() -> None:
    # If no engine exported anything, the subprocess test below would pass
    # vacuously.
    assert _all_session_vars()


def test_no_engine_session_var_reaches_a_test() -> None:
    leaked = sorted(v for v in _all_session_vars() if v in os.environ)
    assert not leaked, f"conftest should clear these before every test: {leaked}"


def test_suite_passes_with_a_served_sessions_environment() -> None:
    """Run the test that issue #166 caught in a child pytest whose environment
    carries every engine's session variables, as a served session's does."""
    env = {**os.environ, **_all_session_vars()}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "tests/unit/test_claims.py::test_session_env_propagates_engine_session_env",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
