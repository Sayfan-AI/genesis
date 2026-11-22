"""Root fixtures — keep the suite hermetic.

Everything here exists so `pytest tests/` passes on a machine that has nothing
configured: no ~/.gitconfig, no ~/.config/genesis, no network.
"""

import pytest


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give git an author/committer so scaffold commits don't need global config.

    `scaffold_new_repo` ends in `git commit`, which aborts with exit 128 and
    "Author identity unknown" when neither ~/.gitconfig nor the env supplies
    one. A fresh GitHub Actions runner has no global identity, so 19 tests
    failed there while passing on every developer machine — the suite silently
    depended on ambient state. These env vars override config, so they hold
    regardless of what the host has set.
    """
    for var, value in (
        ("GIT_AUTHOR_NAME", "genesis-tests"),
        ("GIT_AUTHOR_EMAIL", "tests@genesis.invalid"),
        ("GIT_COMMITTER_NAME", "genesis-tests"),
        ("GIT_COMMITTER_EMAIL", "tests@genesis.invalid"),
    ):
        monkeypatch.setenv(var, value)


@pytest.fixture(autouse=True)
def isolate_genesis_config(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test out of the real ~/.config/genesis.

    Root-level rather than e2e-only since `serve()` started reading the App
    credentials from that file: a unit test that calls `server.serve()` would
    otherwise load the developer's real App key.
    """
    monkeypatch.setenv("GENESIS_CONFIG_DIR", str(tmp_path / "genesis-config"))


@pytest.fixture(autouse=True)
def isolate_session_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the variables the plane exports into every session it launches.

    `genesis serve` sets each engine's `session_env` in its child's
    environment, and the evolver runs this suite from inside such a session
    before it pushes. `LocalControlPlane._session_env()` starts from
    `os.environ`, so those inherited values leaked into its tests: a Codex
    plane's env came back holding `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`, and
    `main` was red in every served session while green on CI (issue #166).

    Derived from `engines.ENGINES` so a new engine's variables are cleared
    without anyone remembering to list them here.
    """
    from genesis import engines

    for engine in engines.ENGINES.values():
        for var in engine.session_env:
            monkeypatch.delenv(var, raising=False)
