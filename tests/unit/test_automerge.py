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
from pathlib import Path
from unittest.mock import patch

import pytest

from genesis import automerge
from genesis.scaffold import TEMPLATES_DIR


def pr(number=1, author="app/genesis-dev-bot", draft=False, mergeable="MERGEABLE",
       checks=(("CI", "SUCCESS"),), created="2026-08-02T00:00:00Z", title="a change",
       is_bot=None):
    """A pull request as `gh pr list --json ...` reports it.

    The author shape is the one `gh` emits, `{"login": "app/<slug>", "is_bot":
    true}` for an App. These fixtures used to say `genesis-dev-bot[bot]`, a login
    `gh pr list` never returns, and the suite passed while the predicate it
    tested matched no real bot pull request (genesis issue #93).
    """
    if is_bot is None:
        is_bot = author.startswith("app/")
    return {
        "number": number,
        "title": title,
        "isDraft": draft,
        "mergeable": mergeable,
        "author": {"login": author, "is_bot": is_bot},
        "createdAt": created,
        "statusCheckRollup": [{"name": n, "conclusion": c} for n, c in checks],
    }


NOT_FOUND = '{"message":"Not Found","status":"404"}gh: Not Found (HTTP 404)'


def fake_gh(listing, merges=None, protected=None, files=None, calls=None, labels=()):
    """`gh` as the sweep sees it. `protected` is the raw `.genesis/protected-paths`
    text on the default branch (None: the repo has none), `files` maps a pull
    request number to the paths it changes, and `calls` records every call."""
    merges = merges if merges is not None else []
    files = files or {}

    def _gh(args, token, timeout=60):
        if calls is not None:
            calls.append(list(args))
        if args[:2] == ["pr", "list"]:
            return 0, json.dumps(listing)
        if args[:2] == ["pr", "merge"]:
            merges.append(int(args[2]))
            return 0, "merged"
        if args[0] == "api" and args[1].endswith("/contents/.genesis/protected-paths"):
            return (1, NOT_FOUND) if protected is None else (0, protected)
        if args[0] == "api" and "/pulls/" in args[1] and args[1].endswith("/files"):
            number = int(args[1].split("/pulls/")[1].split("/")[0])
            return (0, "\n".join(files[number])) if number in files else (1, "HTTP 502")
        if args[:2] == ["pr", "view"]:
            return 0, "true" if "needs:human" in labels else "false"
        if args[:2] in (["pr", "comment"], ["pr", "edit"], ["label", "create"]):
            return 0, ""
        return 1, "unexpected"

    return _gh, merges


def test_merges_a_green_bot_pull_request() -> None:
    gh, merges = fake_gh([pr(number=42)])
    with patch.object(automerge, "_gh", gh):
        assert automerge.merge_ready("o/r", "tok", log=lambda *_: None) == [42]
    assert merges == [42]


@pytest.mark.parametrize(
    "author",
    [
        {"login": "app/genesis-dev-bot", "is_bot": True},
        {"login": "app/dependabot", "is_bot": True},
        {"login": "genesis-dev-bot[bot]"},
    ],
)
def test_a_bot_author_is_recognised_in_every_shape_it_arrives_in(author) -> None:
    """The first two are what `gh pr list` really returns. The third is REST's
    spelling, kept so input from that side still qualifies."""
    gh, merges = fake_gh([pr(number=5) | {"author": author}])
    with patch.object(automerge, "_gh", gh):
        assert automerge.merge_ready("o/r", "tok", log=lambda *_: None) == [5]


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
        if args[0] == "api" and "contents" in args[1]:
            return 1, NOT_FOUND
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
        [pr(number=12) | {"author": {"login": "app/dependabot", "is_bot": True}}],
        [pr(number=13) | {"author": {"login": "genesis-dev-bot[bot]"}}],
        [pr(number=14) | {"author": {"login": "the-gigi", "is_bot": False}}],
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


# ---------- protected paths: what an autonomous pull request can't land alone ----------

PROTECTED = """# reserved for a human
.claude/
.genesis/
src/genesis/automerge.py
"""


def _sweep(listing, **kw):
    calls: list[list[str]] = []
    gh, merges = fake_gh(listing, calls=calls, **kw)
    logs: list[str] = []
    with patch.object(automerge, "_gh", gh):
        merged = automerge.merge_ready("o/r", "tok", log=logs.append)
    return merged, merges, calls, logs


@pytest.mark.parametrize(
    "path",
    [
        ".claude/agents/evolver.md",
        ".genesis/scripts/claude-dir-guard.sh",
        ".genesis/protected-paths",
        "src/genesis/automerge.py",
    ],
)
def test_a_bot_pull_request_touching_a_protected_path_is_held_not_merged(path) -> None:
    """The guards, the agent's charter and the merge rule are what an agent with
    auto-merge could otherwise loosen for itself. Green isn't enough for those."""
    merged, merges, calls, logs = _sweep(
        [pr(number=7)], protected=PROTECTED, files={7: ["tests/x.py", path]}
    )
    assert merged == [] and merges == []
    assert ["pr", "edit", "7", "--repo", "o/r", "--add-label", "needs:human"] in calls
    comment = next(c for c in calls if c[:2] == ["pr", "comment"])
    assert path in comment[-1], "the comment must name the file that tripped it"


def test_a_rename_out_of_a_protected_path_is_held_too() -> None:
    """Moving a guard elsewhere removes it as surely as deleting it. The files
    listing carries `previous_filename`, so the old path is checked."""
    merged, _, _, _ = _sweep(
        [pr(number=7)], protected=PROTECTED, files={7: ["docs/old-guard.sh", ".genesis/scripts/host-guard.sh"]}
    )
    assert merged == []


def test_unprotected_work_still_merges_beside_a_held_pull_request() -> None:
    merged, merges, _, _ = _sweep(
        [pr(number=1), pr(number=2, created="2026-08-03T00:00:00Z")],
        protected=PROTECTED,
        files={1: [".claude/settings.json"], 2: ["src/genesis/server.py", "tests/unit/test_server.py"]},
    )
    assert merged == [2] and merges == [2]


def test_a_prefix_is_a_path_not_a_string_prefix() -> None:
    """`.genesis/` protects that directory, not `.genesis-notes.md`, and
    `src/genesis/automerge.py` protects that file, not `automerge.pyc` beside it."""
    assert automerge.protected_hit([".genesis-notes.md"], [".genesis/"]) is None
    assert automerge.protected_hit(["src/genesis/automerge.pyc"], ["src/genesis/automerge.py"]) is None
    assert automerge.protected_hit([".genesis"], [".genesis/"]) == ".genesis"


def test_a_repo_without_the_file_merges_exactly_as_before() -> None:
    """Genesis seeds no list, so every dev system keeps its current behaviour,
    and the sweep doesn't spend a files call per pull request to learn nothing."""
    merged, merges, calls, _ = _sweep([pr(number=3)], protected=None)
    assert merged == [3] and merges == [3]
    assert not any(c[0] == "api" and c[1].endswith("/files") for c in calls)


def test_an_unreadable_list_merges_nothing() -> None:
    """Fail closed. Merging on an unreadable list is merging unchecked, and the
    next tick costs nothing but a minute."""
    calls: list[list[str]] = []
    gh, merges = fake_gh([pr(number=3)], calls=calls)

    def flaky(args, token, timeout=60):
        if args[0] == "api" and "contents" in args[1]:
            return 1, "HTTP 502: Bad Gateway"
        return gh(args, token, timeout)

    with patch.object(automerge, "_gh", flaky):
        assert automerge.merge_ready("o/r", "tok", log=lambda *_: None) == []
    assert merges == []


def test_an_unreadable_file_list_leaves_that_pull_request_for_the_next_sweep() -> None:
    merged, merges, _, _ = _sweep([pr(number=9)], protected=PROTECTED, files={})
    assert merged == [] and merges == []


def test_a_held_pull_request_is_not_commented_on_every_tick() -> None:
    """The sweep runs every minute. One comment says it; sixty say nothing."""
    _, _, calls, _ = _sweep(
        [pr(number=7)], protected=PROTECTED, files={7: [".claude/x"]}, labels=("needs:human",)
    )
    assert not any(c[:2] == ["pr", "comment"] for c in calls)


def test_genesis_protects_the_rules_its_own_loop_runs_under() -> None:
    """Genesis's own list must cover what its served evolver could otherwise
    rewrite and merge alone: its charter and hooks, the guard scripts and this
    list, the workflows CI and the merge run on, and the merge rule itself."""
    text = (Path(__file__).parents[2] / ".genesis" / "protected-paths").read_text()
    prefixes = [l.split("#", 1)[0].strip() for l in text.splitlines() if l.split("#", 1)[0].strip()]
    for must in (
        ".claude/agents/evolver.md",
        ".claude/settings.json",
        ".genesis/scripts/claude-dir-guard.sh",
        ".genesis/scripts/host-guard.sh",
        ".genesis/protected-paths",
        ".github/workflows/ci.yml",
        ".github/workflows/genesis-merge.yml",
        "src/genesis/automerge.py",
    ):
        assert automerge.protected_hit([must], prefixes), f"{must} is not protected"


# ---------- the workflow's protected-path gate agrees with merge_ready ----------

STUB_GH = r'''#!/usr/bin/env python3
import json, os, sys
sc = json.loads(os.environ["SCENARIO"])
a = sys.argv[1:]
if a[:1] == ["api"] and a[1].endswith("/contents/.genesis/protected-paths"):
    if sc["protected"] is None:
        print('{"message":"Not Found","status":"404"}', end=""); print("gh: Not Found (HTTP 404)", file=sys.stderr); sys.exit(1)
    if sc["protected"] == "BROKEN":
        print("HTTP 502: Bad Gateway", file=sys.stderr); sys.exit(1)
    print(sc["protected"], end=""); sys.exit(0)
if a[:1] == ["api"] and a[1].endswith("/files"):
    n = a[1].split("/pulls/")[1].split("/")[0]
    if n not in sc["files"]:
        print("HTTP 502", file=sys.stderr); sys.exit(1)
    print("\n".join(sc["files"][n])); sys.exit(0)
if a[:2] == ["pr", "view"]:
    print("false"); sys.exit(0)
sys.exit(0)
'''


def _protected_step_script() -> str:
    text = MERGE_WORKFLOW.read_text()
    match = re.search(
        r"- name: Hold pull requests that touch protected paths\n.*?\n        run: \|\n(.*?)\n\n      - name: Squash-merge them",
        text, re.S,
    )
    assert match, "genesis-merge.yml lost its protected-path gate step"
    return "\n".join(line[10:] for line in match.group(1).splitlines())


def _workflow_keeps(numbers: list[int], scenario: dict, tmp_path) -> list[int]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "gh"
    stub.write_text(STUB_GH)
    stub.chmod(0o755)
    out = tmp_path / "out"
    out.write_text("")
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "SCENARIO": json.dumps(scenario),
        "GITHUB_OUTPUT": str(out),
        "GH_REPO": "o/r",
        "NUMBERS": ",".join(map(str, numbers)),
    }
    subprocess.run(["bash", "-c", _protected_step_script()], env=env, check=True, capture_output=True)
    line = [l for l in out.read_text().splitlines() if l.startswith("numbers=")][-1]
    kept = line.split("=", 1)[1]
    return [int(n) for n in kept.split(",") if n]


@pytest.mark.parametrize(
    "scenario",
    [
        {"protected": None, "files": {}},
        {"protected": PROTECTED, "files": {"1": ["src/genesis/server.py"], "2": [".claude/x"]}},
        {"protected": PROTECTED, "files": {"1": ["docs/a.md", ".genesis/scripts/host-guard.sh"], "2": ["README.md"]}},
        {"protected": PROTECTED, "files": {"1": [".genesis-notes.md"], "2": ["src/genesis/automerge.pyc"]}},
        {"protected": PROTECTED, "files": {"2": ["README.md"]}},
        {"protected": "BROKEN", "files": {"1": ["README.md"], "2": ["README.md"]}},
        {"protected": "# only comments\n\n", "files": {}},
    ],
)
def test_the_workflow_gate_and_merge_ready_hold_the_same_pull_requests(scenario, tmp_path) -> None:
    """Two implementations of one rule, bash on a runner and Python in the plane,
    checked by running both over the same scenarios rather than by reading them."""
    listing = [pr(number=1), pr(number=2, created="2026-08-03T00:00:00Z")]
    gh, merges = fake_gh(
        listing,
        protected=None if scenario["protected"] is None else scenario["protected"],
        files={int(k): v for k, v in scenario["files"].items()},
    )
    if scenario["protected"] == "BROKEN":
        base = gh

        def gh(args, token, timeout=60):  # noqa: F811 - the broken-list variant
            if args[0] == "api" and "contents" in args[1]:
                return 1, "HTTP 502: Bad Gateway"
            return base(args, token, timeout)

    with patch.object(automerge, "_gh", gh):
        automerge.merge_ready("o/r", "tok", log=lambda *_: None)
    assert _workflow_keeps([1, 2], scenario, tmp_path) == merges
