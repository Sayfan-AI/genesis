"""Unit tests for event filtering in the local control plane."""

from genesis.server import filter_relevant_events, is_bot_actor


def make_event(
    event_id: str,
    event_type: str = "IssuesEvent",
    actor: str = "alice",
    action: str = "opened",
) -> dict:
    return {
        "id": event_id,
        "type": event_type,
        "actor": {"login": actor},
        "payload": {"action": action},
    }


def test_is_bot_actor_detects_bot_suffix() -> None:
    assert is_bot_actor("genesis-dev-bot[bot]")
    assert is_bot_actor("github-actions[bot]")
    assert is_bot_actor("github-actions")


def test_is_bot_actor_passes_humans() -> None:
    assert not is_bot_actor("alice")
    assert not is_bot_actor("the-gigi")


def test_filter_drops_bot_events() -> None:
    events = [
        make_event("3", actor="genesis-dev-bot[bot]"),
        make_event("2", actor="alice"),
        make_event("1", actor="github-actions[bot]"),
    ]
    result = filter_relevant_events(events, last_event_id=None)
    assert [e["id"] for e in result] == ["2"]


def test_filter_keeps_only_relevant_event_types() -> None:
    events = [
        make_event("5", event_type="PushEvent", actor="alice"),
        make_event("4", event_type="IssuesEvent", actor="alice"),
        make_event("3", event_type="WatchEvent", actor="alice"),
        make_event("2", event_type="IssueCommentEvent", actor="alice"),
        make_event("1", event_type="PullRequestEvent", actor="alice"),
    ]
    result = filter_relevant_events(events, last_event_id=None)
    # Returned in chronological order (oldest first)
    assert [e["id"] for e in result] == ["1", "2", "4"]


def test_filter_stops_at_high_water_mark() -> None:
    # GitHub returns events newest-first; high-water = "5" means we already saw 5,4,3
    events = [
        make_event("8", actor="alice"),
        make_event("7", actor="alice"),
        make_event("6", actor="alice"),
        make_event("5", actor="alice"),  # already processed
        make_event("4", actor="alice"),
    ]
    result = filter_relevant_events(events, last_event_id="5")
    assert [e["id"] for e in result] == ["6", "7", "8"]


def test_filter_returns_chronological_order() -> None:
    # Event 3 came in before 4 before 5. After filtering, oldest first.
    events = [
        make_event("5", actor="alice"),
        make_event("4", actor="bob"),
        make_event("3", actor="carol"),
    ]
    result = filter_relevant_events(events, last_event_id=None)
    assert [e["actor"]["login"] for e in result] == ["carol", "bob", "alice"]


def test_filter_handles_empty_input() -> None:
    assert filter_relevant_events([], last_event_id=None) == []
    assert filter_relevant_events([], last_event_id="anything") == []


def test_filter_no_high_water_returns_all_relevant() -> None:
    events = [
        make_event("3", actor="alice"),
        make_event("2", actor="bot[bot]"),
        make_event("1", actor="bob"),
    ]
    result = filter_relevant_events(events, last_event_id=None)
    assert [e["id"] for e in result] == ["1", "3"]
