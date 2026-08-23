"""Behavior tests for the App-permission check in templates/scripts/activate.sh.

The check exists because of the shape of the failure it replaces, not because a
permission was hard to get right. A workflow's `permission-*` input can only
NARROW what the App installation already grants, so a missing grant is invisible
in every file a reviewer reads — and it surfaces mid-run: the agent authors the
change, commits it, and the push is refused. A whole session's work and budget
buys the discovery of a setup problem.

Measured twice downstream. `workflows` on the-gigi/butterfly (genesis issue #20),
where a committed change to `.github/workflows/` was rejected at push. Then
`actions` (genesis issue #14), where every `gh run list` from inside an agent
returned 403 and the evolver silently lost failed workflow runs — one of its
primary signals — without anything reporting it.

So the tests split by which way an error costs. Missing a required grant is the
expensive direction and must exit non-zero. But an unreadable API response must
NOT gate activation: this is a convenience check on the way to seeding secrets,
and a `python3` that isn't there, or a response shape GitHub changes later, has
no business stopping an adopter from activating a repo.
"""

import re
import subprocess
from pathlib import Path

import pytest

ACTIVATE = Path(__file__).parents[2] / "templates" / "scripts" / "activate.sh"

BASH = "/bin/bash"

COMPLETE = (
    '{"permissions":{"contents":"write","issues":"write","pull_requests":"write",'
    '"workflows":"write","actions":"write","metadata":"read"}}'
)


def _check(body: str):
    """Run `check_app_permissions` on its own, without activating anything.

    Lifted out of the script rather than reimplemented: the requirement list and
    the comparison are the things under test, so a copy here would pass while the
    shipped script drifted.
    """
    source = ACTIVATE.read_text()
    match = re.search(
        r"^# Exported because.*?^check_app_permissions\(\) \{.*?^\}$",
        source,
        re.S | re.M,
    )
    assert match, "activate.sh no longer defines check_app_permissions"
    script = f'set -uo pipefail\nREPO="acme/demo"\n{match.group(0)}\ncheck_app_permissions "$1"\n'
    return subprocess.run(
        [BASH, "-c", script, "_", body], capture_output=True, text=True
    )


def test_a_complete_grant_passes() -> None:
    result = _check(COMPLETE)
    assert result.returncode == 0, result.stderr
    assert "look right" in result.stdout


@pytest.mark.parametrize(
    "missing",
    ["contents", "issues", "pull_requests", "workflows", "actions"],
)
def test_each_required_grant_is_actually_required(missing) -> None:
    """One case per permission, because a check that only ever ran against the
    complete set would pass with any single requirement quietly dropped."""
    import json

    payload = json.loads(COMPLETE)
    del payload["permissions"][missing]
    result = _check(json.dumps(payload))

    assert result.returncode == 1, f"a missing `{missing}` grant did not fail setup"
    assert missing in result.stderr


def test_the_error_says_what_to_do_and_why_yaml_cannot_fix_it() -> None:
    """The whole value is turning a mid-run 403 into a checklist.

    The install-update step is load-bearing and the easiest to miss: adding a
    permission to the App does nothing until the *installation* accepts it, so an
    adopter who does half of it gets the identical failure and reasonably
    concludes the check lied.
    """
    import json

    payload = json.loads(COMPLETE)
    del payload["permissions"]["actions"]
    err = _check(json.dumps(payload)).stderr

    assert "ACCEPT the permission update" in err
    assert "cannot be fixed in YAML" in err
    assert "re-dispatches" in err, "the message should say what the grant is for"


def test_read_is_not_enough_where_write_is_needed() -> None:
    import json

    payload = json.loads(COMPLETE)
    payload["permissions"]["workflows"] = "read"
    result = _check(json.dumps(payload))

    assert result.returncode == 1
    assert "have read" in result.stderr


def test_an_unreadable_response_does_not_block_activation(tmp_path) -> None:
    """The failure direction that would be self-inflicted.

    This runs on the way to seeding secrets, and it's a convenience: a response
    shape GitHub changes later, or a `python3` that isn't on PATH, must not stop
    an adopter from activating a repo that is otherwise fine.
    """
    for body in ("", "not json at all", "[]", '{"no_permissions_key": true}'):
        result = _check(body)
        assert result.returncode == 0, f"activation was blocked by body {body!r}"


def test_the_check_is_actually_called_on_a_successful_install_lookup() -> None:
    """The tests above lift the function out and call it directly, so every one of
    them would pass with it sitting in the file as dead code.

    That's not hypothetical here: `host-guard.sh` was written, seeded and tested
    while inert because nothing declared it, and `claude-dir-guard.sh` was
    manifested but untracked. A check nobody invokes is a comment.
    """
    source = ACTIVATE.read_text()
    install_check = re.search(r"verify_app_installed\(\) \{.*?^\}$", source, re.S | re.M)
    assert install_check, "activate.sh no longer defines verify_app_installed"
    body = install_check.group(0)

    assert "check_app_permissions" in body, (
        "check_app_permissions is defined but never called from the install lookup"
    )
    # And on the 200 arm specifically — calling it on a 404 would evaluate a body
    # that describes an error, not an installation.
    ok_arm = re.search(r"200\)(.*?);;", body, re.S)
    assert ok_arm and "check_app_permissions" in ok_arm.group(1), (
        "the permission check must run on the successful lookup, not another arm"
    )
    assert "-o /dev/null" not in body, (
        "the response body is being discarded again, which is what made a missing "
        "grant a mid-run failure instead of a setup one"
    )
