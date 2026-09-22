"""What a hook's kwargs become, and who is told."""

from hermie_plugin.push import events
from hermie_plugin.push.cron import BY_TASK_ID, Cron
from hermie_plugin.push.registrations import Registration, Section, Seen, read_section


def registration(installation_id="i1", *, preview=False, types=None, transport="expo", user_id=""):
    return Registration(
        installation_id=installation_id,
        transport=transport,
        platform="ios",
        types={name: True for name in (types or events.TYPES)},
        preview=preview,
        updated_at=1000,
        token="ExponentPushToken[abcdefghijklmnopqrstuv]",
        user_id=user_id,
    )


def message(bot="b"):
    return events.from_assistant_message(
        bot=bot, session_id="s", turn_id="t", assistant_response="hello", at=10
    )


def never_retired(installation_id, updated_at):
    return False


def deliver(notification, section, **overrides):
    options = {
        "now": 1000,
        "attached_window_seconds": 90,
        "enabled_types": events.TYPES,
        "gateway_preview": "device",
        "retired": never_retired,
    }
    options.update(overrides)
    return events.recipients(notification, section, **options)


def test_the_payload_says_who_not_what():
    note = events.from_assistant_message(
        bot="jurist", session_id="s1", turn_id="t1", assistant_response="the contract is fine", at=10
    )
    payload = note.payload(preview=False)
    assert payload["bot"] == "jurist"
    assert payload["type"] == "message"
    assert "preview" not in payload
    assert note.rendered(preview=False) == ("jurist", "New message")


def test_preview_carries_the_text_when_the_device_asked():
    note = events.from_assistant_message(
        bot="jurist", session_id="s1", turn_id="t1", assistant_response="the contract is fine", at=10
    )
    assert note.payload(preview=True)["preview"] == "the contract is fine"
    assert note.rendered(preview=True) == ("jurist", "the contract is fine")


def test_the_gateway_setting_is_a_ceiling_not_a_floor():
    """`never` overrides a device that asked; `device` never turns one on."""
    note = events.from_assistant_message(
        bot="b", session_id="s", turn_id="t", assistant_response="hello", at=10
    )
    section = Section(registrations=[registration(preview=True)])

    _, preview = deliver(note, section, gateway_preview="device")[0]
    assert preview is True
    _, preview = deliver(note, section, gateway_preview="never")[0]
    assert preview is False

    quiet = Section(registrations=[registration(preview=False)])
    _, preview = deliver(note, quiet, gateway_preview="device")[0]
    assert preview is False


def test_a_message_is_suppressed_on_the_device_reading_that_chat():
    note = events.from_assistant_message(
        bot="b", session_id="s", turn_id="t", assistant_response="hello", at=10
    )
    watching = Section(registrations=[registration()], seen={"i1": Seen(at=990, bot="b")})
    assert deliver(note, watching) == []


def test_the_other_devices_of_the_same_person_are_still_told():
    """A phone in a pocket should buzz while the same person reads on a laptop."""
    note = events.from_assistant_message(
        bot="b", session_id="s", turn_id="t", assistant_response="hello", at=10
    )
    section = Section(
        registrations=[registration("i1", user_id="u1"), registration("i2", user_id="u1")],
        seen={"i1": Seen(at=990, bot="b")},
    )
    assert [entry.installation_id for entry, _ in deliver(note, section)] == ["i2"]


def test_a_device_reading_another_chat_is_still_told():
    note = events.from_assistant_message(
        bot="b", session_id="s", turn_id="t", assistant_response="hello", at=10
    )
    elsewhere = Section(registrations=[registration()], seen={"i1": Seen(at=990, bot="other")})
    assert len(deliver(note, elsewhere)) == 1


def test_a_heartbeat_that_names_no_chat_suppresses_every_chat_on_that_device():
    note = events.from_assistant_message(
        bot="b", session_id="s", turn_id="t", assistant_response="hello", at=10
    )
    watching = Section(registrations=[registration()], seen={"i1": Seen(at=990)})
    assert deliver(note, watching) == []


def test_a_stale_heartbeat_suppresses_nothing():
    note = events.from_assistant_message(
        bot="b", session_id="s", turn_id="t", assistant_response="hello", at=10
    )
    stale = Section(registrations=[registration()], seen={"i1": Seen(at=500, bot="b")})
    assert len(deliver(note, stale)) == 1


def test_a_request_is_never_suppressed():
    """A question with a countdown on it is worth a buzz even if a tablet is open."""
    note = events.from_approval(
        bot="b", session_key="s", description="delete the build directory", request_id="r1", turn_id="t", at=10
    )
    watching = Section(registrations=[registration()], seen={"i1": Seen(at=990, bot="b")})
    assert len(deliver(note, watching)) == 1


def test_a_cron_and_a_failed_turn_are_never_suppressed_either():
    watching = Section(registrations=[registration()], seen={"i1": Seen(at=990, bot="b")})
    failed = events.from_session_end(
        bot="b", session_id="s", turn_id="t", completed=False, failed=True, interrupted=False, at=10
    )
    assert len(deliver(failed, watching)) == 1


def test_a_type_the_gateway_switched_off_reaches_nobody():
    note = events.from_session_end(
        bot="b", session_id="s", turn_id="t", completed=True, failed=False, interrupted=False, at=10
    )
    section = Section(registrations=[registration()])
    assert deliver(note, section, enabled_types=("message",)) == []


def test_a_type_the_device_did_not_ask_for_reaches_it_anyway_never():
    note = events.from_approval(
        bot="b", session_key="s", description="d", request_id="r", turn_id="t", at=10
    )
    section = Section(registrations=[registration(types=["message"])])
    assert deliver(note, section) == []


def test_a_retired_registration_is_skipped():
    note = events.from_approval(
        bot="b", session_key="s", description="d", request_id="r", turn_id="t", at=10
    )
    section = Section(registrations=[registration()])
    assert deliver(note, section, retired=lambda i, u: True) == []


def test_an_interrupted_turn_says_nothing():
    """Somebody pressed stop. They know."""
    assert (
        events.from_session_end(
            bot="b", session_id="s", turn_id="t", completed=False, failed=False, interrupted=True, at=10
        )
        is None
    )


def test_a_failed_turn_and_a_finished_turn_are_different_types():
    done = events.from_session_end(
        bot="b", session_id="s", turn_id="t", completed=True, failed=False, interrupted=False, at=10
    )
    failed = events.from_session_end(
        bot="b", session_id="s", turn_id="t", completed=False, failed=True, interrupted=False, at=10
    )
    assert (done.type, failed.type) == ("turn_done", "turn_failed")
    # Same turn, different fact: the dedupe keys must not collide.
    assert done.event_id != failed.event_id


def test_the_same_fact_gets_the_same_id_twice():
    first = events.from_approval(
        bot="b", session_key="s", description="d", request_id="r1", turn_id="t", at=10
    )
    again = events.from_approval(
        bot="b", session_key="s", description="d", request_id="r1", turn_id="t", at=99
    )
    assert first.event_id == again.event_id


def test_an_empty_assistant_message_is_not_a_notification():
    assert (
        events.from_assistant_message(
            bot="b", session_id="s", turn_id="t", assistant_response="   ", at=10
        )
        is None
    )


# -- mutes -------------------------------------------------------------------


def muted(bot="b", until=0, user_id="u1"):
    return Section(registrations=[registration(user_id=user_id)], mutes={user_id: {bot: until}})


def test_an_active_mute_says_nothing():
    assert deliver(message(), muted(until=2000), now=1000) == []


def test_a_mute_forever_is_forever():
    """`0` is not "expired at the epoch"; it is "until I say otherwise"."""
    assert deliver(message(), muted(until=0), now=99_999_999) == []


def test_an_expired_mute_is_not_a_mute():
    """The app is not obliged to come back and tidy up a lapsed entry."""
    assert len(deliver(message(), muted(until=900), now=1000)) == 1


def test_a_mute_is_per_bot():
    section = Section(registrations=[registration(user_id="u1")], mutes={"u1": {"other": 0}})
    assert len(deliver(message(bot="b"), section, now=1000)) == 1


def test_a_mute_silences_a_request_too():
    """A mute is not a per-type switch; somebody said no to this bot."""
    note = events.from_approval(
        bot="b", session_key="s", description="d", request_id="r", turn_id="t", at=10
    )
    assert deliver(note, muted(until=0), now=1000) == []


def test_one_persons_mute_leaves_another_person_alone():
    section = Section(
        registrations=[registration("i1", user_id="u1"), registration("i2", user_id="u2")],
        mutes={"u1": {"b": 0}},
    )
    assert [entry.installation_id for entry, _ in deliver(message(), section, now=1000)] == ["i2"]


def test_a_mute_covers_every_device_that_person_registered():
    section = Section(
        registrations=[registration("i1", user_id="u1"), registration("i2", user_id="u1")],
        mutes={"u1": {"b": 0}},
    )
    assert deliver(message(), section, now=1000) == []


# -- the two cron endings, as a registration actually carries them -----------
#
# These go through `read_section` instead of building a `Registration` by hand,
# because the thing they pin is the READER. It used to parse a row over five
# type names, so a row saying `cron_done: true` was read as saying nothing at
# all, and two types the gateway advertised and could send were unreachable
# from a device. The other tests in this file hand `recipients` the switches
# they want; these ones make a row say it.


def expo_row(types):
    """One registration exactly as the app writes it, asking for *types*."""
    return {
        "v": 1,
        "transport": "expo",
        "token": "ExponentPushToken[abcdefghijklmnopqrstuv]",
        "platform": "ios",
        "types": types,
        "preview": False,
        "updatedAt": 1000,
    }


# What a row carries today, before the app ships the two new switches.
TODAYS_ROW = {"message": True, "request": True, "cron": True, "turn_done": True, "turn_failed": True}


def asking_for(types, *, per_bot=None):
    bag = {"v": 1, "push": {"registrations": {"i1": expo_row(types)}}}
    if per_bot is not None:
        bag["push"]["perBot"] = per_bot
    return read_section(bag, "u1")


def scheduled_end(*, completed, bot="b"):
    return events.from_session_end(
        bot=bot,
        session_id="s",
        turn_id="t",
        completed=completed,
        failed=not completed,
        interrupted=False,
        at=10,
        cron=Cron(job_id="nightly", source=BY_TASK_ID),
    )


def test_a_row_that_asks_for_cron_done_is_told_about_one():
    section = asking_for({**TODAYS_ROW, "cron_done": True})
    assert len(deliver(scheduled_end(completed=True), section)) == 1


def test_a_row_that_asks_for_cron_failed_is_told_about_one():
    section = asking_for({**TODAYS_ROW, "cron_failed": True})
    assert len(deliver(scheduled_end(completed=False), section)) == 1


def test_a_row_that_never_heard_of_the_type_is_not_told():
    """Absent is OFF, so every device registered before the switch existed
    keeps exactly the behaviour it has now."""
    section = asking_for(TODAYS_ROW)
    assert deliver(scheduled_end(completed=True), section) == []
    assert deliver(scheduled_end(completed=False), section) == []


def test_asking_for_cron_deliveries_is_not_asking_about_cron_endings():
    """`cron` is "this job delivered something". It is not a stand-in for the
    other two, and inferring one from it would notify people who never asked."""
    section = asking_for({"cron": True})
    assert section.registrations[0].wants("cron") is True
    assert section.registrations[0].wants("cron_done") is False
    assert deliver(scheduled_end(completed=True), section) == []


def test_a_row_that_says_no_is_not_told():
    section = asking_for({**TODAYS_ROW, "cron_done": False, "cron_failed": False})
    assert deliver(scheduled_end(completed=True), section) == []
    assert deliver(scheduled_end(completed=False), section) == []


def test_the_gateway_ceiling_still_outranks_a_row_that_asked():
    """`push.types` is the ceiling: a device cannot switch on what this gateway
    does not send."""
    section = asking_for({**TODAYS_ROW, "cron_done": True})
    off = tuple(name for name in events.TYPES if name != "cron_done")
    assert deliver(scheduled_end(completed=True), section, enabled_types=off) == []


def test_a_chat_can_still_switch_the_two_cron_endings_off():
    section = asking_for({**TODAYS_ROW, "cron_done": True}, per_bot={"b": {"cron_done": False}})
    assert deliver(scheduled_end(completed=True), section) == []
    # And only for the chat it names.
    assert len(deliver(scheduled_end(completed=True, bot="other"), section)) == 1


def test_a_chat_can_still_switch_them_on():
    section = asking_for({**TODAYS_ROW, "cron_failed": False}, per_bot={"b": {"cron_failed": True}})
    assert len(deliver(scheduled_end(completed=False), section)) == 1


def test_a_mute_still_outranks_a_row_that_asked():
    bag = {
        "v": 1,
        "mutes": {"b": 0},
        "push": {"registrations": {"i1": expo_row({**TODAYS_ROW, "cron_failed": True})}},
    }
    assert deliver(scheduled_end(completed=False), read_section(bag, "u1"), now=1000) == []

