"""Behavior tests for the work-selection subcommands of templates/scripts/issues.sh.

`label`, `claim`, `next`, `release` and `sweep-claims` are what make the
`in-progress` label trustworthy, and the board is the only place the loop, the
human and `summary` look to answer "is anyone working this". Every one of them had
been verified once by hand against a throwaway stub and then left unguarded, which
is how the accumulation bug reached a shipped script in the first place.

The claim tests split by which way an error costs. For `claim` the expensive
failure is a label nobody can hand back, so those tests assert the identity is
recorded and that a claim which can't be attributed is given up. For the release
paths it inverts: the expensive failure is releasing a claim somebody is still
working, so most of `TestSweepIsABackstop` asserts what the sweep leaves alone.

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

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


TEMPLATES = Path(__file__).parents[2] / "templates"
ISSUES_SH = TEMPLATES / "scripts" / "issues.sh"

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

# `unanswered-comments` reaches GitHub through `gh api` from inside python rather
# than through `gh issue`, in two shapes: the repo-wide comment feed, and one
# thread at a time. The thread lookup is keyed out of a JSON object so a test can
# hand each thread its own state, and an absent key answers the way a real 404
# does - non-zero, which the script must read as "unknown", not "open".
if [ "$1" = "api" ]; then
  [ -n "${GH_API_FAILS-}" ] && exit 1
  case "$2" in
    */issues/comments*) printf '%s' "${GH_COMMENTS_JSON-[]}" ;;
    *) printf '%s' "${GH_THREADS_JSON-null}" | jq -e --arg n "${2##*/}" '.[$n]' ;;
  esac
  exit $?
fi

case "$1 $2" in
  "issue view")
    # `claim` reads the label back through this path.
    emit "${GH_VIEW_JSON-'{"labels":[{"name":"in-progress"}]}'}" ;;
  "issue list")
    emit "${GH_LIST_JSON-[]}" ;;
  "issue edit")
    [ -n "${GH_EDIT_FAILS-}" ] && exit 1
    echo "https://github.com/o/r/issues/1" ;;
  "issue comment")
    [ -n "${GH_COMMENT_FAILS-}" ] && exit 1
    echo "https://github.com/o/r/issues/1#issuecomment-1" ;;
esac
exit 0
"""

# What `gh issue view --json labels` returns when the claim stuck, and when it
# silently did not. The second is the lying board `claim` exists to catch.
LABEL_STUCK = '{"labels":[{"name":"in-progress"}]}'
LABEL_MISSING = '{"labels":[]}'

# The two actor shapes `unanswered-comments` has to tell apart. A GitHub App
# carries type "Bot"; a PAT-backed bot account carries neither, which is why the
# script also reads the `[bot]` login suffix.
HUMAN = {"login": "gigi", "type": "User"}
APP_BOT = {"login": "genesis-dev-bot[bot]", "type": "Bot"}
SUFFIX_BOT = {"login": "some-runner[bot]", "type": "User"}


def _iso(minutes_ago: float = 0, days_ago: float = 0) -> str:
    when = datetime.now(timezone.utc) - timedelta(days=days_ago, minutes=minutes_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _comment(num: int, user: dict = HUMAN, **age: float) -> dict:
    return {
        "issue_url": f"https://api.github.com/repos/o/r/issues/{num}",
        "created_at": _iso(**age),
        "user": user,
        "html_url": f"https://github.com/o/r/issues/{num}#issuecomment-{num}0",
    }


def _thread(
    num: int,
    title: str = "a task",
    closed_by: dict | None = None,
    pr: bool = False,
    **closed_age: float,
) -> dict:
    t: dict = {"number": num, "title": title, "state": "open"}
    if closed_by is not None:
        t.update(state="closed", closed_at=_iso(**closed_age), closed_by=closed_by)
    if pr:
        t["pull_request"] = {"url": f"https://api.github.com/repos/o/r/pulls/{num}"}
    return t


@pytest.fixture
def unanswered(run):
    """Run `unanswered-comments` over a canned comment feed and thread set."""

    def _go(*args: str, comments: list[dict], threads: list[dict], **env: str) -> str:
        proc, _ = run(
            "unanswered-comments",
            *args,
            GH_COMMENTS_JSON=json.dumps(comments),
            GH_THREADS_JSON=json.dumps({str(t["number"]): t for t in threads}),
            **env,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    return _go


def _board(*claims: tuple[int, str | None, float]) -> str:
    """What `gh issue list --label in-progress --json number,comments` returns.

    One tuple per open `in-progress` issue: number, claiming session (None for a
    label with no claim marker behind it), and how many hours ago it was claimed.
    Built as real comment JSON with real timestamps rather than as pre-filtered
    rows, because the marker regex and the age arithmetic live in the script's own
    `--jq` expression - hand-filtering here would leave both untested.
    """
    issues = []
    for number, session, age_hours in claims:
        stamp = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        comments = [{"body": "unrelated chatter", "createdAt": stamp}]
        if session is not None:
            comments.append(
                {
                    "body": (
                        f"Claimed by session `{session}`. It's released when that session "
                        f"ends without finishing. <!-- genesis-claim session={session} -->"
                    ),
                    "createdAt": stamp,
                }
            )
        issues.append({"number": number, "comments": comments})
    return json.dumps(issues)


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
                # Claim identity is read from the environment, and CI *is* a
                # GitHub Actions run - without this every claim test would pick up
                # the runner's own GITHUB_RUN_ID and pass or fail differently on a
                # laptop than on CI. Tests that want an identity set one.
                "GENESIS_SESSION": "",
                "GITHUB_RUN_ID": "",
                "GITHUB_RUN_ATTEMPT": "",
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


class TestUnansweredComments:
    """The one input no other net keys on: a person having said something.

    Detection is a single comparison - a thread's newest comment is
    human-authored - so nothing below spends long on it. The four exclusions are
    the design, because this section prints on every tick and a backstop that
    cries wolf is one the loop learns to skip. Each exclusion gets its own case,
    with the *other three* deliberately unable to fire, so a passing test names
    which rule did the work.
    """

    def test_reports_a_trailing_human_comment_on_an_open_thread(self, unanswered) -> None:
        out = unanswered(
            comments=[_comment(7, minutes_ago=90)],
            threads=[_thread(7, title="Milestone 2 plan")],
        )
        assert "#7" in out
        assert "unanswered 1h" in out
        assert "@gigi" in out
        assert 'issue "Milestone 2 plan"' in out
        # The URL is the point of the line: the reader has to be able to go and
        # read what the person actually asked for.
        assert "issuecomment-70" in out

    def test_reports_when_the_loop_closed_over_the_human(self, unanswered) -> None:
        """MaKlaude issue #141 exactly: the person spoke, then the bot closed.

        This is the shape that survives all four exclusions, and the only reason
        the closed branch exists at all - a close hides the thread from every
        other section of `summary` while the request is still unmet.
        """
        out = unanswered(
            comments=[_comment(141, minutes_ago=90)],
            threads=[_thread(141, closed_by=APP_BOT, minutes_ago=60)],
        )
        assert "#141" in out
        assert "the loop closed this over them" in out

    def test_a_bot_reply_last_is_not_reported(self, unanswered) -> None:
        """Exclusion 1. The loop has answered; nothing is waiting."""
        out = unanswered(
            comments=[_comment(7, user=APP_BOT, minutes_ago=90)],
            threads=[_thread(7)],
        )
        assert out.strip() == ""

    def test_a_bot_without_the_type_is_still_a_bot(self, unanswered) -> None:
        """Exclusion 1, via the `[bot]` login suffix rather than type "Bot".

        A PAT-backed bot account has no Bot type. Reading only the type would
        report every one of the loop's own comments back to it.
        """
        out = unanswered(
            comments=[_comment(7, user=SUFFIX_BOT, minutes_ago=90)],
            threads=[_thread(7)],
        )
        assert out.strip() == ""

    def test_outside_the_window_is_not_reported(self, unanswered) -> None:
        """Exclusion 2. Open thread, human comment - only the age excludes it."""
        out = unanswered(comments=[_comment(7, days_ago=8)], threads=[_thread(7)])
        assert out.strip() == ""

    def test_the_window_flag_widens_it(self, unanswered) -> None:
        """Proves the age is what excluded the case above, not something else."""
        out = unanswered(
            "--window-days", "30", comments=[_comment(7, days_ago=8)], threads=[_thread(7)]
        )
        assert "#7" in out

    def test_the_window_env_var_widens_it(self, unanswered) -> None:
        out = unanswered(
            comments=[_comment(7, days_ago=8)],
            threads=[_thread(7)],
            GENESIS_COMMENT_WINDOW_DAYS="30",
        )
        assert "#7" in out

    def test_a_comment_after_the_close_is_not_reported(self, unanswered) -> None:
        """Exclusion 3: the closing note - "LGTM", "signed off", "Closing."

        That is the dominant shape of a human comment trailing a thread, and the
        largest false-positive class. The closer here is a *bot*, so exclusion 4
        can't be what's doing the work.
        """
        out = unanswered(
            comments=[_comment(7, minutes_ago=10)],
            threads=[_thread(7, closed_by=APP_BOT, minutes_ago=60)],
        )
        assert out.strip() == ""

    def test_a_human_closer_is_not_reported(self, unanswered) -> None:
        """Exclusion 4: a person who comments and then closes has answered.

        The comment predates the close here, so exclusion 3 can't be what's doing
        the work - only the identity of the closer can.
        """
        out = unanswered(
            comments=[_comment(7, minutes_ago=90)],
            threads=[_thread(7, closed_by=HUMAN, minutes_ago=60)],
        )
        assert out.strip() == ""

    def test_stalest_first(self, unanswered) -> None:
        """Same order as every other report: the one going unanswered longest."""
        out = unanswered(
            comments=[_comment(8, days_ago=1), _comment(7, days_ago=3)],
            threads=[_thread(7), _thread(8)],
        )
        assert out.index("#7") < out.index("#8")

    def test_feed_order_does_not_decide_which_comment_is_newest(self, unanswered) -> None:
        """The bot's reply is newest but arrives second in the feed.

        Trusting the feed's order would take the human's comment as the newest and
        report a thread the loop has already answered - the exact way this section
        would start crying wolf.
        """
        out = unanswered(
            comments=[
                _comment(7, minutes_ago=90),
                _comment(7, user=APP_BOT, minutes_ago=30),
            ],
            threads=[_thread(7)],
        )
        assert out.strip() == ""

    def test_a_pr_thread_says_pr(self, unanswered) -> None:
        out = unanswered(
            comments=[_comment(154, minutes_ago=90)],
            threads=[_thread(154, title="add the thing", pr=True)],
        )
        assert 'PR "add the thing"' in out

    def test_empty_means_all_clear(self, unanswered) -> None:
        assert unanswered(comments=[], threads=[]).strip() == ""

    def test_an_unreadable_thread_is_not_reported(self, unanswered) -> None:
        """A 404 or a rate limit must not be guessed at in either direction."""
        out = unanswered(comments=[_comment(7, minutes_ago=90)], threads=[])
        assert out.strip() == ""

    def test_an_unreadable_api_says_so_rather_than_printing_nothing(self, run) -> None:
        """Silence here would read as all-clear, which is the failure inverted."""
        proc, _ = run("unanswered-comments", GH_API_FAILS="1")
        assert proc.returncode == 0, proc.stderr
        assert "could not be read" in proc.stdout
        assert "do NOT read" in proc.stdout


class TestSummarySection:
    """Unconditional, like the other sections: empty is the all-clear signal.

    Printing the section only when it has content makes "nothing waiting"
    indistinguishable from "this check silently stopped running", which is the
    class of invisible nothing-happened the whole command exists to close.
    """

    def test_the_section_prints_even_when_nothing_is_waiting(self, run) -> None:
        proc, _ = run("summary", GH_COMMENTS_JSON="[]")
        assert proc.returncode == 0, proc.stderr
        assert "=== Unanswered Human Comments" in proc.stdout

    def test_the_section_carries_the_finding(self, run) -> None:
        comments = json.dumps([_comment(7, minutes_ago=90)])
        threads = json.dumps({"7": _thread(7, title="Milestone 2 plan")})
        proc, _ = run("summary", GH_COMMENTS_JSON=comments, GH_THREADS_JSON=threads)
        section = proc.stdout.split("=== Unanswered Human Comments")[1]
        assert "#7" in section.split("=== Blocked")[0]

    def test_help_documents_the_command_and_its_window(self, run) -> None:
        proc, _ = run("help")
        assert "unanswered-comments" in proc.stderr
        assert "--window-days" in proc.stderr


class TestRecheckBeforeMergeIsSeeded:
    """The report only helps the *next* run; the damage happens at merge time.

    So the rule has to reach the session that is about to merge, in **both**
    execution modes. Under `genesis serve` every `genesis-*` workflow is disabled,
    so a rule carried only by a workflow prompt reaches nobody there. `CLAUDE.md`
    and `.claude/agents/*.md` are the two carriers that survive both modes, which
    is why these assert on those templates and not on a workflow.
    """

    @pytest.mark.parametrize(
        "template",
        [
            "agents/orchestrator.md",
            "agents/evolver.md",
            "claude_md.md.j2",
        ],
    )
    def test_the_carrier_tells_the_session_to_re_check(self, template: str) -> None:
        text = (TEMPLATES / template).read_text()
        assert "issues.sh unanswered-comments" in text
        assert "before" in text.lower()

    def test_claude_md_exempts_the_narrow_class_by_classification(self) -> None:
        """Not by a second list: a narrow runner is exempt by being classified,
        never by someone remembering to add it somewhere."""
        text = (TEMPLATES / "claude_md.md.j2").read_text()
        assert "narrow-class runner is exempt" in text
        assert "by\nits classification" in text or "by its classification" in text


class TestClaimRecordsWhoClaimed:
    """A claim with no identity is a claim nothing can hand back (#48).

    `in-progress` looks identical whether a live session applied it four seconds
    ago or a killed one applied it an hour ago. The marker comment is what turns
    "somebody has this" into "session X has had this since 02:18", and every
    release path keys off it.
    """

    def test_the_claiming_session_is_recorded_on_the_issue(self, run) -> None:
        proc, calls = run(
            "claim", "--id", "42", GH_VIEW_JSON=LABEL_STUCK, GENESIS_SESSION="serve-abc123"
        )
        assert proc.returncode == 0, proc.stderr
        marker = [c for c in calls if c.startswith("issue comment 42")]
        assert marker, f"claim recorded no identity: {calls}"
        assert "<!-- genesis-claim session=serve-abc123 -->" in marker[0]

    def test_the_marker_ends_the_body_so_the_prose_still_renders(self, run) -> None:
        """A line that *starts* with `<!--` opens a markdown HTML block, and the
        text after the `-->` then renders as raw source instead of a sentence."""
        _, calls = run(
            "claim", "--id", "42", GH_VIEW_JSON=LABEL_STUCK, GENESIS_SESSION="serve-abc123"
        )
        body = next(c for c in calls if c.startswith("issue comment 42"))
        assert body.rstrip().endswith("-->")
        assert not body.split("--body ", 1)[1].startswith("<!--")

    def test_actions_runs_are_identified_by_their_run_id(self, run) -> None:
        """GitHub Actions has no continuation ladder, but it does have a run id,
        which is the handle the backstop sweep needs to name what it released."""
        _, calls = run(
            "claim", "--id", "42", GH_VIEW_JSON=LABEL_STUCK,
            GITHUB_RUN_ID="99887", GITHUB_RUN_ATTEMPT="2",
        )
        body = next(c for c in calls if c.startswith("issue comment 42"))
        assert "session=gha-99887-2" in body

    def test_an_identityless_caller_still_records_something(self, run) -> None:
        """`unattributed` is a worse claim than a named one, but it is still
        datable, which is what keeps it reachable by the sweep."""
        _, calls = run("claim", "--id", "42", GH_VIEW_JSON=LABEL_STUCK)
        body = next(c for c in calls if c.startswith("issue comment 42"))
        assert "session=unattributed" in body

    def test_a_claim_it_cannot_attribute_is_given_back(self, run) -> None:
        """The label stuck but the marker didn't. Holding it would park the issue
        with nothing able to release it - `release --session` can't match it and
        the sweep can't date it - which is the exact failure #48 is about."""
        proc, calls = run(
            "claim", "--id", "42", GH_VIEW_JSON=LABEL_STUCK,
            GENESIS_SESSION="serve-abc123", GH_COMMENT_FAILS="1",
        )
        assert proc.returncode == 1
        assert "could not record who claimed" in proc.stderr
        assert "issue edit 42 --remove-label in-progress" in calls
        assert "claimed #42" not in proc.stdout


class TestReleaseHandsClaimsBack:
    """The layer that keys on the ladder's decision rather than on age.

    Reproduces MaKlaude issue #195: claimed at 02:18, session killed at 03:33
    with a clean tree and no branch, and `next --milestone 6` skipped the issue
    until a human removed the label by hand.
    """

    def test_releases_only_the_issues_that_session_claimed(self, run) -> None:
        board = _board((40, "serve-dead", 0.5), (41, "serve-alive", 0.5))
        proc, calls = run(
            "release", "--session", "serve-dead",
            "--reason", "the session deadline was reached",
            GH_LIST_JSON=board,
        )
        assert proc.returncode == 0, proc.stderr
        assert "issue edit 40 --remove-label in-progress" in calls
        assert "issue edit 41 --remove-label in-progress" not in calls
        assert "released #40" in proc.stdout

    def test_says_on_the_issue_why_the_claim_ended(self, run) -> None:
        """Without this a human sees a label vanish and can't tell a deliberate
        release from an agent quietly un-labelling something."""
        board = _board((40, "serve-dead", 0.5))
        _, calls = run(
            "release", "--session", "serve-dead",
            "--reason", "the session deadline was reached",
            GH_LIST_JSON=board,
        )
        comment = next(c for c in calls if c.startswith("issue comment 40"))
        assert "the session deadline was reached" in comment

    def test_a_session_that_claimed_nothing_releases_nothing(self, run) -> None:
        """The common case by far: most chains end holding no claim at all, and
        that has to cost one query and no writes."""
        board = _board((40, "serve-other", 0.5))
        proc, calls = run("release", "--session", "serve-dead", GH_LIST_JSON=board)
        assert proc.returncode == 0, proc.stderr
        assert not any("--remove-label" in c for c in calls)

    def test_only_the_most_recent_claim_on_an_issue_counts(self, run) -> None:
        """Claimed, released, re-claimed. Matching the first marker would release
        the live claim of whoever holds it now."""
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        board = json.dumps([
            {
                "number": 40,
                "comments": [
                    {"body": "<!-- genesis-claim session=serve-old -->", "createdAt": stamp},
                    {"body": "<!-- genesis-claim session=serve-new -->", "createdAt": stamp},
                ],
            }
        ])
        proc, calls = run("release", "--session", "serve-old", GH_LIST_JSON=board)
        assert proc.returncode == 0, proc.stderr
        assert not any("--remove-label" in c for c in calls), (
            "released a claim that had already been superseded"
        )

    def test_a_single_issue_can_be_handed_back_by_number(self, run) -> None:
        """For an agent abandoning work deliberately - blocked, wrong milestone,
        wrong pick - which is otherwise done by bare label surgery."""
        proc, calls = run("release", "--id", "40", "--reason", "blocked on a dependency")
        assert proc.returncode == 0, proc.stderr
        assert "issue edit 40 --remove-label in-progress" in calls

    def test_neither_target_is_a_usage_error(self, run) -> None:
        proc, calls = run("release")
        assert proc.returncode == 1
        assert calls == []

    def test_both_targets_is_a_usage_error(self, run) -> None:
        """--id and --session mean different scopes; guessing which one was meant
        would either miss claims or release ones nobody asked about."""
        proc, calls = run("release", "--id", "40", "--session", "serve-abc")
        assert proc.returncode == 1
        assert calls == []

    def test_a_failed_removal_fails_the_command(self, run) -> None:
        proc, _ = run("release", "--id", "40", GH_EDIT_FAILS="1")
        assert proc.returncode == 1


class TestSweepIsABackstop:
    """Age releases a claim only where nothing else can (#48).

    The sweep exists for the control plane that is SIGKILLed and the Actions run
    the runner cancels: neither reaches a decision, so neither releases anything.
    Every rule here is about not releasing too much.
    """

    def test_releases_a_claim_older_than_the_window(self, run) -> None:
        board = _board((40, "serve-gone", 9))
        proc, calls = run("sweep-claims", "--older-than", "2", GH_LIST_JSON=board)
        assert proc.returncode == 0, proc.stderr
        assert "issue edit 40 --remove-label in-progress" in calls
        comment = next(c for c in calls if c.startswith("issue comment 40"))
        assert "serve-gone" in comment and "9.0h" in comment

    def test_a_healthy_in_flight_claim_is_never_released(self, run) -> None:
        """The expensive false positive, and the reason age is the second layer
        rather than the first.

        Two workers on one issue produce two branches and a merge conflict with
        neither run aware of the other, which is strictly worse than the
        invisibility the sweep is fixing. A claim minutes old belongs to a session
        that is very probably still typing.
        """
        board = _board((40, "serve-working", 0.25), (41, "serve-working", 1.5))
        proc, calls = run("sweep-claims", "--older-than", "2", GH_LIST_JSON=board)
        assert proc.returncode == 0, proc.stderr
        assert not any("--remove-label" in c for c in calls), (
            f"the sweep released a live claim: {calls}"
        )
        assert proc.stdout.strip() == "", "a sweep with nothing to do must stay quiet"

    def test_never_releases_the_callers_own_claim(self, run) -> None:
        """A long-running session that sweeps the board must not hand away the
        issue it is at that moment working on. Age can't rule that out - the
        session may legitimately have held the claim for hours - but identity can.
        """
        board = _board((40, "serve-me", 40))
        proc, calls = run(
            "sweep-claims", "--older-than", "2",
            GH_LIST_JSON=board, GENESIS_SESSION="serve-me",
        )
        assert proc.returncode == 0, proc.stderr
        assert not any("--remove-label" in c for c in calls)

    def test_an_anonymous_sweeper_exempts_nothing(self, run) -> None:
        """The self-exemption keys on a real identity. A caller with none must not
        collapse into exempting every `unattributed` claim, which is the set most
        in need of sweeping."""
        board = _board((40, "unattributed", 40))
        proc, calls = run("sweep-claims", "--older-than", "2", GH_LIST_JSON=board)
        assert proc.returncode == 0, proc.stderr
        assert "issue edit 40 --remove-label in-progress" in calls

    def test_an_in_progress_label_with_no_marker_is_left_alone(self, run) -> None:
        """Nothing dates it, so nothing can say whether it is stale. Guessing is
        the race this design refuses; `release --id N` is the hatch."""
        board = _board((40, None, 99))
        proc, calls = run("sweep-claims", "--older-than", "2", GH_LIST_JSON=board)
        assert proc.returncode == 0, proc.stderr
        assert not any("--remove-label" in c for c in calls)

    def test_a_window_shorter_than_a_session_is_refused(self, run) -> None:
        """A sub-hour window races the control plane's own session cap. Refusing
        it is the difference between a backstop and an expiry."""
        proc, calls = run("sweep-claims", "--older-than", "0.25")
        assert proc.returncode == 1
        assert "shorter than a session" in proc.stderr
        assert calls == []

    def test_the_default_window_clears_the_session_cap(self, run) -> None:
        """Called with no flag - by the scaffolded Actions workflow, among others -
        the default has to be safe on its own."""
        board = _board((40, "serve-gone", 1.5))
        proc, calls = run("sweep-claims", GH_LIST_JSON=board)
        assert proc.returncode == 0, proc.stderr
        assert not any("--remove-label" in c for c in calls), (
            "the default window is inside the one-hour session cap"
        )
