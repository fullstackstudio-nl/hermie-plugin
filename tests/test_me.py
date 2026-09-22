"""What `/me` answers, and what it answers when it cannot name anybody."""

import types

from hermie_plugin.context import ContextModule
from hermie_plugin.context.live_session import GATEWAY_MODULE, LiveSessions
from hermie_plugin.context.me import FIX, MAX_CHARS, RUNGS
from hermie_plugin.context.render import BY_LIVE_SESSION, BY_ONLY_USER
from hermie_plugin.context.session_vars import (
    SESSION_ID,
    SESSION_NAMES,
    USER_ID,
    USER_ID_ALT,
    USER_NAME,
    SessionVars,
)


class FakeRuntime:
    def __init__(self, sections, settings=None, bot="jurist"):
        self.sections = sections
        self.settings = settings or {}
        self.bot = bot

    def config(self, key, default=None):
        return self.settings.get(key, default)

    def bot_name(self):
        return self.bot

    def app_sections(self):
        return self.sections

    def app_stamp(self):
        return (1, 1)


def bag(users, default=""):
    return {"context": {"v": 1, "default": default, "users": users}}


def person(**overrides):
    entry = {
        "displayName": "Kim",
        "about": "Runs Willow Studio. Prefers short answers.",
        "device": {"model": "iPhone 17 Pro", "os": "iOS 27", "appVersion": "1.4.0"},
        "timezone": "Europe/Amsterdam",
        "locale": "nl-NL",
        "perBot": {"jurist": "Always cite the article number."},
        "updatedAt": 1789957143,
    }
    entry.update(overrides)
    return entry


def gateway_naming(auth_user_id):
    module = types.ModuleType(GATEWAY_MODULE)
    module._sessions = {"sid-1": {"session_key": "key-1", "auth_user_id": auth_user_id}}
    module._session_for_key = lambda key: next(
        (item for item in module._sessions.values() if item.get("session_key") == key), None
    )
    module._session_auth_user_id = lambda session: (session or {}).get("auth_user_id")
    return module


class FakeVariable:
    def __init__(self, value=""):
        self.value = value

    def set(self, value):
        self.value = value


class FakeSessionContext:
    """Hermes' session variables: no user here, only which session this is —
    which is all `/me` gets to go on, since a slash command carries no id."""

    def __init__(self, **values):
        names = (USER_ID, USER_ID_ALT, USER_NAME) + SESSION_NAMES
        self._VAR_MAP = {name: FakeVariable(values.get(name, "")) for name in names}

    def get_session_env(self, name, default=""):
        variable = self._VAR_MAP.get(name)
        return variable.value if variable is not None else default


def module_for(sections, *, login=None, settings=None):
    return ContextModule(
        FakeRuntime(sections, settings),
        session_vars=SessionVars(FakeSessionContext(**{SESSION_ID: "sid-1"})),
        live_sessions=LiveSessions(gateway_naming(login) if login else None),
    )


def test_it_names_the_person_and_how_it_found_them():
    module = module_for(
        [("7f3c02", bag({"7f3c02": person()})), ("ana", bag({"ana": person(displayName="Ana")}))],
        login="self-hosted:7f3c02",
    )

    answer = module.on_me_command()
    assert "Hermie context for jurist" in answer
    assert "Kim" in answer
    assert RUNGS[BY_LIVE_SESSION] in answer
    # Both spellings, because which one is missing is the usual bug.
    assert "self-hosted:7f3c02" in answer
    assert "7f3c02" in answer


def test_it_reports_the_person_the_app_wrote():
    module = module_for([("7f3c02", bag({"7f3c02": person()}))], login="self-hosted:7f3c02")

    answer = module.on_me_command()
    assert "iPhone 17 Pro running iOS 27" in answer
    assert "Europe/Amsterdam, nl-NL" in answer
    assert "Runs Willow Studio" in answer
    assert "Always cite the article number." in answer


def test_it_names_the_key_the_entry_came_from_and_when_it_was_written():
    module = module_for([("7f3c02", bag({"7f3c02": person()}))], login="self-hosted:7f3c02")

    answer = module.on_me_command()
    assert "hermie-app:7f3c02" in answer
    assert "updated 2026-09-21" in answer


def test_the_legacy_key_says_it_is_the_shared_one():
    module = module_for([("", bag({"7f3c02": person()}))])

    answer = module.on_me_command()
    assert "hermie-app" in answer
    assert "shared key" in answer
    assert RUNGS[BY_ONLY_USER] in answer


def test_a_bot_with_no_note_of_its_own_does_not_borrow_one():
    module = module_for([("7f3c02", bag({"7f3c02": person()}))], login="self-hosted:7f3c02")
    module.runtime.bot = "marketing"

    assert "Always cite the article number." not in module.on_me_command()


def test_it_says_nobody_and_what_to_do_about_it():
    module = module_for([("", bag({"7f3c02": person(), "ana": person(displayName="Ana")}))])

    answer = module.on_me_command()
    assert "nobody" in answer
    assert "2 people are registered" in answer
    assert FIX in answer
    assert "Kim" not in answer
    assert "Ana" not in answer


def test_a_sender_nobody_registered_is_shown_rather_than_swallowed():
    """"The gateway said nothing" and "it said someone we do not know" are
    different problems, and only one of them is the app's."""
    module = module_for(
        [("", bag({"7f3c02": person(), "ana": person(displayName="Ana")}))],
        login="oidc:stranger",
    )

    answer = module.on_me_command()
    assert "oidc:stranger" in answer
    assert FIX in answer


def test_nobody_registered_at_all_says_so():
    module = module_for([])

    answer = module.on_me_command()
    assert "registered nobody on this gateway" in answer
    assert FIX in answer


def test_an_essay_cannot_become_the_answer():
    module = module_for([("7f3c02", bag({"7f3c02": person(about="x" * 5000)}))])

    assert len(module.on_me_command()) <= MAX_CHARS


def test_a_broken_report_is_not_a_broken_session():
    module = module_for([("7f3c02", bag({"7f3c02": person()}))])
    module.section = lambda: (_ for _ in ()).throw(RuntimeError("no metadata today"))

    assert "could not work out" in module.on_me_command()
