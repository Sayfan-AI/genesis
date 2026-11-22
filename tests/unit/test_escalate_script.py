"""Behaviour tests for templates/scripts/escalate.sh.

This script is the one path in a dev system that runs after everything else has
already failed, so "it looked right when I read it" is not a standard it can be
held to. Every test here executes it against a stubbed `gh` and asserts on the
calls it makes.

Three properties are worth the execution cost:

1. **It must reach a human even when its own inputs are broken.** A lookup that
   fails has to produce a duplicate escalation, never silence — the opposite of
   the trade every other script in here makes.
2. **It must work on a repo that has never seen these labels.** `gh` resolves
   label names to IDs and fails the whole call on an unknown one, and a freshly
   scaffolded repo has neither `needs:human` nor `automation:failure`. The agent
   paths survive that by retrying; this one can't improvise.
3. **It must survive `set -u` on bash 3.2**, which is what macOS ships and
   therefore what `genesis serve` runs. The date arithmetic is the specific
   hazard: GNU `date -d` and BSD `date -v` reject each other's flags, so a
   script written against one silently only works in one of the two modes.
"""

import json
import os
from datetime import datetime, timedelta, timezone
import subprocess
from pathlib import Path

import pytest


TEMPLATES = Path(__file__).parents[2] / "templates"
ESCALATE_SH = TEMPLATES / "scripts" / "escalate.sh"

# Not `/usr/bin/env bash`: the point is to run under the oldest bash this system
# is expected to work on, which on macOS is /bin/bash 3.2.
BASH = "/bin/bash"

FAKE_GH = """#!/bin/sh
# The first three argv entries identify the call, and none of them can contain a
# newline, so the log stays one line per call and a test can assert on ordering.
# Bodies and labels are multi-line or repeated, so they go to files of their own.
echo "$1 $2 $3" >> "$GH_CALLS"

JQ_EXPR=""
STATE_ARG="open"
prev=""
for a in "$@"; do
  [ "$prev" = "--jq" ] && JQ_EXPR="$a"
  [ "$prev" = "--body" ] && printf '%s' "$a" > "$GH_BODY"
  [ "$prev" = "--label" ] && echo "$a" >> "$GH_LABELS"
  [ "$prev" = "--state" ] && STATE_ARG="$a"
  prev="$a"
done

# Real gh applies --jq to its own output, so this does too - the artifact query's
# jq expression is part of what's under test, and pre-filtering the fixture would
# leave exactly that part unexercised.
emit() {
  if [ -n "$JQ_EXPR" ]; then
    printf '%s' "$1" | jq -r "$JQ_EXPR"
  else
    printf '%s\\n' "$1"
  fi
}

if [ "$1" = "api" ]; then
  [ -n "${GH_API_FAILS-}" ] && exit 1
  emit "${GH_ARTIFACTS_JSON-[]}"
  exit 0
fi

case "$1 $2" in
  "label create")
    [ -n "${GH_LABEL_FAILS-}" ] && exit 1
    exit 0 ;;
  "issue list")
    [ -n "${GH_LIST_FAILS-}" ] && exit 1
    # Real gh filters on --state server-side, so a lookup that asks only for
    # open issues never sees a closed fixture.
    listing="${GH_LIST_JSON-[]}"
    if [ "$STATE_ARG" != "all" ]; then
      listing="$(printf '%s' "$listing" | jq -c --arg s "$STATE_ARG" \
        '[.[] | select((.state // "OPEN") == ($s | ascii_upcase))]')"
    fi
    emit "$listing" ;;
  "issue create")
    echo "https://github.com/o/r/issues/9" ;;
  "issue comment")
    echo "https://github.com/o/r/issues/9#issuecomment-1" ;;
  "issue reopen")
    [ -n "${GH_REOPEN_FAILS-}" ] && exit 1
    exit 0 ;;
esac
exit 0
"""

WF = "Genesis Orchestrator (Scheduled)"
MARKER = f"<!-- genesis-failure-wf: {WF} -->"
RUN_URL = "https://github.com/o/r/actions/runs/123"


def _artifact(number: int, title: str, pr: bool = False, state: str = "open") -> dict:
    item = {
        "number": number,
        "title": title,
        "state": state,
        "html_url": f"https://github.com/o/r/issues/{number}",
    }
    if pr:
        item["pull_request"] = {"url": f"https://api.github.com/repos/o/r/pulls/{number}"}
    return item


@pytest.fixture
def run(tmp_path: Path):
    """Run escalate.sh with a stubbed gh; hand back the result and what it did."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    calls = tmp_path / "calls"
    calls.touch()
    body = tmp_path / "body"
    body.touch()
    labels = tmp_path / "labels"
    labels.touch()

    def _run(**env: str):
        proc = subprocess.run(
            [BASH, str(ESCALATE_SH)],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PATH": f"{bindir}:{os.environ['PATH']}",
                "GH_CALLS": str(calls),
                "GH_BODY": str(body),
                "GH_LABELS": str(labels),
                "GH_TOKEN": "t",
                "GH_REPO": "o/r",
                "WF_NAME": WF,
                "RUN_URL": RUN_URL,
                # The runner sets neither, and a laptop might; pin them so a test
                # asserts the same thing in both places.
                "GENESIS_RUN_STARTED": "",
                "GENESIS_ARTIFACT_LOOKBACK_MIN": "",
                **env,
            },
        )
        return proc, calls.read_text().splitlines(), body.read_text(), labels.read_text().split()

    return _run


class TestItAlwaysReachesAHuman:
    """The failure this script exists for has already happened by the time it
    runs, so every branch in it has to end with someone being told."""

    def test_a_fresh_repo_gets_an_issue(self, run) -> None:
        proc, calls, body, labels = run()
        assert proc.returncode == 0, proc.stderr
        assert "issue create --title" in calls
        assert WF in body and RUN_URL in body

    def test_both_labels_are_created_before_the_issue_that_needs_them(self, run) -> None:
        """`gh issue create --label` resolves names to IDs and fails the whole
        call on one that doesn't exist, and a scaffolded repo has neither label.

        Asserted as an ordering, not as presence: creating the labels after the
        issue is exactly as broken as not creating them at all, and reads fine.
        """
        _, calls, _, labels = run()
        for label in ("needs:human", "automation:failure"):
            assert f"label create {label}" in calls, (
                f"escalate.sh applies {label} without ensuring it exists; on a "
                "fresh repo gh fails the whole `issue create` call"
            )
            assert calls.index(f"label create {label}") < calls.index(
                "issue create --title"
            ), f"{label} is created after the issue that needs it, which is no help"
        assert set(labels) == {"needs:human", "automation:failure"}

    def test_a_repo_that_already_has_the_labels_still_escalates(self, run) -> None:
        """`gh label create` on an existing label exits non-zero, and under
        `set -e` that would take the whole escalation down on every repo after
        the first failure it ever handles."""
        proc, calls, body, _ = run(GH_LABEL_FAILS="1")
        assert proc.returncode == 0, proc.stderr
        assert "issue create --title" in calls

    def test_a_broken_lookup_files_a_duplicate_rather_than_nothing(self, run) -> None:
        """Every other script here fails toward doing less. This one fails toward
        telling someone: a duplicate escalation is noise, a missing one is the
        outage that issue #27 was filed for."""
        proc, calls, _, _ = run(GH_LIST_FAILS="1")
        assert proc.returncode == 0, proc.stderr
        assert "issue create --title" in calls

    def test_an_unreadable_artifact_query_does_not_stop_the_escalation(self, run) -> None:
        proc, calls, body, _ = run(GH_API_FAILS="1")
        assert proc.returncode == 0, proc.stderr
        assert "issue create --title" in calls
        assert "Nothing was touched since" in body

    @pytest.mark.parametrize("missing", ["GH_TOKEN", "GH_REPO"])
    def test_it_refuses_to_run_without_the_inputs_it_needs(self, run, missing: str) -> None:
        """The one case where failing loudly beats carrying on: with no token
        there is nobody to tell, and a step that exits 0 having told nobody is
        indistinguishable in the log from one that worked."""
        proc, _, _, _ = run(**{missing: ""})
        assert proc.returncode != 0
        assert missing in proc.stderr


class TestDedupIsPerWorkflow:
    """One open escalation per workflow: repeated failures append, and two
    workflows failing in the same window don't get conflated into one issue a
    human has to untangle and can only close for one of them."""

    def test_a_repeat_failure_comments_instead_of_filing_a_twin(self, run) -> None:
        existing = json.dumps([{"number": 42, "state": "OPEN", "body": f"earlier failure\n{MARKER}"}])
        proc, calls, body, _ = run(GH_LIST_JSON=existing)
        assert proc.returncode == 0, proc.stderr
        assert "issue comment 42" in calls
        assert not any(c.startswith("issue create") for c in calls)
        assert MARKER in body

    def test_another_workflows_open_escalation_is_not_reused(self, run) -> None:
        """The marker is what makes this per-workflow. Matching on the label
        alone reads as working right up until two workflows fail together."""
        other = json.dumps(
            [{"number": 7, "state": "OPEN", "body": "<!-- genesis-failure-wf: Genesis Evolver -->"}]
        )
        proc, calls, _, _ = run(GH_LIST_JSON=other)
        assert proc.returncode == 0, proc.stderr
        assert "issue create --title" in calls
        assert not any(c.startswith("issue comment") for c in calls)

    def test_the_marker_it_writes_is_the_one_it_searches_for(self, run) -> None:
        """Two spellings of one dedup key is a dedup key that never matches, and
        the symptom is a new issue per failure rather than an error."""
        _, _, first_body, _ = run()
        marker_line = [ln for ln in first_body.splitlines() if ln.startswith("<!--")]
        assert marker_line, f"no dedup marker in the body: {first_body}"
        _, calls, _, _ = run(GH_LIST_JSON=json.dumps([{"number": 5, "state": "OPEN", "body": marker_line[0]}]))
        assert "issue comment 5" in calls


def _ago(hours: float) -> str:
    when = datetime.now(timezone.utc) - timedelta(hours=hours)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _closed(number: int, hours_ago: float, marker: str = MARKER) -> dict:
    return {"number": number, "state": "CLOSED", "closedAt": _ago(hours_ago), "body": marker}


class TestAnEarlyCloseDoesNotFileATwin:
    """Dedup against open issues holds only while the issue stays open, and the
    orchestrator closes them. On sagrin/sweatless one was closed as "tracked in
    issue #31" while its cause was live, and the next Deploy failure 17 seconds
    later filed a twin (genesis issue #138)."""

    def test_a_recently_closed_escalation_is_reopened_and_commented(self, run) -> None:
        proc, calls, body, _ = run(GH_LIST_JSON=json.dumps([_closed(34, 0.01)]))
        assert proc.returncode == 0, proc.stderr
        assert "issue reopen 34" in calls
        assert "issue comment 34" in calls
        assert calls.index("issue reopen 34") < calls.index("issue comment 34")
        assert not any(c.startswith("issue create") for c in calls)

    def test_an_escalation_closed_outside_the_window_is_a_new_incident(self, run) -> None:
        proc, calls, _, _ = run(GH_LIST_JSON=json.dumps([_closed(34, 25)]))
        assert proc.returncode == 0, proc.stderr
        assert "issue create --title" in calls
        assert not any(c.startswith("issue reopen") for c in calls)

    def test_the_window_is_configurable(self, run) -> None:
        _, calls, _, _ = run(
            GH_LIST_JSON=json.dumps([_closed(34, 25)]),
            GENESIS_ESCALATION_REOPEN_HOURS="48",
        )
        assert "issue reopen 34" in calls

    def test_an_open_match_beats_a_closed_one(self, run) -> None:
        listing = [_closed(34, 0.01), {"number": 35, "state": "OPEN", "body": MARKER}]
        _, calls, _, _ = run(GH_LIST_JSON=json.dumps(listing))
        assert "issue comment 35" in calls
        assert not any(c.startswith("issue reopen") for c in calls)

    def test_the_most_recently_closed_match_is_the_one_reopened(self, run) -> None:
        listing = [_closed(30, 5), _closed(34, 0.5), _closed(32, 2)]
        _, calls, _, _ = run(GH_LIST_JSON=json.dumps(listing))
        assert "issue reopen 34" in calls

    def test_another_workflows_closed_escalation_is_not_reopened(self, run) -> None:
        other = _closed(7, 0.01, "<!-- genesis-failure-wf: Genesis Evolver -->")
        _, calls, _, _ = run(GH_LIST_JSON=json.dumps([other]))
        assert "issue create --title" in calls
        assert not any(c.startswith("issue reopen") for c in calls)

    def test_a_failed_reopen_files_a_new_issue_rather_than_commenting_on_a_closed_one(
        self, run
    ) -> None:
        """A comment on a closed issue nobody watches is silence, and this
        script fails toward telling someone."""
        proc, calls, _, _ = run(
            GH_LIST_JSON=json.dumps([_closed(34, 0.01)]), GH_REOPEN_FAILS="1"
        )
        assert proc.returncode == 0, proc.stderr
        assert "issue create --title" in calls
        assert not any(c.startswith("issue comment") for c in calls)


class TestItSaysWhatMayAlreadyHaveLanded:
    """"The run died" isn't the question a human has - "is the repo where it
    was?" is. A max-turns death usually happens after the deliverable landed and
    loses only the wrap-up, so an escalation that says only "run failed" costs a
    person a hunt for work that's already sitting in an open pull request."""

    def test_touched_issues_and_pull_requests_are_both_listed(self, run) -> None:
        artifacts = json.dumps(
            [_artifact(86, "add the thing", pr=True), _artifact(85, "task: add the thing")]
        )
        _, _, body, _ = run(GH_ARTIFACTS_JSON=artifacts)
        assert "PR #86 (open) — add the thing" in body
        assert "Issue #85 (open) — task: add the thing" in body

    def test_an_empty_window_says_so_instead_of_leaving_a_blank(self, run) -> None:
        _, _, body, _ = run(GH_ARTIFACTS_JSON="[]")
        assert "Nothing was touched since" in body
        assert "pushed branch with no pull request" in body

    def test_the_default_window_is_computed_on_this_platform(self, run) -> None:
        """GNU `date -d` and BSD `date -v` reject each other's flags. Whichever
        one this machine has, an unset GENESIS_RUN_STARTED must still produce a
        timestamp - a failed `date` under `set -e` would kill the escalation on
        one of the two platforms this runs on and neither on the other."""
        _, _, body, _ = run(GENESIS_ARTIFACT_LOOKBACK_MIN="30")
        assert "Nothing was touched since 20" in body, body

    def test_an_explicit_run_start_is_used_verbatim(self, run) -> None:
        _, _, body, _ = run(GENESIS_RUN_STARTED="2026-08-22T01:02:03Z")
        assert "since 2026-08-22T01:02:03Z" in body


def test_no_model_is_reachable_from_this_path() -> None:
    """The whole design constraint in one assertion.

    The failure being escalated is a run that hit `error_max_turns`, so anything
    in this path that can itself run out of turns rebuilds the bug. The obvious
    "improvement" - have a model summarise the failure before filing - is the
    one change that must never land here.
    """
    body = ESCALATE_SH.read_text().lower()
    for forbidden in ("claude", "anthropic", "--max-turns", "prompt"):
        assert forbidden not in body, (
            f"escalate.sh mentions {forbidden!r}; this path must stay deterministic"
        )
