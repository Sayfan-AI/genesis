"""Detecting issue claims a finished session left behind with no pull request.

`LocalControlPlane.release_claims` covers every abnormal ending: budget exhaustion
that the continuation ladder stops on, a session timeout, SIGTERM, or a CLI crash.
What escaped it on sweatless issue #103 was a session that reported `subtype ==
"success"` on stdout while its background worker died unreleased: the chain ended
"cleanly", `release_claims` did not run, no pull request existed, and `in-progress`
stayed on the issue for hours.

A chain that ends in `success` legitimately keeps an `in-progress` claim in only one
case: it opened (or updated) a pull request that is now waiting on CI and auto-merge
to close the issue. When the chain claimed an open issue under its own
`GENESIS_SESSION` marker, left that issue open, and left no open pull request
backing it, the claim no longer describes reality and can be released immediately
rather than waiting two hours for `sweep-claims`.

Every GitHub read fails open (returns `[]`): a transient API failure must fall back
to the two-hour sweep, never strip a live pull request's claim.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re

from genesis.automerge import _gh

CLAIM_MARKER_RE = re.compile(r"<!-- genesis-claim session=([^\s>]+)")


def _parse_iso(ts: str | None) -> float | None:
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def _last_claim_session(issue: dict) -> str | None:
    """Session id from the most recent `genesis-claim` comment, matching `issues.sh`."""
    found: str | None = None
    for comment in issue.get("comments") or []:
        if not isinstance(comment, dict):
            continue
        match = CLAIM_MARKER_RE.search(str(comment.get("body") or ""))
        if match:
            found = match.group(1)
    return found


def _pr_backs_issue(pr: dict, issue_number: int, chain_started_at: float) -> bool:
    """True when an open PR plausibly carries the work for `issue_number`."""
    ref_re = re.compile(rf"(?<!\w)#{issue_number}\b")
    branch_re = re.compile(rf"(?:^|[-_/]){issue_number}(?:$|[-_/])")
    title = str(pr.get("title") or "")
    body = str(pr.get("body") or "")
    branch = str(pr.get("headRefName") or "")
    if ref_re.search(title) or ref_re.search(body) or branch_re.search(branch):
        return True
    # Even if the agent forgot `#N` in the body and branch name, any open PR
    # created or updated during this chain is live in-flight work.
    for key in ("createdAt", "updatedAt"):
        stamp = _parse_iso(pr.get(key))
        if stamp is not None and stamp >= chain_started_at - 5.0:
            return True
    return False


def unbacked_claimed_issues(
    repo: str,
    session_token: str,
    chain_started_at: float,
    token: str | None,
    *,
    gh=_gh,
) -> list[int]:
    """Open `in-progress` issues claimed by `session_token` with no open PR."""
    if not session_token:
        return []
    code, out = gh(
        [
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--label",
            "in-progress",
            "--json",
            "number,comments",
            "--limit",
            "100",
        ],
        token,
    )
    if code != 0:
        return []
    try:
        issues = json.loads(out)
    except ValueError:
        return []
    if not isinstance(issues, list):
        return []

    held = [
        int(issue["number"])
        for issue in issues
        if isinstance(issue, dict)
        and isinstance(issue.get("number"), int)
        and _last_claim_session(issue) == session_token
    ]
    if not held:
        return []

    code, out = gh(
        [
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--json",
            "number,title,body,headRefName,createdAt,updatedAt",
            "--limit",
            "100",
        ],
        token,
    )
    if code != 0:
        return []
    try:
        prs = json.loads(out)
    except ValueError:
        return []
    if not isinstance(prs, list):
        return []

    return [
        num
        for num in held
        if not any(
            isinstance(pr, dict) and _pr_backs_issue(pr, num, chain_started_at)
            for pr in prs
        )
    ]
