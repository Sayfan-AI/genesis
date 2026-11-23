"""Unit tests for direct `/issues/comments` polling and `/events` deduplication."""

from __future__ import annotations

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from genesis import comments, server
from genesis.comments import CommentPollResult


@pytest.fixture(autouse=True)
def isolate_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    yield tmp_path


@pytest.fixture
def plane() -> server.LocalControlPlane:
    return server.LocalControlPlane(repo="sagrin/alpha", poll_interval=1, session_timeout=5)


def _comment(
    cid: int,
    login: str = "delirium-64",
    *,
    user_type: str = "User",
    issue_number: int = 24,
    body: str = "Clarify unsaved work recovery.",
) -> dict:
    return {
        "id": cid,
        "issue_url": f"https://api.github.com/repos/sagrin/alpha/issues/{issue_number}",
        "html_url": f"https://github.com/sagrin/alpha/issues/{issue_number}#issuecomment-{cid}",
        "body": body,
        "user": {"login": login, "type": user_type},
    }


def _fake_urlopen(payload: list[dict], etag: str = '"c-etag"') -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.headers.get.side_effect = lambda k, default=None: etag if k == "ETag" else default
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return cm


def test_fetch_comments_sends_etag_and_parses_page() -> None:
    seen_req = []

    def fake_open(req, timeout=30):
        seen_req.append((req, timeout))
        return _fake_urlopen([_comment(7111845513)], etag='"next-etag"')

    with patch("urllib.request.urlopen", side_effect=fake_open):
        res = comments.fetch_comments("sagrin/alpha", etag='"prev-etag"', token="tok")

    assert res.not_modified is False
    assert res.etag == '"next-etag"'
    assert [c["id"] for c in res.comments] == [7111845513]
    req, _ = seen_req[0]
    assert req.full_url.endswith("/repos/sagrin/alpha/issues/comments?sort=created&direction=desc&per_page=100")
    assert req.get_header("If-none-match") == '"prev-etag"'


def test_fetch_comments_handles_304_and_reraises_500() -> None:
    not_mod = urllib.error.HTTPError(url="x", code=304, msg="Not Modified", hdrs=None, fp=None)
    with patch("urllib.request.urlopen", side_effect=not_mod):
        res = comments.fetch_comments("sagrin/alpha", etag='"same"', token="tok")
    assert res == CommentPollResult(comments=[], etag='"same"', not_modified=True)

    boom = urllib.error.HTTPError(url="x", code=502, msg="Bad Gateway", hdrs=None, fp=None)
    with patch("urllib.request.urlopen", side_effect=boom):
        with pytest.raises(urllib.error.HTTPError):
            comments.fetch_comments("sagrin/alpha", etag=None, token="tok")


def test_filter_relevant_comments_drops_bots_script_notes_and_deleted_highwater() -> None:
    page = [
        _comment(105, "alice", issue_number=24),
        _comment(104, "sagrin-dev-bot[bot]", user_type="Bot", issue_number=24),
        _comment(103, "operator", body="Claimed.\n<!-- genesis-claim: tok -->"),
        _comment(102, "bob", issue_number=19),
        # Comment 100 (the exact high-water mark) was deleted; 99 is older than 100.
        _comment(99, "carol", issue_number=12),
    ]
    out = comments.filter_relevant_comments(page, last_comment_id="100")
    assert [e["id"] for e in out] == ["comment-102", "comment-105"]
    assert [e["payload"]["issue"]["number"] for e in out] == [19, 24]
    assert out[1]["actor"]["login"] == "alice"


def test_poll_once_catches_comment_when_events_feed_returns_304(plane) -> None:
    """Alpha issue #24: `/events` stayed 304 while `/issues/comments` had 7111845513."""
    plane.last_event_id = "21051561070"
    plane.last_comment_id = "7090426316"

    events_304 = server.PollResult(events=[], etag='"ev-old"', not_modified=True)
    comments_200 = CommentPollResult(
        comments=[
            _comment(7111845513, "delirium-64", issue_number=24),
            _comment(7090426316, "sagrin-dev-bot[bot]", user_type="Bot", issue_number=24),
        ],
        etag='"cm-new"',
        not_modified=False,
    )
    with (
        patch.object(server, "fetch_events", return_value=events_304),
        patch.object(server, "fetch_comments", return_value=comments_200),
    ):
        emitted = plane.poll_once("tok")

    assert len(emitted) == 1
    ev = emitted[0]
    assert ev["type"] == "IssueCommentEvent"
    assert ev["actor"]["login"] == "delirium-64"
    assert ev["payload"]["issue"]["number"] == 24
    assert ev["payload"]["comment"]["id"] == 7111845513
    assert plane.last_comment_id == "7111845513"
    assert plane.comment_etag == '"cm-new"'

    prompt = server._build_prompt(ev)
    assert "- Event: IssueCommentEvent / created" in prompt
    assert "- Actor: delirium-64" in prompt
    assert "- Issue: #24" in prompt
    assert "issuecomment-7111845513" in prompt


def test_poll_once_deduplicates_same_tick_and_lagging_events_feed(plane) -> None:
    plane.last_event_id = "50"
    plane.last_comment_id = "900"

    # Case 1: both feeds report comment 901 on the same tick -> emitted once.
    event_901 = {
        "id": "51",
        "type": "IssueCommentEvent",
        "actor": {"login": "delirium-64"},
        "payload": {"action": "created", "issue": {"number": 24}, "comment": {"id": 901}},
    }
    with (
        patch.object(
            server,
            "fetch_events",
            return_value=server.PollResult(events=[event_901], etag='"e1"', not_modified=False),
        ),
        patch.object(
            server,
            "fetch_comments",
            return_value=CommentPollResult(
                comments=[_comment(901), _comment(900)], etag='"c1"', not_modified=False
            ),
        ),
    ):
        first = plane.poll_once("tok")
    assert [e["id"] for e in first] == ["51"]
    assert plane.last_comment_id == "901"

    # Case 2: `/issues/comments` sees comment 902 on tick 2, then `/events` lags
    # and delivers `IssueCommentEvent/created` for 902 on tick 3 -> still emitted once.
    with (
        patch.object(
            server,
            "fetch_events",
            return_value=server.PollResult(events=[], etag='"e1"', not_modified=True),
        ),
        patch.object(
            server,
            "fetch_comments",
            return_value=CommentPollResult(
                comments=[_comment(902), _comment(901)], etag='"c2"', not_modified=False
            ),
        ),
    ):
        second = plane.poll_once("tok")
    assert [e["id"] for e in second] == ["comment-902"]
    assert plane.last_comment_id == "902"

    late_created = {
        "id": "52",
        "type": "IssueCommentEvent",
        "actor": {"login": "delirium-64"},
        "payload": {"action": "created", "issue": {"number": 24}, "comment": {"id": 902}},
    }
    edited_902 = {
        "id": "53",
        "type": "IssueCommentEvent",
        "actor": {"login": "delirium-64"},
        "payload": {"action": "edited", "issue": {"number": 24}, "comment": {"id": 902}},
    }
    with (
        patch.object(
            server,
            "fetch_events",
            return_value=server.PollResult(
                events=[edited_902, late_created], etag='"e2"', not_modified=False
            ),
        ),
        patch.object(
            server,
            "fetch_comments",
            return_value=CommentPollResult(comments=[], etag='"c2"', not_modified=True),
        ),
    ):
        third = plane.poll_once("tok")
    assert [e["id"] for e in third] == ["53"]


def test_comment_state_persistence_prime_and_reprime(plane) -> None:
    with patch.object(
        server,
        "fetch_comments",
        return_value=CommentPollResult(
            comments=[_comment(500), _comment(499)], etag='"cm-500"', not_modified=False
        ),
    ):
        plane._prime_comment_high_water_if_needed("tok")

    reloaded = server.LocalControlPlane(repo="sagrin/alpha")
    reloaded.load_state()
    assert reloaded.last_comment_id == "500"
    assert reloaded.comment_etag == '"cm-500"'

    # Stale mark (off the page, every returned comment has id > 500) reprimes cleanly.
    with patch.object(
        server,
        "fetch_comments",
        return_value=CommentPollResult(
            comments=[_comment(900), _comment(800)], etag='"cm-900"', not_modified=False
        ),
    ):
        reloaded._reprime_stale_comment_high_water("tok")
    assert reloaded.last_comment_id == "900"
    assert reloaded.comment_etag == '"cm-900"'
