"""Repository issue-comment poller for local mode.

GitHub's `/repos/{owner}/{repo}/events` feed is best-effort and can omit
`IssueCommentEvent` entries altogether: on `sagrin/alpha` issue #24 a human
comment (`id=7111845513`) never appeared on `/events`, so `genesis serve` kept
receiving `304 Not Modified` and never woke up for it.

Polling `/repos/{owner}/{repo}/issues/comments?sort=created&direction=desc`
covers both issue and pull-request conversation comments directly, supports
`ETag` / `304 Not Modified` so idle ticks spend zero rate-limit quota, and
synthesizes `IssueCommentEvent` records in the shape the control plane already
consumes. Symmetric deduplication by comment ID prevents double runs whether
`/events` delivers the same comment on the same tick, lags behind by a tick, or
drops it completely.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

COMMENT_ETAG_PATH = Path(".genesis/.comment-etag")
COMMENT_HIGHWATER_PATH = Path(".genesis/.comment-highwater")

# Matches `SCRIPT_NOTE` in `templates/scripts/issues.sh` (issue #144): comments
# stamped by control-plane scripts (`<!-- genesis-claim: ... -->`, etc.) aren't
# human conversation even when posted under an operator token.
SCRIPT_COMMENT_RE = re.compile(r"<!-- genesis-[a-z][a-z0-9-]*[ :-]")


@dataclass
class CommentPollResult:
    comments: list[dict]
    etag: str | None
    not_modified: bool


def is_bot_comment_user(user: object) -> bool:
    if not isinstance(user, dict):
        return False
    login = str(user.get("login", ""))
    return user.get("type") == "Bot" or login.endswith("[bot]") or login == "github-actions"


def is_script_comment(comment: dict) -> bool:
    return bool(SCRIPT_COMMENT_RE.search(str(comment.get("body") or "")))


def comment_reached_highwater(comment_id: str, last_comment_id: str) -> bool:
    """True when `comment_id` is at or older than `last_comment_id`.

    GitHub issue-comment IDs are monotonically increasing integers. Comparing
    numerically means deleting the exact comment that sat at `last_comment_id`
    still stops the scan cleanly instead of replaying the whole page.
    """
    if not comment_id or not last_comment_id:
        return False
    if comment_id == last_comment_id:
        return True
    if comment_id.isdigit() and last_comment_id.isdigit():
        return int(comment_id) <= int(last_comment_id)
    return False


def fetch_comments(repo: str, etag: str | None, token: str) -> CommentPollResult:
    """Fetch newest repo issue/PR comments; returns `not_modified=True` on 304."""
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues/comments?sort=created&direction=desc&per_page=100",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "genesis-local-control-plane",
        },
    )
    if etag:
        req.add_header("If-None-Match", etag)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            new_etag = resp.headers.get("ETag")
            body = resp.read()
            data = json.loads(body) if body else []
            comments = [c for c in data if isinstance(c, dict)] if isinstance(data, list) else []
            return CommentPollResult(comments=comments, etag=new_etag, not_modified=False)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return CommentPollResult(comments=[], etag=etag, not_modified=True)
        raise


def seen_event_comment_ids(events: Iterable[dict]) -> set[str]:
    """Comment IDs already carried by `IssueCommentEvent` records in `events`."""
    seen: set[str] = set()
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "IssueCommentEvent":
            continue
        comment = (event.get("payload") or {}).get("comment")
        if isinstance(comment, dict) and comment.get("id") is not None:
            seen.add(str(comment["id"]))
    return seen


def drop_already_polled_comment_events(
    events: list[dict], last_comment_id: str | None
) -> list[dict]:
    """Drop late `IssueCommentEvent/created` entries already seen via `/issues/comments`.

    When `/issues/comments` observes a comment on tick N and `/events` delivers
    the corresponding `IssueCommentEvent` on tick N+1, `last_comment_id` has
    already moved past that comment ID. Filtering here prevents a second run.
    """
    if last_comment_id is None:
        return events
    kept: list[dict] = []
    for event in events:
        payload = event.get("payload") or {}
        comment = payload.get("comment")
        cid = str(comment.get("id")) if isinstance(comment, dict) and comment.get("id") is not None else ""
        if (
            event.get("type") == "IssueCommentEvent"
            and payload.get("action") == "created"
            and cid
            and comment_reached_highwater(cid, last_comment_id)
        ):
            continue
        kept.append(event)
    return kept


def comment_to_event(comment: dict) -> dict:
    cid = comment.get("id")
    tail = str(comment.get("issue_url") or "").rsplit("/", 1)[-1]
    issue = {"number": int(tail)} if tail.isdigit() else {}
    return {
        "id": f"comment-{cid}",
        "type": "IssueCommentEvent",
        "actor": {"login": str((comment.get("user") or {}).get("login", ""))},
        "payload": {
            "action": "created",
            "issue": issue,
            "comment": {
                "id": cid,
                "html_url": str(comment.get("html_url") or ""),
            },
        },
    }


def filter_relevant_comments(
    comments: list[dict],
    last_comment_id: str | None,
    seen_comment_ids: set[str] | None = None,
) -> list[dict]:
    """Convert unseen human comments (newest-first input) into chronological events."""
    seen = seen_comment_ids or set()
    synthesized: list[dict] = []
    for comment in comments:
        raw_id = comment.get("id")
        if raw_id is None:
            continue
        cid = str(raw_id)
        if last_comment_id is not None and comment_reached_highwater(cid, last_comment_id):
            break
        if cid in seen:
            continue
        if is_bot_comment_user(comment.get("user")) or is_script_comment(comment):
            continue
        synthesized.append(comment_to_event(comment))
    synthesized.reverse()
    return synthesized
