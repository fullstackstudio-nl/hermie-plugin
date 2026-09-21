"""The registration reader, which is the one place a bad bag of JSON arrives."""

from hermie_plugin.push.registrations import (
    Section,
    read_section,
    read_sections,
    registration_of,
    someone_attached,
)


def expo_entry(**overrides):
    entry = {
        "v": 1,
        "transport": "expo",
        "token": "ExponentPushToken[abcdefghijklmnopqrstuv]",
        "platform": "ios",
        "types": {"message": True, "request": True},
        "preview": False,
        "updatedAt": 1789957143,
    }
    entry.update(overrides)
    return entry


def test_a_good_expo_entry_reads():
    parsed = registration_of("i1", expo_entry())
    assert parsed is not None
    assert parsed.transport == "expo"
    assert parsed.wants("message") is True


def test_an_absent_type_is_off():
    """A device that has never heard of a type cannot have agreed to it."""
    parsed = registration_of("i1", expo_entry())
    assert parsed.wants("turn_done") is False


def test_an_unknown_version_is_dropped():
    assert registration_of("i1", expo_entry(v=2)) is None


def test_an_entry_carrying_both_transports_is_a_confusion():
    both = expo_entry(endpoint="https://push.example/x")
    assert registration_of("i1", both) is None


def test_a_webpush_entry_needs_both_keys():
    base = {
        "v": 1,
        "transport": "webpush",
        "endpoint": "https://push.example/x",
        "platform": "web",
        "types": {"message": True},
        "updatedAt": 1,
    }
    assert registration_of("i1", {**base, "keys": {"p256dh": "a"}}) is None
    assert registration_of("i1", {**base, "keys": {"p256dh": "a", "auth": "b"}}) is not None


def test_one_bad_entry_costs_only_itself():
    section = read_section(
        {"push": {"registrations": {"good": expo_entry(), "bad": {"v": 1, "transport": "carrier-pigeon"}}}}
    )
    assert [entry.installation_id for entry in section.registrations] == ["good"]


def test_the_section_survives_anything():
    for junk in (None, 42, "push", {}, {"push": "yes"}, {"push": {"registrations": []}}):
        assert read_section(junk).registrations == []


def test_seen_is_a_heartbeat_with_a_window():
    section = Section(seen={"i1": 1000})
    assert someone_attached(section, now=1050, window_seconds=90) is True
    assert someone_attached(section, now=1200, window_seconds=90) is False


# -- one key per person ------------------------------------------------------


def bag(*installation_ids, seen=None):
    return {"push": {"registrations": {i: expo_entry() for i in installation_ids}, "seen": seen or {}}}


def test_a_registration_knows_whose_key_it_came_from():
    section = read_sections([("u1", bag("i1"))])
    assert section.registrations[0].user_id == "u1"


def test_the_legacy_key_names_nobody():
    section = read_sections([("", bag("i1"))])
    assert section.registrations[0].user_id == ""


def test_both_keys_are_read_and_the_devices_add_up():
    section = read_sections([("", bag("i1")), ("u1", bag("i2")), ("u2", bag("i3"))])
    assert [(r.installation_id, r.user_id) for r in section.registrations] == [
        ("i1", ""), ("i2", "u1"), ("i3", "u2")
    ]


def test_the_per_user_key_wins_for_the_same_device():
    """While the app writes both, the device must not be notified twice."""
    section = read_sections([("", bag("i1")), ("u1", bag("i1"))])
    assert len(section.registrations) == 1
    assert section.registrations[0].user_id == "u1"


def test_a_missing_per_user_key_costs_nothing():
    assert read_sections([]).registrations == []
    assert read_sections([("u1", None), ("u2", {"push": "yes"})]).registrations == []
