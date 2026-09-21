"""Who the module decides is asking, and what it tells Hermes about them."""

import types

from hermie_plugin.context import ContextModule
from hermie_plugin.context.live_session import GATEWAY_MODULE, LiveSessions
from hermie_plugin.context.render import read_section
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
    module = module_for([("u1", bag({"u1": {"displayName": "Sebas", "userIdAlt": "alt-1"}}))], hermes)

    assert module.fill_session_vars("u1", module.section) == {
        USER_ID: "u1", USER_ID_ALT: "alt-1", USER_NAME: "Sebas",
    }
    assert hermes.snapshot() == {USER_ID: "u1", USER_ID_ALT: "alt-1", USER_NAME: "Sebas"}


def test_a_gateway_that_already_knows_is_left_alone():
    """The shim exists for a gap; it goes quiet the day the gap closes."""
    hermes = FakeSessionContext(**{USER_ID: "oidc:sebas"})
    module = module_for([("u1", bag({"u1": {"displayName": "Sebas"}}))], hermes)

    assert module.fill_session_vars("u1", module.section) == {}
    assert hermes.snapshot() == {USER_ID: "oidc:sebas"}


def test_a_variable_that_already_has_a_value_keeps_it():
    hermes = FakeSessionContext(**{USER_NAME: "set by the gateway"})
    module = module_for([("u1", bag({"u1": {"displayName": "Sebas"}}))], hermes)

    assert module.fill_session_vars("u1", module.section) == {USER_ID: "u1"}
    assert hermes.snapshot()[USER_NAME] == "set by the gateway"


def test_the_only_registered_person_is_named_when_the_gateway_names_nobody():
    hermes = FakeSessionContext()
    module = module_for([("u1", bag({"u1": {"displayName": "Sebas"}}))], hermes)

    assert module.fill_session_vars("", module.section)[USER_ID] == "u1"


def test_several_people_and_no_sender_names_nobody():
    hermes = FakeSessionContext()
    module = module_for([("", bag({"u1": {"displayName": "Sebas"}, "u2": {"displayName": "Ana"}}))], hermes)

    assert module.fill_session_vars("", module.section) == {}
    assert hermes.snapshot() == {}


def test_a_sender_with_no_entry_is_still_named_but_borrows_nobodys_name():
    """The id is a fact from the gateway; the display name would be a guess."""
    hermes = FakeSessionContext()
    module = module_for([("", bag({"u1": {"displayName": "Sebas"}}, default="u1"))], hermes)

    assert module.fill_session_vars("u2", module.section) == {USER_ID: "u2"}


def test_the_setting_switches_it_off():
    hermes = FakeSessionContext()
    module = module_for(
        [("u1", bag({"u1": {"displayName": "Sebas"}}))], hermes, settings={"context.session_vars": False}
    )

    assert module.fill_session_vars("u1", module.section) == {}
    assert hermes.snapshot() == {}


def test_a_gateway_without_the_session_variables_costs_nothing():
    """An older Hermes, or a run outside one, must not fail the turn."""
    module = module_for([("u1", bag({"u1": {"displayName": "Sebas"}}))], None)

    assert module.session_vars.available() is False
    assert module.fill_session_vars("u1", module.section) == {}


def test_the_hook_fills_them_on_its_way_past():
    hermes = FakeSessionContext()
    module = module_for([("u1", bag({"u1": {"displayName": "Sebas"}}))], hermes)

    module.on_pre_llm_call(session_id="s1", sender_id="u1")
    assert hermes.snapshot() == {USER_ID: "u1", USER_NAME: "Sebas"}


def test_the_alternative_id_is_read_from_the_person_the_app_wrote():
    assert read_section(bag({"u1": {"userIdAlt": "alt-1"}})).users["u1"].user_id_alt == "alt-1"


# -- the session variable as a sender ----------------------------------------
#
# `sender_id` is empty on the dashboard's WebSocket route while the gateway
# does know the login, because it binds it into the session variables instead.


def two_people():
    return [("", bag({"u1": {"displayName": "Sebas"}, "u2": {"displayName": "Ana"}}))]


def test_the_session_variable_names_the_sender_when_the_kwarg_is_empty():
    hermes = FakeSessionContext(**{USER_ID: "u2"})
    module = module_for(two_people(), hermes)

    added = module.on_pre_llm_call(session_id="s1", sender_id="")
    assert "Ana" in added["context"]


def test_the_kwarg_still_wins_over_the_session_variable():
    hermes = FakeSessionContext(**{USER_ID: "u2"})
    module = module_for(two_people(), hermes)

    added = module.on_pre_llm_call(session_id="s1", sender_id="u1")
    assert "Sebas" in added["context"]


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
    module = module_for([("ef11a9", bag({"ef11a9": {"displayName": "Sebas", "userIdAlt": "alt-1"}}))], hermes)

    assert module.fill_session_vars("self-hosted:ef11a9", module.section) == {
        USER_ID: "self-hosted:ef11a9", USER_ID_ALT: "alt-1", USER_NAME: "Sebas",
    }


def test_the_frozen_section_already_covers_the_same_person_under_either_spelling():
    hermes = FakeSessionContext()
    module = module_for(
        [("", bag({"ef11a9": {"displayName": "Sebas"}, "ana": {"displayName": "Ana"}}, default="ef11a9"))],
        hermes,
    )

    module.render_section({"session_id": "s1", "profile_name": "jurist"})
    assert module.on_pre_llm_call(session_id="s1", sender_id="self-hosted:ef11a9") is None


def test_a_covered_sender_is_recognised_without_re_reading_the_metadata():
    """Either spelling of the frozen person ends the turn before the file read."""
    hermes = FakeSessionContext(**{USER_ID: "self-hosted:ef11a9"})
    runtime = FakeRuntime([("", bag({"ef11a9": {"displayName": "Sebas"}, "ana": {"displayName": "Ana"}}))])
    module = ContextModule(runtime, session_vars=SessionVars(hermes))

    assert "Sebas" in module.render_section({"session_id": "s1", "profile_name": "jurist"})
    reads = runtime.reads
    assert module.on_pre_llm_call(session_id="s1", sender_id="self-hosted:ef11a9") is None
    assert runtime.reads == reads


# -- the live session record -------------------------------------------------
#
# The dashboard names nobody to a hook and nobody in the prompt section's
# mapping, but it stamped the login on its own session record.


def two_logins():
    return [("", bag({"ef11a9": {"displayName": "Sebas"}, "ana": {"displayName": "Ana"}}))]


def test_the_live_record_names_the_sender_when_nothing_else_does():
    module = module_for(two_logins(), FakeSessionContext(), gateway=fake_gateway("self-hosted:ef11a9"))

    added = module.on_pre_llm_call(session_id="key-1", sender_id="")
    assert "Sebas" in added["context"]


def test_the_id_may_come_from_the_session_variables_rather_than_the_hook():
    hermes = FakeSessionContext(**{UI_SESSION_ID: "sid-1"})
    module = module_for(two_logins(), hermes, gateway=fake_gateway("self-hosted:ef11a9"))

    assert "Sebas" in module.on_pre_llm_call(session_id="", sender_id="")["context"]


def test_the_session_variable_beats_the_live_record():
    hermes = FakeSessionContext(**{USER_ID: "ana"})
    module = module_for(two_logins(), hermes, gateway=fake_gateway("self-hosted:ef11a9"))

    assert "Ana" in module.on_pre_llm_call(session_id="key-1", sender_id="")["context"]


def test_the_hook_sender_beats_both():
    hermes = FakeSessionContext(**{USER_ID: "ana"})
    module = module_for(two_logins(), hermes, gateway=fake_gateway("self-hosted:ef11a9"))

    assert "Sebas" in module.on_pre_llm_call(session_id="key-1", sender_id="ef11a9")["context"]


def test_the_frozen_section_asks_the_live_record_too():
    module = module_for(two_logins(), FakeSessionContext(), gateway=fake_gateway("self-hosted:ef11a9"))

    text = module.render_section({"session_id": "key-1", "profile_name": "jurist"})
    assert "Sebas" in text
    assert module.on_pre_llm_call(session_id="key-1", sender_id="") is None


def test_a_session_admitted_under_no_login_leaves_the_order_alone():
    module = module_for(two_logins(), FakeSessionContext(), gateway=fake_gateway(None))

    assert module.render_section({"session_id": "key-1", "profile_name": "jurist"}) == ""
    assert module.on_pre_llm_call(session_id="key-1", sender_id="") is None


def test_the_live_login_is_what_hermes_is_told_this_turn():
    """The prefixed id is the gateway's own fact; the name is the app's."""
    hermes = FakeSessionContext()
    module = module_for(
        [("ef11a9", bag({"ef11a9": {"displayName": "Sebas", "userIdAlt": "alt-1"}}))],
        hermes,
        gateway=fake_gateway("self-hosted:ef11a9"),
    )

    module.on_pre_llm_call(session_id="key-1", sender_id="")
    assert hermes.snapshot()[USER_ID] == "self-hosted:ef11a9"
    assert hermes.snapshot()[USER_NAME] == "Sebas"
    assert hermes.snapshot()[USER_ID_ALT] == "alt-1"
