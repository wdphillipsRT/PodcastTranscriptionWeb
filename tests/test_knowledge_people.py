"""Hosts and guests resolve to contract participants with their basis."""

from types import SimpleNamespace

import pytest

from integrations.knowledge.people import (
    GUEST_PROMPT_VERSION, INFERRED_GUEST_MIN_CONFIDENCE, guest_input_sha256, has_host, item_guests,
    parse_admin_names, participants, plain_text, source_hosts,
)


def source(feed=None, admin=None):
    return SimpleNamespace(feed_hosts=feed, admin_hosts=admin)


def item(feed=None, admin=None, title="Episode", description=None):
    return SimpleNamespace(feed_guests=feed, admin_guests=admin, title=title, description=description)


def extraction(guests, state="completed"):
    return SimpleNamespace(state=state, guests=guests)


def test_admin_hosts_win_then_feed_hosts():
    assert source_hosts(source(["Feed"], ["Admin", "Second"])) == (("Admin", "Second"), "admin_set")
    assert source_hosts(source(["Feed"], None)) == (("Feed",), "feed_declared")
    assert source_hosts(source(None, None)) == ((), None)


def test_guest_precedence_is_admin_then_feed_then_inferred():
    inferred = extraction([{"name": "Likely Guest", "confidence": 0.81}, {"name": "Unsure", "confidence": 0.3}])
    assert item_guests(item(["Feed Guest"], ["Admin Guest"]), inferred) == (("Admin Guest", "admin_set", 1),)
    assert item_guests(item(["Feed Guest"], []), inferred) == ()
    assert item_guests(item(["Feed Guest"]), inferred) == (("Feed Guest", "feed_declared", 1),)
    assert item_guests(item(), inferred) == (("Likely Guest", "inferred", 0.81),)
    assert item_guests(item(), extraction(None, state="failed")) == ()
    assert item_guests(item(), None) == ()
    assert INFERRED_GUEST_MIN_CONFIDENCE == 0.5


def test_participants_follow_the_contract_and_skip_duplicates_and_unnameable():
    people = participants(("!!!", "Ada Host", "Grace Cohost"), "feed_declared",
                          (("ada host", "feed_declared", 1), ("James Clear", "inferred", 0.92), ("???", "admin_set", 1)))
    assert people == [
        {"name": "Ada Host", "role": "host", "basis": "feed_declared", "confidence": 1, "entity_id": "person:ada-host"},
        {"name": "Grace Cohost", "role": "co_host", "basis": "feed_declared", "confidence": 1,
         "entity_id": "person:grace-cohost"},
        {"name": "James Clear", "role": "guest", "basis": "inferred", "confidence": 0.92,
         "entity_id": "person:james-clear"},
    ]
    assert has_host(people) and not has_host(participants((), None, (("James Clear", "inferred", 0.9),)))
    many = participants(tuple(f"Host {n}" for n in range(60)), "admin_set", ())
    assert len(many) == 50


@pytest.mark.parametrize("text, expected", [
    ("Ada Host\n\n  Grace   Cohost \nada host\n", ("Ada Host", "Grace Cohost")),
    ("", ()),
    ("   \n", ()),
])
def test_admin_names_are_parsed_one_per_line(text, expected):
    assert parse_admin_names(text, limit=10) == expected


@pytest.mark.parametrize("text", ["!!!", "\n".join(f"Name {n}" for n in range(11)), "x" * 10_001, None, 7])
def test_admin_names_reject_unnameable_too_many_or_invalid_input(text):
    with pytest.raises(ValueError):
        parse_admin_names(text, limit=10)


def test_plain_text_strips_markup_and_collapses_whitespace():
    assert plain_text("<p>Guest:&nbsp;<b>James Clear</b></p>\n<p>Habits</p>") == "Guest: James Clear Habits"
    assert plain_text(None) == ""


def test_guest_input_identity_covers_prompt_title_and_description_only():
    base = guest_input_sha256(item(title="Episode", description="<p>With James Clear</p>"))
    assert base == guest_input_sha256(item(title="Episode", description="With   James Clear"))
    assert base != guest_input_sha256(item(title="Episode 2", description="With James Clear"))
    assert GUEST_PROMPT_VERSION == "episode-guest-extraction/v1"
