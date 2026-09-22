"""Who the module decides is asking, and what it tells Hermes about them."""

import types

from hermie_plugin.context import ContextModule
from hermie_plugin.context.live_session import GATEWAY_MODULE, LiveSessions
from hermie_plugin.context.render import (
    INTRODUCED,
    RETRACTED_BY_SENDER,
    SUPERSEDES_BY_SENDER,
    SUPERSEDES_BY_SENDER_IN_CHAT,
    read_section,
)
from hermie_plugin.context.session_vars import (
    SESSION_NAMES,
    UI_SESSION_ID,
    USER_ID,
    USER_ID_ALT,
    USER_NAME,
    SessionVars,
)


class FakeVariable:
    """Stands in for one of Hermes' session `ContextVar`s."""

    def __init__(self, value=""):
        self.value = value

    def set(self, value):
        self.value = value


class FakeSessionContext:
    """The two things the shim uses out of `gateway.session_context`."""

    def __init__(self, **values):
        names = (USER_ID, USER_ID_ALT, USER_NAME) + SESSION_NAMES
        self._VAR_MAP = {name: FakeVariable(values.get(name, "")) for name in names}

    def get_session_env(self, name, default=""):
        variable = self._VAR_MAP.get(name)
        return variable.value if variable is not None else default

    def snapshot(self):
        return {name: variable.value for name, variable in self._VAR_MAP.items() if variable.value}


class FakeRuntime:
    def __init__(self, sections, settings=None):
        self.sections = sections
        self.settings = settings or {}
        # The app's metadata is a file read on the agent's own path, so the
        # module promises to do at most one a turn — and none at all when it
        # already knows the answer. Counted, not assumed.
        self.reads = 0
        # Stands in for `profile.yaml`'s (mtime_ns, size). Moving it is how a
        # test says "the person edited their profile"; leaving it alone is how
        # it says "nothing happened", which is every ordinary turn.
        self.stamp = (1, 1)
        self.stamps = 0

    def app_stamp(self):
        self.stamps += 1
        return self.stamp

    def edited(self, sections):
        """The app wrote a new bag: the file moved and now says something else."""
        self.sections = sections
        self.stamp = (self.stamp[0] + 1, self.stamp[1])

    def touched(self):
        """The file moved without this module's section changing a word."""
        self.stamp = (self.stamp[0] + 1, self.stamp[1])

    def config(self, key, default=None):
        return self.settings.get(key, default)

    def bot_name(self):
        return "jurist"

    def app_sections(self):
        self.reads += 1
        return self.sections


def bag(users, default=""):
    return {"context": {"v": 1, "default": default, "users": users}}


def module_for(sections, hermes, settings=None, gateway=None):
    return ContextModule(
        FakeRuntime(sections, settings),
        session_vars=SessionVars(hermes),
        live_sessions=LiveSessions(gateway),
    )


def fake_gateway(auth_user_id, *, sid="sid-1", session_key="key-1"):
    """A stand-in for the dashboard's server module and its session table."""
    module = types.ModuleType(GATEWAY_MODULE)
    module._sessions = {sid: {"session_key": session_key, "auth_user_id": auth_user_id}}
    module._session_for_key = lambda key: next(
        (item for item in module._sessions.values() if item.get("session_key") == key), None
    )
    module._session_auth_user_id = lambda session: (session or {}).get("auth_user_id")
    return module


def test_the_variables_are_filled_when_they_are_empty():
    hermes = FakeSessionContext()
    module = module_for([("u1", bag({"u1": {"displayName": "Kim", "userIdAlt": "alt-1"}}))], hermes)

    assert module.fill_session_vars("u1", module.section) == {
        USER_ID: "u1", USER_ID_ALT: "alt-1", USER_NAME: "Kim",
    }
    assert hermes.snapshot() == {USER_ID: "u1", USER_ID_ALT: "alt-1", USER_NAME: "Kim"}


def test_a_gateway_that_already_names_this_person_is_left_alone():
    """The shim exists for a gap; it goes quiet the day the gap closes.

    Including when the gateway spells the id with the provider that issued the
    login while the app registered the bare one: that is one person, so there is
    nothing to correct and nothing to read.
    """
    hermes = FakeSessionContext(**{USER_ID: "oidc:u1"})
    module = module_for([("u1", bag({"u1": {"displayName": "Kim"}}))], hermes)

    assert module.fill_session_vars("u1", module.section) == {}
    assert hermes.snapshot() == {USER_ID: "oidc:u1"}
    assert module.runtime.reads == 0, "agreement was settled without reading the profile"


def test_a_variable_naming_somebody_else_is_put_right():
    """The field case: a shared chat, and Hermes\' own "User:" line was wrong.

    A session variable is bound when the session is created and names whoever
    opened it. On a turn from somebody else that value is not a value to keep —
    it is the line the model reads as "User:", contradicting the context section
    out loud, which is exactly what the bot in the report went by.
    """
    hermes = FakeSessionContext(**{USER_ID: "u2", USER_NAME: "Ana"})
    module = module_for(
        [("", bag({"u1": {"displayName": "Kim", "userIdAlt": "alt-1"}, "u2": {"displayName": "Ana"}}))],
        hermes,
    )

    assert module.fill_session_vars("u1", module.section) == {
        USER_ID: "u1", USER_ID_ALT: "alt-1", USER_NAME: "Kim",
    }
    assert hermes.snapshot() == {USER_ID: "u1", USER_ID_ALT: "alt-1", USER_NAME: "Kim"}


def test_a_name_belonging_to_the_previous_person_does_not_survive_the_correction():
    """Nothing is known about the sender, so nothing may be said about them."""
    hermes = FakeSessionContext(**{USER_ID: "u2", USER_NAME: "Ana"})
    module = module_for([("", bag({"u2": {"displayName": "Ana"}}))], hermes)

    assert module.fill_session_vars("u1", module.section) == {USER_ID: "u1", USER_NAME: ""}
    assert hermes.snapshot() == {USER_ID: "u1"}


def test_nothing_is_rewritten_while_the_gateway_names_nobody_for_this_turn():
    """A bound login beats nothing at all; there is no better answer to give."""
    hermes = FakeSessionContext(**{USER_ID: "u2", USER_NAME: "Ana"})
    module = module_for([("", bag({"u1": {"displayName": "Kim"}}, default="u1"))], hermes)

    assert module.fill_session_vars("", module.section) == {}
    assert hermes.snapshot() == {USER_ID: "u2", USER_NAME: "Ana"}


def test_a_variable_that_already_has_a_value_keeps_it():
    hermes = FakeSessionContext(**{USER_NAME: "set by the gateway"})
    module = module_for([("u1", bag({"u1": {"displayName": "Kim"}}))], hermes)

    assert module.fill_session_vars("u1", module.section) == {USER_ID: "u1"}
    assert hermes.snapshot()[USER_NAME] == "set by the gateway"


def test_the_only_registered_person_is_named_when_the_gateway_names_nobody():
    hermes = FakeSessionContext()
    module = module_for([("u1", bag({"u1": {"displayName": "Kim"}}))], hermes)

    assert module.fill_session_vars("", module.section)[USER_ID] == "u1"


def test_several_people_and_no_sender_names_nobody():
    hermes = FakeSessionContext()
    module = module_for([("", bag({"u1": {"displayName": "Kim"}, "u2": {"displayName": "Ana"}}))], hermes)

    assert module.fill_session_vars("", module.section) == {}
    assert hermes.snapshot() == {}


def test_a_sender_with_no_entry_is_still_named_but_borrows_nobodys_name():
    """The id is a fact from the gateway; the display name would be a guess."""
    hermes = FakeSessionContext()
    module = module_for([("", bag({"u1": {"displayName": "Kim"}}, default="u1"))], hermes)

    assert module.fill_session_vars("u2", module.section) == {USER_ID: "u2"}


def test_the_setting_switches_it_off():
    hermes = FakeSessionContext()
    module = module_for(
        [("u1", bag({"u1": {"displayName": "Kim"}}))], hermes, settings={"context.session_vars": False}
    )

    assert module.fill_session_vars("u1", module.section) == {}
    assert hermes.snapshot() == {}


def test_a_gateway_without_the_session_variables_costs_nothing():
    """An older Hermes, or a run outside one, must not fail the turn."""
    module = module_for([("u1", bag({"u1": {"displayName": "Kim"}}))], None)

    assert module.session_vars.available() is False
    assert module.fill_session_vars("u1", module.section) == {}


def test_the_hook_fills_them_on_its_way_past():
    hermes = FakeSessionContext()
    module = module_for([("u1", bag({"u1": {"displayName": "Kim"}}))], hermes)

    module.on_pre_llm_call(session_id="s1", sender_id="u1")
    assert hermes.snapshot() == {USER_ID: "u1", USER_NAME: "Kim"}


def test_the_alternative_id_is_read_from_the_person_the_app_wrote():
    assert read_section(bag({"u1": {"userIdAlt": "alt-1"}})).users["u1"].user_id_alt == "alt-1"


# -- the session variable as a sender ----------------------------------------
#
# `sender_id` is empty on the dashboard's WebSocket route while the gateway
# does know the login, because it binds it into the session variables instead.


def two_people():
    return [("", bag({"u1": {"displayName": "Kim"}, "u2": {"displayName": "Ana"}}))]


def test_the_session_variable_names_the_sender_when_the_kwarg_is_empty():
    hermes = FakeSessionContext(**{USER_ID: "u2"})
    module = module_for(two_people(), hermes)

    added = module.on_pre_llm_call(session_id="s1", sender_id="")
    assert "Ana" in added["context"]


def test_the_kwarg_still_wins_over_the_session_variable():
    hermes = FakeSessionContext(**{USER_ID: "u2"})
    module = module_for(two_people(), hermes)

    added = module.on_pre_llm_call(session_id="s1", sender_id="u1")
    assert "Kim" in added["context"]


def test_the_frozen_section_names_the_session_user():
    """The mapping core hands the section carries no identity; this one does."""
    hermes = FakeSessionContext(**{USER_ID: "u2"})
    module = module_for(two_people(), hermes)

    assert "Ana" in module.render_section({"session_id": "s1", "profile_name": "jurist"})


def test_the_frozen_section_covers_that_sender_so_the_turn_costs_nothing():
    hermes = FakeSessionContext(**{USER_ID: "u2"})
    module = module_for(two_people(), hermes)

    module.render_section({"session_id": "s1", "profile_name": "jurist"})
    assert module.on_pre_llm_call(session_id="s1", sender_id="") is None


def test_no_session_variable_leaves_the_old_answer():
    """An ungated gateway names nobody anywhere, and nothing is injected."""
    hermes = FakeSessionContext()
    module = module_for(two_people(), hermes)

    assert module.render_section({"session_id": "s1", "profile_name": "jurist"}) == ""
    assert module.on_pre_llm_call(session_id="s1", sender_id="") is None


def test_reading_the_sender_is_not_gated_by_the_write_switch():
    """`context.session_vars` switches off filling them in, not asking them."""
    hermes = FakeSessionContext(**{USER_ID: "u2"})
    module = module_for(two_people(), hermes, settings={"context.session_vars": False})

    assert "Ana" in module.on_pre_llm_call(session_id="s1", sender_id="")["context"]


def test_a_gateway_without_the_session_variables_asks_nobody():
    module = module_for(two_people(), None)

    assert module.on_pre_llm_call(session_id="s1", sender_id="") is None


# -- the login's provider prefix ---------------------------------------------


def test_a_prefixed_login_is_written_as_the_gateway_spelled_it():
    """The id is the gateway's fact; the name comes off the person it names."""
    hermes = FakeSessionContext()
    module = module_for([("7f3c02", bag({"7f3c02": {"displayName": "Kim", "userIdAlt": "alt-1"}}))], hermes)

    assert module.fill_session_vars("self-hosted:7f3c02", module.section) == {
        USER_ID: "self-hosted:7f3c02", USER_ID_ALT: "alt-1", USER_NAME: "Kim",
    }


def test_the_frozen_section_already_covers_the_same_person_under_either_spelling():
    hermes = FakeSessionContext()
    module = module_for(
        [("", bag({"7f3c02": {"displayName": "Kim"}, "ana": {"displayName": "Ana"}}, default="7f3c02"))],
        hermes,
    )

    module.render_section({"session_id": "s1", "profile_name": "jurist"})
    assert module.on_pre_llm_call(session_id="s1", sender_id="self-hosted:7f3c02") is None


def test_a_covered_sender_is_recognised_without_re_reading_the_metadata():
    """Either spelling of the frozen person ends the turn before the file read."""
    hermes = FakeSessionContext(**{USER_ID: "self-hosted:7f3c02"})
    runtime = FakeRuntime([("", bag({"7f3c02": {"displayName": "Kim"}, "ana": {"displayName": "Ana"}}))])
    module = ContextModule(runtime, session_vars=SessionVars(hermes))

    assert "Kim" in module.render_section({"session_id": "s1", "profile_name": "jurist"})
    reads = runtime.reads
    assert module.on_pre_llm_call(session_id="s1", sender_id="self-hosted:7f3c02") is None
    assert runtime.reads == reads


# -- the live session record -------------------------------------------------
#
# The dashboard names nobody to a hook and nobody in the prompt section's
# mapping, but it stamped the login on its own session record.


def two_logins():
    return [("", bag({"7f3c02": {"displayName": "Kim"}, "ana": {"displayName": "Ana"}}))]


def test_the_live_record_names_the_sender_when_nothing_else_does():
    module = module_for(two_logins(), FakeSessionContext(), gateway=fake_gateway("self-hosted:7f3c02"))

    added = module.on_pre_llm_call(session_id="key-1", sender_id="")
    assert "Kim" in added["context"]


def test_the_id_may_come_from_the_session_variables_rather_than_the_hook():
    hermes = FakeSessionContext(**{UI_SESSION_ID: "sid-1"})
    module = module_for(two_logins(), hermes, gateway=fake_gateway("self-hosted:7f3c02"))

    assert "Kim" in module.on_pre_llm_call(session_id="", sender_id="")["context"]


def test_the_live_record_beats_the_session_variable():
    """The record is per connection; the variable is per session, and older.

    A session variable is bound when the session is created and is never rebound,
    so on a chat several people share it names whoever opened it. The gateway
    stamped the login of the connection THIS turn arrived on, which is the
    question being asked.
    """
    hermes = FakeSessionContext(**{USER_ID: "ana"})
    module = module_for(two_logins(), hermes, gateway=fake_gateway("self-hosted:7f3c02"))

    assert "Kim" in module.on_pre_llm_call(session_id="key-1", sender_id="")["context"]


def test_the_session_variable_is_still_asked_where_no_record_answers():
    """A gateway whose table this plugin cannot read has nothing else to go on."""
    hermes = FakeSessionContext(**{USER_ID: "ana"})
    module = module_for(two_logins(), hermes, gateway=fake_gateway(None))

    assert "Ana" in module.on_pre_llm_call(session_id="key-1", sender_id="")["context"]


def test_the_hook_sender_beats_both():
    hermes = FakeSessionContext(**{USER_ID: "ana"})
    module = module_for(two_logins(), hermes, gateway=fake_gateway("self-hosted:7f3c02"))

    assert "Kim" in module.on_pre_llm_call(session_id="key-1", sender_id="7f3c02")["context"]


def test_the_frozen_section_asks_the_live_record_too():
    module = module_for(two_logins(), FakeSessionContext(), gateway=fake_gateway("self-hosted:7f3c02"))

    text = module.render_section({"session_id": "key-1", "profile_name": "jurist"})
    assert "Kim" in text
    assert module.on_pre_llm_call(session_id="key-1", sender_id="") is None


def test_a_session_admitted_under_no_login_leaves_the_order_alone():
    module = module_for(two_logins(), FakeSessionContext(), gateway=fake_gateway(None))

    assert module.render_section({"session_id": "key-1", "profile_name": "jurist"}) == ""
    assert module.on_pre_llm_call(session_id="key-1", sender_id="") is None


def test_the_live_login_is_what_hermes_is_told_this_turn():
    """The prefixed id is the gateway's own fact; the name is the app's."""
    hermes = FakeSessionContext()
    module = module_for(
        [("7f3c02", bag({"7f3c02": {"displayName": "Kim", "userIdAlt": "alt-1"}}))],
        hermes,
        gateway=fake_gateway("self-hosted:7f3c02"),
    )

    module.on_pre_llm_call(session_id="key-1", sender_id="")
    assert hermes.snapshot()[USER_ID] == "self-hosted:7f3c02"
    assert hermes.snapshot()[USER_NAME] == "Kim"
    assert hermes.snapshot()[USER_ID_ALT] == "alt-1"


# -- a shared chat -----------------------------------------------------------
#
# One session, several people. Whoever opened it is bound into the session
# variables for the life of the session; the person typing now is whoever the
# turn itself names. Every test below is about the second of those winning.

OPENER = "basic:opener"
SENDER = "oidc:sender-sub"


def shared_gateway():
    """Two people with their own rows, and a login that opened the chat with none.

    Each per-user bag names its own person as the default, the way the app
    writes them, so the two disagree and the section has no default at all.
    """
    return [
        ("sender-sub", bag({"sender-sub": {"displayName": "Sam", "userIdAlt": "alt-s"}}, default="sender-sub")),
        ("other", bag({"other": {"displayName": "Olga"}}, default="other")),
    ]


def start(module, session_id="s1"):
    return module.render_section({"session_id": session_id, "profile_name": "jurist"})


def test_the_field_case_the_person_sending_is_described_not_the_one_who_opened():
    """The report: the opener has no row, the sender has one, the bot knew neither.

    The session variables name the login that opened the chat. The turn names
    somebody else, somebody the app does have a row for. The bot must be told
    about that person, and Hermes' own "User:" line must agree with it.
    """
    hermes = FakeSessionContext(**{USER_ID: OPENER})
    module = module_for(shared_gateway(), hermes)

    assert start(module) == "", "the opener has no row and nobody is the default"

    added = module.on_pre_llm_call(session_id="s1", sender_id=SENDER)
    assert added is not None, "the person sending the turn never reached the bot"
    assert added["context"].startswith(INTRODUCED)
    assert "Sam" in added["context"] and "Olga" not in added["context"]
    assert hermes.snapshot()[USER_ID] == SENDER
    assert hermes.snapshot()[USER_NAME] == "Sam"
    assert hermes.snapshot()[USER_ID_ALT] == "alt-s"


def test_the_field_case_after_the_opener_has_already_had_a_turn():
    """The opener typed first and was told nothing. That must not stand in the way."""
    hermes = FakeSessionContext(**{USER_ID: OPENER})
    module = module_for(shared_gateway(), hermes)
    start(module)

    assert module.on_pre_llm_call(session_id="s1", sender_id=OPENER) is None

    added = module.on_pre_llm_call(session_id="s1", sender_id=SENDER)
    assert added is not None and added["context"].startswith(INTRODUCED)
    assert "Sam" in added["context"]
    assert module.on_pre_llm_call(session_id="s1", sender_id=SENDER) is None, "said on every turn"


def test_the_field_case_when_only_the_live_record_names_the_sender():
    """The hook names nobody; the session variables still name the opener.

    The record of the connection this turn arrived on is asked before the
    variables bound when the session was created.
    """
    hermes = FakeSessionContext(**{USER_ID: OPENER})
    module = module_for(shared_gateway(), hermes, gateway=fake_gateway(SENDER))

    added = module.on_pre_llm_call(session_id="key-1", sender_id="")
    assert added is not None and "Sam" in added["context"]
    assert hermes.snapshot()[USER_ID] == SENDER


def test_a_change_of_sender_supersedes_once_and_not_on_every_turn():
    hermes = FakeSessionContext(**{USER_ID: "other"})
    module = module_for(shared_gateway(), hermes, settings={"context.session_vars": False})
    assert "Olga" in start(module)

    first = module.on_pre_llm_call(session_id="s1", sender_id=SENDER)
    assert first is not None and first["context"].startswith(SUPERSEDES_BY_SENDER)
    assert "Sam" in first["context"] and "Olga" not in first["context"]
    assert module.on_pre_llm_call(session_id="s1", sender_id=SENDER) is None
    assert module.on_pre_llm_call(session_id="s1", sender_id=SENDER) is None

    back = module.on_pre_llm_call(session_id="s1", sender_id="other")
    assert back is not None, "the person who opened the chat was left described as the other one"
    assert back["context"].startswith(SUPERSEDES_BY_SENDER_IN_CHAT)
    assert "Olga" in back["context"] and "Sam" not in back["context"]
    assert module.on_pre_llm_call(session_id="s1", sender_id="other") is None


def test_an_empty_record_is_introduced_once_somebody_resolves():
    """Nobody resolved when the chat began; the first person who does is introduced."""
    hermes = FakeSessionContext()
    module = module_for(shared_gateway(), hermes)
    assert start(module) == ""
    assert module.on_pre_llm_call(session_id="s1", sender_id="") is None

    added = module.on_pre_llm_call(session_id="s1", sender_id=SENDER)
    assert added is not None and added["context"].startswith(INTRODUCED)
    assert module.on_pre_llm_call(session_id="s1", sender_id=SENDER) is None


def test_the_variables_follow_the_sender_from_turn_to_turn():
    hermes = FakeSessionContext(**{USER_ID: OPENER})
    module = module_for(shared_gateway(), hermes)

    module.on_pre_llm_call(session_id="s1", sender_id=SENDER)
    assert hermes.snapshot() == {USER_ID: SENDER, USER_ID_ALT: "alt-s", USER_NAME: "Sam"}

    module.on_pre_llm_call(session_id="s1", sender_id="other")
    assert hermes.snapshot() == {USER_ID: "other", USER_NAME: "Olga"}


def test_a_sender_without_a_row_and_no_default_is_shown_nobody_elses():
    """One registered person and no default: a stranger still gets nothing of theirs."""
    hermes = FakeSessionContext(**{USER_ID: OPENER})
    module = module_for([("", bag({"sender-sub": {"displayName": "Sam", "about": "Private."}}))], hermes)

    assert start(module) == ""
    assert module.on_pre_llm_call(session_id="s1", sender_id=OPENER) is None
    assert module.on_pre_llm_call(session_id="s1", sender_id=OPENER) is None
    assert hermes.snapshot() == {USER_ID: OPENER}


def test_a_stranger_after_somebody_described_withdraws_that_description_once():
    """Nothing is said about the stranger, and the previous person is not left standing."""
    hermes = FakeSessionContext(**{USER_ID: SENDER})
    module = module_for(shared_gateway(), hermes, settings={"context.session_vars": False})
    assert "Sam" in start(module)

    added = module.on_pre_llm_call(session_id="s1", sender_id=OPENER)
    assert added == {"context": RETRACTED_BY_SENDER}
    assert module.on_pre_llm_call(session_id="s1", sender_id=OPENER) is None
