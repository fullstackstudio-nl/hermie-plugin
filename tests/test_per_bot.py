"""One chat's notification switches, where they differ from the global ones.

The rule this file pins is the one the app states in
`packages/gateway-client/src/push.ts`: an override is honoured for the type it
names, a type it does not name follows the device's own switch, and a mute is
not weighed against either of them.
"""

from hermie_plugin.push import events
from hermie_plugin.push.registrations import (
    Registration,
    Section,
    effective_types,
    per_bot_of,
    read_section,
    read_sections,
)


def registration(installation_id="i1", *, types=None, user_id="u1"):
    return Registration(
        installation_id=installation_id,
        transport="expo",
        platform="ios",
        types={name: True for name in (types or events.TYPES)},
        preview=False,
        updated_at=1000,
        token="ExponentPushToken[abcdefghijklmnopqrstuv]",
        user_id=user_id,
    )


def message(bot="jurist"):
    return events.from_assistant_message(
        bot=bot, session_id="s", turn_id="t", assistant_response="hello", at=10
    )


def deliver(notification, section, **overrides):
    options = {
        "now": 1000,
        "attached_window_seconds": 90,
        "enabled_types": events.TYPES,
        "gateway_preview": "device",
        "retired": lambda installation_id, updated_at: False,
    }
    options.update(overrides)
    return events.recipients(notification, section, **options)


# -- the fold ----------------------------------------------------------------


def test_an_override_that_says_yes_turns_a_type_on():
    """The device's own switch is off and this chat's is on."""
    merged = effective_types({"message": False, "cron": False}, {"message": True})
    assert merged == {"message": True, "cron": False}


def test_an_override_that_says_no_turns_a_type_off():
    merged = effective_types({"message": True, "cron": True}, {"cron": False})
    assert merged == {"message": True, "cron": False}


def test_a_type_nobody_overrode_follows_the_global_switch():
    """And keeps following it. That is why the bag is partial rather than a copy
    of all five switches taken on the day one of them was touched."""
    assert effective_types({"message": True, "cron": False}, {"turn_done": True})["message"] is True
    assert effective_types({"message": True}, {})["message"] is True
    assert effective_types({"message": True}, None)["message"] is True


def test_an_override_that_is_not_a_boolean_is_not_an_answer():
    assert effective_types({"message": True}, {"message": "no"})["message"] is True
    assert effective_types({"message": True}, {"message": None})["message"] is True


# -- reading the section -----------------------------------------------------


def test_the_overrides_are_read_from_where_the_app_writes_them():
    found = per_bot_of({"push": {"perBot": {"jurist": {"cron": False, "message": True}}}})
    assert found == {"jurist": {"cron": False, "message": True}}


def test_a_type_outside_the_schema_is_not_an_override():
    """`dm` is gone and `cron_done` is not a switch the app offers. A bag that
    names one is read for the types that exist and no others."""
    found = per_bot_of({"push": {"perBot": {"jurist": {"dm": True, "cron": False}}}})
    assert found == {"jurist": {"cron": False}}


def test_a_chat_with_nothing_recognisable_is_dropped_rather_than_kept_empty():
    assert per_bot_of({"push": {"perBot": {"jurist": {}, "": {"cron": False}}}}) == {}
    assert per_bot_of({"push": {"perBot": {"jurist": "off"}}}) == {}


def test_an_absent_bag_is_not_an_error():
    assert per_bot_of({}) == {}
    assert per_bot_of({"push": {}}) == {}
    assert per_bot_of(None) == {}


def test_the_overrides_belong_to_the_person_whose_key_they_were_found_under():
    section = read_section({"push": {"perBot": {"jurist": {"cron": False}}}}, "u1")
    assert section.per_bot == {"u1": {"jurist": {"cron": False}}}
    assert section.overrides_for("u1", "jurist") == {"cron": False}
    assert section.overrides_for("u2", "jurist") == {}


def test_the_per_user_key_wins_over_the_legacy_one():
    """Same precedence the registrations and the mutes already follow."""
    section = read_sections(
        [
            ("", {"push": {"perBot": {"jurist": {"cron": True}}}}),
            ("u1", {"push": {"perBot": {"jurist": {"cron": False}}}}),
        ]
    )
    assert section.per_bot["u1"] == {"jurist": {"cron": False}}


# -- and what it decides -----------------------------------------------------


def test_an_override_on_delivers_a_type_the_device_switched_off():
    section = Section(
        registrations=[registration(types=["request"])],
        per_bot={"u1": {"jurist": {"message": True}}},
    )
    assert len(deliver(message(), section)) == 1


def test_an_override_off_silences_a_type_the_device_switched_on():
    section = Section(
        registrations=[registration()], per_bot={"u1": {"jurist": {"message": False}}}
    )
    assert deliver(message(), section) == []


def test_an_override_is_per_chat_and_leaves_the_other_chats_alone():
    section = Section(
        registrations=[registration()], per_bot={"u1": {"jurist": {"message": False}}}
    )
    assert len(deliver(message(bot="marketing"), section)) == 1


def test_an_absent_override_falls_back_to_the_global_switch():
    global_off = Section(registrations=[registration(types=["request"])], per_bot={})
    assert deliver(message(), global_off) == []

    global_on = Section(registrations=[registration()], per_bot={"u1": {"jurist": {"cron": False}}})
    assert len(deliver(message(), global_on)) == 1


def test_a_mute_still_wins_over_an_override_that_says_yes():
    """An override is a preference about a type; a mute is somebody saying no to
    the whole bot. The two are not weighed against each other."""
    section = Section(
        registrations=[registration(types=["request"])],
        per_bot={"u1": {"jurist": {"message": True}}},
        mutes={"u1": {"jurist": 0}},
    )
    assert deliver(message(), section) == []


def test_an_override_belongs_to_one_person_not_to_a_device():
    """Somebody who silences a chat means it on every device they registered,
    and on nobody else's."""
    section = Section(
        registrations=[
            registration("i1", user_id="u1"),
            registration("i2", user_id="u1"),
            registration("i3", user_id="u2"),
        ],
        per_bot={"u1": {"jurist": {"message": False}}},
    )
    assert [entry.installation_id for entry, _ in deliver(message(), section)] == ["i3"]


def test_the_gateways_own_switch_is_still_a_ceiling():
    """An override cannot turn on a type this gateway does not send at all."""
    section = Section(
        registrations=[registration()], per_bot={"u1": {"jurist": {"message": True}}}
    )
    assert deliver(message(), section, enabled_types=("request",)) == []
