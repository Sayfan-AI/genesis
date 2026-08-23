"""Tests for the local equivalent of the genesis-merge workflow.

The gap being closed: in CI a pull request going green fires `workflow_run` and a
merge agent lands it. Locally that workflow is disabled, CI-completion is not an
event the poller sees, and the agent's own pull requests are bot-authored so the
actor filter drops them. Without a state-derived sweep the loop can open work it
can never land.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from unittest.mock import patch

import pytest

from genesis import automerge
from genesis.scaffold import TEMPLATES_DIR


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


# --- parity with the GitHub Actions half of the same rule --------------------
#
# `templates/workflows/genesis-merge.yml` runs this exact predicate as a jq
# filter on a runner. The two can't share code - this is Python inside the
# genesis package, that is jq in a scaffolded repo that has never heard of
# genesis - so the guard is differential: run both over the same pull-request
# listings and fail if they ever disagree. Text-matching the two files would
# pass on any rewrite that kept the vocabulary and changed the meaning.

MERGE_WORKFLOW = TEMPLATES_DIR / "workflows" / "genesis-merge.yml"


def _jq_filter() -> str:
    """The jq program the workflow writes with a heredoc, lifted verbatim."""
    match = re.search(r"<<'JQ'\n(.*?)\n\s*JQ\n", MERGE_WORKFLOW.read_text(), re.S)
    assert match, "genesis-merge.yml no longer embeds a jq merge predicate"
    return match.group(1)


def _workflow_verdict(listing: list[dict]) -> list[int]:
    result = subprocess.run(
        ["jq", _jq_filter()],
        input=json.dumps(listing),
        capture_output=True,
        text=True,
        check=True,
    )
    return [p["number"] for p in json.loads(result.stdout)]


def _python_verdict(listing: list[dict]) -> list[int]:
    with patch.object(automerge, "_gh", lambda *a, **k: (0, json.dumps(listing))):
        return [p["number"] for p in automerge.ready_to_merge("o/r", "tok")]


# Real `gh pr list` output is messier than the `pr()` helper above: a check run
# that is still going reports an empty `conclusion` rather than a status word, a
# legacy status context carries `state` and no `conclusion` at all, and
# `mergeable` is absent entirely while GitHub is still computing it. Each of
# those is a plausible place for two implementations to drift apart quietly.
IN_PROGRESS_CHECK = {"name": "CI", "status": "IN_PROGRESS", "conclusion": ""}
LEGACY_STATUS = {"context": "legacy-ci", "state": "SUCCESS"}


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
@pytest.mark.parametrize(
    "listing",
    [
        [],
        [pr(number=42)],
        [pr(draft=True)],
        [pr(author="the-gigi")],
        [pr(checks=(("CI", "FAILURE"),))],
        [pr(checks=(("CI", "SUCCESS"), ("e2e", "PENDING")))],
        [pr(checks=())],
        [pr(checks=(("CI", "SUCCESS"), ("optional", "SKIPPED")))],
        [pr(mergeable="CONFLICTING")],
        [
            pr(number=3, created="2026-08-02T03:00:00Z"),
            pr(number=1, created="2026-08-02T01:00:00Z"),
            pr(number=2, created="2026-08-02T02:00:00Z"),
        ],
        [pr(number=7) | {"statusCheckRollup": [IN_PROGRESS_CHECK]}],
        [pr(number=8) | {"statusCheckRollup": [LEGACY_STATUS]}],
        [pr(number=9) | {"statusCheckRollup": None}],
        [{k: v for k, v in pr(number=10).items() if k != "mergeable"}],
        [{k: v for k, v in pr(number=11).items() if k != "author"}],
    ],
)
def test_the_workflow_reaches_the_same_verdict(listing) -> None:
    """Change the rule in automerge.py, change it in genesis-merge.yml."""
    assert _workflow_verdict(listing) == _python_verdict(listing)


def test_the_workflow_asks_github_for_the_same_fields() -> None:
    """A field dropped from one `--json` list makes that side blind to a
    condition the other still enforces, and the differential test above catches
    that only if a fixture happens to exercise it. Compare the lists directly,
    and without needing jq, so this one guard holds even where jq is missing."""
    import inspect

    field_list = re.compile(r'"([\w,]*statusCheckRollup[\w,]*)"')
    python = field_list.search(inspect.getsource(automerge.ready_to_merge))
    workflow = re.search(r"--json\s+([\w,]+)", MERGE_WORKFLOW.read_text())
    assert python and workflow, "could not locate a --json field list on both sides"
    assert set(python.group(1).split(",")) == set(workflow.group(1).split(","))
