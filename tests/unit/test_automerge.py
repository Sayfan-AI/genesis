"""Tests for the local equivalent of the genesis-merge workflow.

The gap being closed: in CI a pull request going green fires `workflow_run` and a
merge agent lands it. Locally that workflow is disabled, CI-completion is not an
event the poller sees, and the agent's own pull requests are bot-authored so the
actor filter drops them. Without a state-derived sweep the loop can open work it
can never land.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from genesis import automerge


def pr(number=1, author="genesis-dev-bot[bot]", draft=False, mergeable="MERGEABLE",
       checks=(("CI", "SUCCESS"),), created="2026-08-02T00:00:00Z", title="a change"):
    return {
        "number": number,
        "title": title,
        "isDraft": draft,
        "mergeable": mergeable,
        "author": {"login": author},
        "createdAt": created,
        "statusCheckRollup": [{"name": n, "conclusion": c} for n, c in checks],
    }


def fake_gh(listing, merges=None):
    merges = merges if merges is not None else []

    def _gh(args, token, timeout=60):
        if args[:2] == ["pr", "list"]:
            return 0, json.dumps(listing)
        if args[:2] == ["pr", "merge"]:
            merges.append(int(args[2]))
            return 0, "merged"
        return 1, "unexpected"

    return _gh, merges


def test_merges_a_green_bot_pull_request() -> None:
    gh, merges = fake_gh([pr(number=42)])
    with patch.object(automerge, "_gh", gh):
        assert automerge.merge_ready("o/r", "tok", log=lambda *_: None) == [42]
    assert merges == [42]


@pytest.mark.parametrize(
    "candidate,why",
    [
        (pr(draft=True), "draft"),
        (pr(author="the-gigi"), "human-authored: a human's PR stays a human's decision"),
        (pr(checks=(("CI", "FAILURE"),)), "a failing check"),
        (pr(checks=(("CI", "SUCCESS"), ("e2e", "PENDING"))), "a check still running"),
        (pr(checks=()), "no checks at all, so CI has probably not started"),
        (pr(mergeable="CONFLICTING"), "conflicts"),
    ],
)
def test_refuses_to_merge(candidate, why) -> None:
    gh, merges = fake_gh([candidate])
    with patch.object(automerge, "_gh", gh):
        assert automerge.merge_ready("o/r", "tok", log=lambda *_: None) == [], why
    assert merges == []


def test_stalest_first() -> None:
    listing = [
        pr(number=3, created="2026-08-02T03:00:00Z"),
        pr(number=1, created="2026-08-02T01:00:00Z"),
        pr(number=2, created="2026-08-02T02:00:00Z"),
    ]
    gh, _ = fake_gh(listing)
    with patch.object(automerge, "_gh", gh):
        assert [p["number"] for p in automerge.ready_to_merge("o/r", "tok")] == [1, 2, 3]


def test_a_failed_merge_does_not_stop_the_sweep() -> None:
    """A merge can lose a race or hit branch protection. The next tick retries."""
    def gh(args, token, timeout=60):
        if args[:2] == ["pr", "list"]:
            return 0, json.dumps([pr(number=1), pr(number=2)])
        if args[:2] == ["pr", "merge"]:
            return (1, "not mergeable") if args[2] == "1" else (0, "merged")
        return 1, "unexpected"

    with patch.object(automerge, "_gh", gh):
        assert automerge.merge_ready("o/r", "tok", log=lambda *_: None) == [2]


def test_unparseable_listing_is_survivable() -> None:
    with patch.object(automerge, "_gh", lambda *a, **k: (0, "not json")):
        assert automerge.ready_to_merge("o/r", "tok") == []
    with patch.object(automerge, "_gh", lambda *a, **k: (1, "gh exploded")):
        assert automerge.ready_to_merge("o/r", "tok") == []
