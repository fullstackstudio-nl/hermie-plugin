"""What a hook's kwargs become, and who is told."""

from hermie_plugin.push import events
from hermie_plugin.push.registrations import Registration, Section


def registration(installation_id="i1", *, preview=False, types=None, transport="expo"):
    return Registration(
        installation_id=installation_id,
        transport=transport,
        platform="ios",
        types={name: True for name in (types or events.TYPES)},
        preview=preview,
        updated_at=1000,
        token="ExponentPushToken[abcdefghijklmnopqrstuv]",
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


def test_a_message_is_suppressed_while_somebody_is_looking():
    note = events.from_assistant_message(
        bot="b", session_id="s", turn_id="t", assistant_response="hello", at=10
    )
    watching = Section(registrations=[registration()], seen={"i1": 990})
    assert deliver(note, watching) == []


def test_a_request_is_never_suppressed():
    """A question with a countdown on it is worth a buzz even if a tablet is open."""
    note = events.from_approval(
        bot="b", session_key="s", description="delete the build directory", request_id="r1", turn_id="t", at=10
    )
    watching = Section(registrations=[registration()], seen={"i1": 990})
    assert len(deliver(note, watching)) == 1


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
