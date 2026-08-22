"""Behavior tests for the work-selection subcommands of templates/scripts/issues.sh.

`label`, `claim` and `next` are what make the `in-progress` label trustworthy, and
the board is the only place the loop, the human and `summary` look to answer "is
anyone working this". Every one of them had been verified once by hand against a
throwaway stub and then left unguarded, which is how the accumulation bug reached
a shipped script in the first place.

Two properties make these worth testing by execution rather than by reading:

1. The failures are silent. The pre-fix `label` removed one of two requested
   labels, printed the issue URL and exited 0, so a partial removal was
   indistinguishable from a full one.
2. They must survive `set -u` on **bash 3.2**, which is what macOS ships and
   therefore what `genesis serve` local mode runs. Expanding an EMPTY array is an
   unbound-variable error there, so the add-only and remove-only cases are the
   ones that matter, and a test that always passes both flags proves nothing.

The `gh` stub records every invocation to a log file and reads its canned replies
from the environment, so a test can make the label fail to stick and assert that
`claim` refuses to report success. It applies the script's own `--jq` expression
to that canned JSON rather than returning a pre-filtered answer, because for
`next` the selection rule *is* the jq expression - hand-filtering the fixture
would leave the part most likely to be wrong untested.
"""

import os
import subprocess
from pathlib import Path

import pytest


ISSUES_SH = Path(__file__).parents[2] / "templates" / "scripts" / "issues.sh"

# The shell the script must work under, not merely the one that happens to be on
# PATH. macOS /bin/bash is 3.2, which is the empty-array trap; CI Linux is 5.x.
BASH = "/bin/bash"

FAKE_GH = """#!/bin/sh
# Records the call, then answers from the environment. Real gh applies --jq to
# its own output, so this does too: pull the expression out of the argv and run
# the canned JSON through it.
echo "$*" >> "$GH_CALLS"

JQ_EXPR=""
prev=""
for a in "$@"; do
  [ "$prev" = "--jq" ] && JQ_EXPR="$a"
  prev="$a"
done

emit() {
  if [ -n "$JQ_EXPR" ]; then
    printf '%s' "$1" | jq -r "$JQ_EXPR"
  else
    printf '%s\\n' "$1"
  fi
}

case "$1 $2" in
  "issue view")
    # `claim` reads the label back through this path.
    emit "${GH_VIEW_JSON-'{"labels":[{"name":"in-progress"}]}'}" ;;
  "issue list")
    emit "${GH_LIST_JSON-[]}" ;;
  "issue edit")
    [ -n "${GH_EDIT_FAILS-}" ] && exit 1
    echo "https://github.com/o/r/issues/1" ;;
esac
exit 0
"""

# What `gh issue view --json labels` returns when the claim stuck, and when it
# silently did not. The second is the lying board `claim` exists to catch.
LABEL_STUCK = '{"labels":[{"name":"in-progress"}]}'
LABEL_MISSING = '{"labels":[]}'


@pytest.fixture
def run(tmp_path: Path):
    """Run issues.sh with a stubbed gh, and hand back (result, calls)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    calls = tmp_path / "calls"
    calls.touch()

    def _run(*args: str, **env: str) -> tuple[subprocess.CompletedProcess, list[str]]:
        proc = subprocess.run(
            [BASH, str(ISSUES_SH), *args],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PATH": f"{bindir}:{os.environ['PATH']}",
                "GH_CALLS": str(calls),
                **env,
            },
        )
        return proc, calls.read_text().splitlines()

    return _run


class TestLabelAccumulates:
    """The bug from MaKlaude #202: repeated flags overwrote a scalar."""

    def test_two_removes_both_removed(self, run) -> None:
        proc, calls = run(
            "label", "--id", "199", "--remove", "in-progress", "--remove", "needs:human"
        )
        assert proc.returncode == 0, proc.stderr
        assert "issue edit 199 --remove-label in-progress" in calls
        assert "issue edit 199 --remove-label needs:human" in calls

    def test_two_adds_both_added(self, run) -> None:
        proc, calls = run("label", "--id", "7", "--add", "bug", "--add", "milestone:6")
        assert proc.returncode == 0, proc.stderr
        assert "issue edit 7 --add-label bug" in calls
        assert "issue edit 7 --add-label milestone:6" in calls

    def test_add_only_does_not_trip_set_u(self, run) -> None:
        """REMOVE is empty here. Under bash 3.2 + set -u that expansion aborts."""
        proc, calls = run("label", "--id", "7", "--add", "bug")
        assert proc.returncode == 0, proc.stderr
        assert "unbound variable" not in proc.stderr
        assert calls == ["issue edit 7 --add-label bug"]

    def test_remove_only_does_not_trip_set_u(self, run) -> None:
        """The exact call #202 reported. ADD is empty."""
        proc, calls = run("label", "--id", "7", "--remove", "in-progress")
        assert proc.returncode == 0, proc.stderr
        assert "unbound variable" not in proc.stderr
        assert calls == ["issue edit 7 --remove-label in-progress"]

    def test_mixed_add_and_remove(self, run) -> None:
        proc, calls = run("label", "--id", "7", "--add", "bug", "--remove", "in-progress")
        assert proc.returncode == 0, proc.stderr
        assert len(calls) == 2

    def test_neither_flag_is_a_usage_error(self, run) -> None:
        """Exiting 0 here would report success for having done nothing."""
        proc, calls = run("label", "--id", "7")
        assert proc.returncode == 1
        assert "nothing to do" in proc.stderr
        assert calls == []

    def test_missing_id_is_a_usage_error(self, run) -> None:
        proc, calls = run("label", "--add", "bug")
        assert proc.returncode == 1
        assert calls == []

    def test_one_failing_label_fails_the_command(self, run) -> None:
        """A partial failure that exits 0 is the whole class of bug here."""
        proc, _ = run("label", "--id", "7", "--add", "bug", GH_EDIT_FAILS="1")
        assert proc.returncode == 1


class TestClaimVerifies:
    """claim exists because a model following a labeling rule is not a state machine."""

    def test_success_when_the_label_sticks(self, run) -> None:
        proc, calls = run("claim", "--id", "42", GH_VIEW_JSON=LABEL_STUCK)
        assert proc.returncode == 0, proc.stderr
        assert "claimed #42" in proc.stdout
        assert "issue edit 42 --add-label in-progress" in calls
        # The read-back is the point: without it this is just a label call.
        assert any(c.startswith("issue view 42") for c in calls)

    def test_refuses_success_when_the_label_did_not_stick(self, run) -> None:
        """gh exits 0 but the label is absent - a silently lying board."""
        proc, _ = run("claim", "--id", "42", GH_VIEW_JSON=LABEL_MISSING)
        assert proc.returncode == 1
        assert "did not stick" in proc.stderr

    def test_fails_when_the_write_itself_fails(self, run) -> None:
        proc, _ = run("claim", "--id", "42", GH_EDIT_FAILS="1")
        assert proc.returncode == 1
        assert "could not add in-progress" in proc.stderr

    def test_missing_id_is_a_usage_error(self, run) -> None:
        proc, calls = run("claim")
        assert proc.returncode == 1
        assert calls == []


class TestNextSelects:
    """Selection is a query, not a judgment, and picking is the same call as marking."""

    def test_picks_the_oldest_eligible_and_claims_it(self, run) -> None:
        listing = (
            '[{"number":50,"createdAt":"2026-08-02T00:00:00Z","labels":[]},'
            '{"number":40,"createdAt":"2026-08-01T00:00:00Z","labels":[]}]'
        )
        proc, calls = run(
            "next", "--milestone", "6", GH_LIST_JSON=listing, GH_VIEW_JSON=LABEL_STUCK
        )
        assert proc.returncode == 0, proc.stderr
        # Oldest first so nothing starves, and nothing but the number on stdout
        # so callers can do ISSUE=$(issues.sh next --milestone 6).
        assert proc.stdout.strip() == "40"
        assert "issue edit 40 --add-label in-progress" in calls

    @pytest.mark.parametrize("skip", ["blocked", "in-progress", "needs:human"])
    def test_skips_ineligible_issues(self, run, skip: str) -> None:
        listing = (
            '[{"number":40,"createdAt":"2026-08-01T00:00:00Z",'
            f'"labels":[{{"name":"{skip}"}}]}},'
            '{"number":50,"createdAt":"2026-08-02T00:00:00Z","labels":[]}]'
        )
        proc, _ = run("next", "--milestone", "6", GH_LIST_JSON=listing, GH_VIEW_JSON=LABEL_STUCK)
        assert proc.stdout.strip() == "50"

    def test_exits_3_with_no_output_when_there_is_nothing_to_work(self, run) -> None:
        """3, not 0 and not 1: an empty board is neither success nor failure.

        A caller that conflates them either does nothing forever or escalates a
        finished milestone as a crash.
        """
        proc, calls = run("next", "--milestone", "6", GH_LIST_JSON="[]")
        assert proc.returncode == 3
        assert proc.stdout.strip() == ""
        assert not any("--add-label" in c for c in calls)

    def test_exits_3_when_every_candidate_is_ineligible(self, run) -> None:
        listing = (
            '[{"number":40,"createdAt":"2026-08-01T00:00:00Z",'
            '"labels":[{"name":"blocked"}]}]'
        )
        proc, _ = run("next", "--milestone", "6", GH_LIST_JSON=listing)
        assert proc.returncode == 3

    def test_missing_milestone_is_a_usage_error_not_exit_3(self, run) -> None:
        """1 and 3 must stay distinct, or a typo looks like an empty milestone."""
        proc, _ = run("next")
        assert proc.returncode == 1

    def test_claim_failure_does_not_report_a_pick(self, run) -> None:
        """If the claim can't be verified, `next` must not hand back a number that
        the caller will treat as marked."""
        listing = '[{"number":40,"createdAt":"2026-08-01T00:00:00Z","labels":[]}]'
        proc, _ = run(
            "next", "--milestone", "6", GH_LIST_JSON=listing, GH_VIEW_JSON=LABEL_MISSING
        )
        assert proc.returncode == 1
        assert proc.stdout.strip() == ""
