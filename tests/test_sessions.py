"""Which conversation a notification belongs to.

The classification is pure and is driven here with a title lookup handed in, the
same way the cron detection is driven with its session variable. What is NOT
covered is the lookup itself: reading a title needs Hermes' session registry,
and there is none in a bare checkout.
"""

from hermie_plugin.push import events, sessions


def test_the_canonical_chat_is_the_one_titled_exactly_that():
    """ADR-0007's forever-chat. The title is not a convention, it is the
    registry key the roster already looks a bot's chat up by."""
    assert sessions.kind_of("Bot Chat") == "canonical"


def test_a_branch_is_recognised_by_the_name_the_app_gives_it():
    assert sessions.kind_of("Branch · what if we filed it late") == "branch"
    # `branchTitle` falls back to the bare word for a row with no words in it.
    assert sessions.kind_of("Branch") == "branch"


def test_a_conversation_that_merely_starts_with_the_word_is_not_a_branch():
    """The app writes `Branch · …`; anything else is a name somebody chose."""
    assert sessions.kind_of("Branching strategy") == "other"
    assert sessions.kind_of("Branch: late filing") == "other"


def test_a_retired_conversation_is_neither_canonical_nor_a_branch():
    """`Bot Chat · <date time>` is what `/new` puts away. It starts with the
    canonical title and is emphatically not it."""
    assert sessions.kind_of("Bot Chat · 22 Sep 2026 14:03") == "other"


def test_a_session_nobody_named_says_nothing():
    """Unreadable rather than "other": a lookup that answered nothing must not
    send a tap to a screen it guessed at."""
    assert sessions.kind_of(None) == ""
    assert sessions.kind_of("") == ""
    assert sessions.kind_of("   ") == ""


def test_the_kinds_are_the_three_the_payload_may_carry():
    assert sessions.KINDS == ("canonical", "branch", "other")


def test_a_lookup_is_driven_by_the_id_it_was_given():
    seen = []

    def read(session_id):
        seen.append(session_id)
        return "Branch · a second thought"

    assert sessions.kind_for("stored-abc123", read=read) == "branch"
    assert seen == ["stored-abc123"]


def test_a_lookup_that_raises_is_not_a_failed_notification():
    def read(session_id):
        raise RuntimeError("the registry is closed")

    assert sessions.kind_for("s1", read=read) == ""


def test_without_hermes_nothing_is_claimed():
    """A bare checkout has no session registry, and that is exactly the state a
    gateway too old for one is in."""
    assert sessions.read_title("s1") is None
    assert sessions.available() is False


# -- and what travels --------------------------------------------------------


def test_the_payload_carries_the_kind_beside_the_id():
    note = events.from_assistant_message(
        bot="jurist", session_id="stored-abc123", turn_id="t1", assistant_response="hello", at=10
    )
    payload = note.payload(preview=False, session_kind="branch")

    assert payload["sessionId"] == "stored-abc123"
    assert payload["sessionKind"] == "branch"


def test_a_kind_this_gateway_could_not_work_out_is_simply_absent():
    note = events.from_assistant_message(
        bot="jurist", session_id="stored-abc123", turn_id="t1", assistant_response="hello", at=10
    )
    assert "sessionKind" not in note.payload(preview=False)
    assert "sessionKind" not in note.payload(preview=False, session_kind="")
