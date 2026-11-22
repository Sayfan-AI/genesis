"""Behaviour tests for templates/scripts/upstream.sh (genesis issue #155).

The script exists because filing an escalation looked like it worked while it
didn't. A project's App can open an issue on genesis but can't label it, so
seven escalations from sagrin/sweatless landed outside the `needs:evolver`
queue and nothing reported it. Each test runs the script against a stubbed `gh`
that can refuse the label, and checks what the script tells the agent and the
local issue.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]
UPSTREAM_SH = ROOT / "templates" / "scripts" / "upstream.sh"
BASH = "/bin/bash"  # macOS's bash 3.2, which is what `genesis serve` runs
URL = "https://github.com/Sayfan-AI/genesis/issues/200"

FAKE_GH = """#!/bin/sh
echo "$*" >> "$GH_CALLS"
prev=""
for a in "$@"; do
  [ "$prev" = "--body" ] && printf '%s\\n===\\n' "$a" >> "$GH_COMMENTS"
  prev="$a"
done
case "$1 $2" in
  "issue create")
    [ -n "${GH_CREATE_FAILS-}" ] && { echo "HTTP 403" >&2; exit 1; }
    cat > "$GH_BODY"
    echo "Creating issue in Sayfan-AI/genesis"
    echo "$GH_URL" ;;
  "issue edit")
    if [ -n "${GH_LABEL_FAILS-}" ]; then
      echo "GraphQL: Resource not accessible by integration (addLabelsToLabelable)" >&2
      exit 1
    fi
    touch "$GH_LABELLED" ;;
  "issue view")
    [ -n "${GH_VIEW_FAILS-}" ] && exit 1
    [ -f "$GH_LABELLED" ] && echo "needs:evolver"
    exit 0 ;;
  "repo view")
    echo "o/ambient" ;;
esac
exit 0
"""


@pytest.fixture
def env(tmp_path: Path) -> dict:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    body = tmp_path / "finding.md"
    body.write_text("## What went wrong\n\nA thing broke.")
    return {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "GH_REPO": "o/proj",
        "GH_URL": URL,
        "GH_CALLS": str(tmp_path / "calls"),
        "GH_COMMENTS": str(tmp_path / "comments"),
        "GH_BODY": str(tmp_path / "body"),
        "GH_LABELLED": str(tmp_path / "labelled"),
        "_FINDING": str(body),
    }


def _run(env: dict, *extra: str, local: str | None = "7") -> subprocess.CompletedProcess:
    args = [BASH, str(UPSTREAM_SH), "--title", "A finding", "--body-file", env["_FINDING"]]
    if local:
        args += ["--local", local]
    args += list(extra)
    return subprocess.run(args, env=env, capture_output=True, text=True)


def _read(env: dict, key: str) -> str:
    p = Path(env[key])
    return p.read_text() if p.exists() else ""


def test_labelled_escalation_reports_url_and_notes_local_issue(env):
    r = _run(env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == URL
    assert r.stderr == ""
    calls = _read(env, "GH_CALLS")
    assert "issue create -R Sayfan-AI/genesis" in calls
    assert f"issue edit {URL} -R Sayfan-AI/genesis --add-label needs:evolver" in calls
    comments = _read(env, "GH_COMMENTS")
    assert URL in comments and "labelled" in comments and "unlabelled" not in comments


def test_body_carries_the_footer_naming_the_source(env):
    _run(env)
    body = _read(env, "GH_BODY")
    assert body.startswith("## What went wrong")
    assert body.rstrip().endswith(
        "Escalated with .genesis/scripts/upstream.sh from o/proj issue #7."
    )


def test_refused_label_is_reported_but_not_a_failure(env):
    """The sweatless case: the issue is filed, the label is refused."""
    env["GH_LABEL_FAILS"] = "1"
    r = _run(env)
    # Exit 0 and the URL: a non-zero exit invites a retry, which files a twin.
    assert r.returncode == 0
    assert r.stdout.strip() == URL
    assert "didn't stick" in r.stderr
    assert "Resource not accessible by integration" in r.stderr
    assert "don't file it again" in r.stderr
    comments = _read(env, "GH_COMMENTS")
    assert "**unlabelled**" in comments
    assert "Resource not accessible by integration" in comments
    assert _read(env, "GH_CALLS").count("issue create") == 1


def test_label_that_cannot_be_read_back_counts_as_missing(env):
    """An edit that exits 0 isn't proof; only the read-back is."""
    env["GH_VIEW_FAILS"] = "1"
    r = _run(env)
    assert r.returncode == 0
    assert "didn't stick" in r.stderr
    assert "**unlabelled**" in _read(env, "GH_COMMENTS")


def test_create_failure_exits_nonzero_and_labels_nothing(env):
    env["GH_CREATE_FAILS"] = "1"
    r = _run(env)
    assert r.returncode == 1
    assert "nothing was filed" in r.stderr
    calls = _read(env, "GH_CALLS")
    assert "issue edit" not in calls and "issue comment" not in calls


def test_without_local_issue_nothing_is_commented(env):
    env["GH_LABEL_FAILS"] = "1"
    r = _run(env, local=None)
    assert r.returncode == 0
    assert "issue comment" not in _read(env, "GH_CALLS")
    assert _read(env, "GH_BODY").rstrip().endswith("upstream.sh from o/proj.")


def test_project_falls_back_to_gh_repo_view(env):
    del env["GH_REPO"]
    _run(env)
    assert "from o/ambient issue #7." in _read(env, "GH_BODY")


def test_upstream_repo_is_overridable(env):
    env["GENESIS_UPSTREAM_REPO"] = "me/fork"
    _run(env)
    assert "issue create -R me/fork" in _read(env, "GH_CALLS")


def test_missing_arguments_are_a_usage_error(env):
    r = subprocess.run([BASH, str(UPSTREAM_SH), "--title", "x"], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 2
    assert not Path(env["GH_CALLS"]).exists()


def test_footer_phrase_is_what_genesis_searches_for():
    """Genesis finds unlabelled escalations by the footer. If the two drift,
    the fallback finds nothing, which looks the same as having nothing."""
    m = re.search(r'^FOOTER="([^"]+)"', UPSTREAM_SH.read_text(), re.M)
    assert m, "upstream.sh no longer defines FOOTER"
    claude_md = (ROOT / "CLAUDE.md").read_text()
    assert f'"{m.group(1)}"' in claude_md, (
        "CLAUDE.md's unlabelled-escalation search no longer matches upstream.sh's footer"
    )


def test_seeded_evolver_escalates_through_the_script():
    evolver = (ROOT / "templates" / "agents" / "evolver.md").read_text()
    assert "bash .genesis/scripts/upstream.sh" in evolver
    assert "with label `needs:evolver`" not in evolver, (
        "the seeded evolver is told to label the upstream issue by hand again, "
        "which a project's App can't do"
    )


def test_script_is_seeded():
    from genesis.scaffold import SEED_SCRIPTS
    assert "upstream.sh" in SEED_SCRIPTS
