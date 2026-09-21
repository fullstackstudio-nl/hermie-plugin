"""Filling in Hermes' session user variables, and leaving them alone."""

from hermie_plugin.context import ContextModule
from hermie_plugin.context.render import read_section
from hermie_plugin.context.session_vars import USER_ID, USER_ID_ALT, USER_NAME, SessionVars


class FakeVariable:
    """Stands in for one of Hermes' session `ContextVar`s."""

    def __init__(self, value=""):
        self.value = value

    def set(self, value):
        self.value = value


class FakeSessionContext:
    """The two things the shim uses out of `gateway.session_context`."""

    def __init__(self, **values):
        self._VAR_MAP = {name: FakeVariable(values.get(name, "")) for name in (USER_ID, USER_ID_ALT, USER_NAME)}

    def get_session_env(self, name, default=""):
        variable = self._VAR_MAP.get(name)
        return variable.value if variable is not None else default

    def snapshot(self):
        return {name: variable.value for name, variable in self._VAR_MAP.items() if variable.value}


class FakeRuntime:
    def __init__(self, sections, settings=None):
        self.sections = sections
        self.settings = settings or {}

    def config(self, key, default=None):
        return self.settings.get(key, default)

    def bot_name(self):
        return "jurist"

    def app_sections(self):
        return self.sections


def bag(users, default=""):
    return {"context": {"v": 1, "default": default, "users": users}}


def module_for(sections, hermes, settings=None):
    return ContextModule(FakeRuntime(sections, settings), session_vars=SessionVars(hermes))


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
