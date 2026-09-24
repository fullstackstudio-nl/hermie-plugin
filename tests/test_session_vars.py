"""Who the module tells Hermes is asking, and what it will and will not say.

Until HERM-119 `fill_session_vars` also read a profile the app had written, to
fill `HERMES_SESSION_USER_NAME` and `_ID_ALT` with what it found. There is no
more profile to read, so those two are never filled with anything any more —
only blanked, when correcting a stale binding, so a name belonging to whoever
opened the session does not survive being told somebody else is typing. What
is left to test is `HERMES_SESSION_USER_ID` itself, and the rung order that
decides which sender it is filled with.
"""

import types

from hermie_plugin.context import ContextModule
from hermie_plugin.context.live_session import GATEWAY_MODULE, LiveSessions
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
    def __init__(self, settings=None, bot="jurist"):
        self.settings = settings or {}
        self.bot = bot

    def config(self, key, default=None):
        return self.settings.get(key, default)

    def bot_name(self):
        return self.bot


def module_for(hermes, settings=None, gateway=None):
    return ContextModule(
        FakeRuntime(settings),
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


# -- fill_session_vars --------------------------------------------------------


def test_the_id_is_filled_when_it_is_empty():
    hermes = FakeSessionContext()
    module = module_for(hermes)

    assert module.fill_session_vars("u1") == {USER_ID: "u1"}
    assert hermes.snapshot() == {USER_ID: "u1"}


def test_a_gateway_that_already_names_this_person_is_left_alone():
    """The shim exists for a gap; it goes quiet the day the gap closes.

    Including when the gateway spells the id with the provider that issued
    the login while the sender arrives bare: that is one person, so there is
    nothing to correct.
    """
    hermes = FakeSessionContext(**{USER_ID: "oidc:u1"})
    module = module_for(hermes)

    assert module.fill_session_vars("u1") == {}
    assert hermes.snapshot() == {USER_ID: "oidc:u1"}


def test_a_variable_naming_somebody_else_is_put_right():
    """The field case: a shared chat, and Hermes' own "User:" line was wrong.

    A session variable is bound when the session is created and names whoever
    opened it. On a turn from somebody else that value is not a value to
    keep — it is the line the model reads as "User:", contradicting who is
    actually typing.
    """
    hermes = FakeSessionContext(**{USER_ID: "u2", USER_NAME: "Ana"})
    module = module_for(hermes)

    # `USER_ID_ALT` was already empty, so `fill` has nothing to report for it —
    # only what actually changed is written back.
    assert module.fill_session_vars("u1") == {USER_ID: "u1", USER_NAME: ""}
    assert hermes.snapshot() == {USER_ID: "u1"}


def test_a_name_belonging_to_the_previous_person_does_not_survive_the_correction():
    """There is no profile any more to say anything about the new sender, so
    the correction blanks the name rather than leaving the old one standing."""
    hermes = FakeSessionContext(**{USER_ID: "u2", USER_NAME: "Ana", USER_ID_ALT: "alt-2"})
    module = module_for(hermes)

    module.fill_session_vars("u1")

    assert hermes.snapshot() == {USER_ID: "u1"}


def test_nothing_is_rewritten_while_the_gateway_names_nobody_for_this_turn():
    """A bound login beats nothing at all; there is no better answer to give."""
    hermes = FakeSessionContext(**{USER_ID: "u2", USER_NAME: "Ana"})
    module = module_for(hermes)

    assert module.fill_session_vars("") == {}
    assert hermes.snapshot() == {USER_ID: "u2", USER_NAME: "Ana"}


def test_a_variable_that_already_has_a_value_keeps_it():
    """Filling `USER_ID` in must not blank a name Hermes (or another plugin)
    already put there for a person this module agrees is the sender."""
    hermes = FakeSessionContext(**{USER_NAME: "set by the gateway"})
    module = module_for(hermes)

    assert module.fill_session_vars("u1") == {USER_ID: "u1"}
    assert hermes.snapshot()[USER_NAME] == "set by the gateway"


def test_the_setting_switches_it_off():
    hermes = FakeSessionContext()
    module = module_for(hermes, settings={"context.session_vars": False})

    assert module.fill_session_vars("u1") == {}
    assert hermes.snapshot() == {}


def test_a_gateway_without_the_session_variables_costs_nothing():
    """An older Hermes, or a run outside one, must not fail the turn."""
    module = module_for(None)

    assert module.session_vars.available() is False
    assert module.fill_session_vars("u1") == {}


def test_a_prefixed_login_is_written_as_the_gateway_spelled_it():
    hermes = FakeSessionContext()
    module = module_for(hermes)

    assert module.fill_session_vars("self-hosted:7f3c02") == {USER_ID: "self-hosted:7f3c02"}


def test_the_hook_fills_it_on_its_way_past():
    hermes = FakeSessionContext()
    module = module_for(hermes)

    module.on_pre_llm_call(session_id="s1", sender_id="u1")
    assert hermes.snapshot() == {USER_ID: "u1"}


def test_a_gateway_without_the_session_variables_asks_nobody():
    module = module_for(None)

    assert module.on_pre_llm_call(session_id="s1", sender_id="u1") is None


# -- the rung order: which sender wins ----------------------------------------


def test_the_session_variable_names_the_sender_when_the_kwarg_is_empty():
    hermes = FakeSessionContext(**{USER_ID: "u2"})
    module = module_for(hermes)

    module.on_pre_llm_call(session_id="s1", sender_id="")
    assert hermes.snapshot()[USER_ID] == "u2"


def test_the_kwarg_still_wins_over_the_session_variable():
    hermes = FakeSessionContext(**{USER_ID: "u2"})
    module = module_for(hermes)

    module.on_pre_llm_call(session_id="s1", sender_id="u1")
    assert hermes.snapshot()[USER_ID] == "u1"


def test_reading_the_sender_is_not_gated_by_the_write_switch():
    """`context.session_vars` switches off filling them in, not asking them."""
    hermes = FakeSessionContext(**{USER_ID: "u2"})
    module = module_for(hermes, settings={"context.session_vars": False})

    assert module.sender_with_source("", "s1")[0] == "u2"
    module.on_pre_llm_call(session_id="s1", sender_id="")
    assert hermes.snapshot() == {USER_ID: "u2"}, "the switch is off, so nothing here was rewritten"


def test_the_live_record_names_the_sender_when_nothing_else_does():
    module = module_for(FakeSessionContext(), gateway=fake_gateway("self-hosted:7f3c02"))

    module.on_pre_llm_call(session_id="key-1", sender_id="")
    assert module.session_vars.read(USER_ID) == "self-hosted:7f3c02"


def test_the_id_may_come_from_the_session_variables_rather_than_the_hook():
    hermes = FakeSessionContext(**{UI_SESSION_ID: "sid-1"})
    module = module_for(hermes, gateway=fake_gateway("self-hosted:7f3c02"))

    module.on_pre_llm_call(session_id="", sender_id="")
    assert hermes.snapshot()[USER_ID] == "self-hosted:7f3c02"


def test_the_live_record_beats_the_session_variable():
    """The record is per connection; the variable is per session, and older.

    A session variable is bound when the session is created and is never
    rebound, so on a chat several people share it names whoever opened it.
    The gateway stamped the login of the connection THIS turn arrived on,
    which is the question being asked.
    """
    hermes = FakeSessionContext(**{USER_ID: "ana"})
    module = module_for(hermes, gateway=fake_gateway("self-hosted:7f3c02"))

    module.on_pre_llm_call(session_id="key-1", sender_id="")
    assert hermes.snapshot()[USER_ID] == "self-hosted:7f3c02"


def test_the_session_variable_is_still_asked_where_no_record_answers():
    """A gateway whose table this plugin cannot read has nothing else to go on."""
    hermes = FakeSessionContext(**{USER_ID: "ana"})
    module = module_for(hermes, gateway=fake_gateway(None))

    module.on_pre_llm_call(session_id="key-1", sender_id="")
    assert hermes.snapshot() == {USER_ID: "ana"}


def test_the_hook_sender_beats_both():
    hermes = FakeSessionContext(**{USER_ID: "ana"})
    module = module_for(hermes, gateway=fake_gateway("self-hosted:7f3c02"))

    module.on_pre_llm_call(session_id="key-1", sender_id="7f3c02")
    assert hermes.snapshot()[USER_ID] == "7f3c02"


def test_a_session_admitted_under_no_login_leaves_the_order_alone():
    hermes = FakeSessionContext()
    module = module_for(hermes, gateway=fake_gateway(None))

    module.on_pre_llm_call(session_id="key-1", sender_id="")
    assert hermes.snapshot() == {}
