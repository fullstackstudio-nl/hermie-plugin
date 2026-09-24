"""What `/me` answers, since HERM-119 removed the profile it used to report."""

import types

from hermie_plugin.context import ContextModule
from hermie_plugin.context.live_session import GATEWAY_MODULE, LiveSessions
from hermie_plugin.context.me import MAX_CHARS, RUNGS
from hermie_plugin.context.render import BY_LIVE_SESSION, VERIFIED_RUNGS
from hermie_plugin.context.session_vars import SESSION_ID, SESSION_NAMES, USER_ID, SessionVars


class FakeRuntime:
    def __init__(self, settings=None, bot="jurist"):
        self.settings = settings or {}
        self.bot = bot

    def config(self, key, default=None):
        return self.settings.get(key, default)

    def bot_name(self):
        return self.bot


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
        names = (USER_ID,) + SESSION_NAMES
        self._VAR_MAP = {name: FakeVariable(values.get(name, "")) for name in names}

    def get_session_env(self, name, default=""):
        variable = self._VAR_MAP.get(name)
        return variable.value if variable is not None else default


def module_for(*, login=None, settings=None):
    return ContextModule(
        FakeRuntime(settings),
        session_vars=SessionVars(FakeSessionContext(**{SESSION_ID: "sid-1"})),
        live_sessions=LiveSessions(gateway_naming(login) if login else None),
    )


def test_it_names_the_login_and_how_it_found_it():
    module = module_for(login="self-hosted:7f3c02")

    answer = module.on_me_command()

    assert "Hermie context for jurist" in answer
    assert "self-hosted:7f3c02" in answer
    assert RUNGS[BY_LIVE_SESSION] in answer


def test_it_says_whether_the_rung_is_confirmed():
    module = module_for(login="self-hosted:7f3c02")

    answer = module.on_me_command()

    # `VERIFIED_RUNGS` is empty today, so every rung reports unconfirmed.
    assert BY_LIVE_SESSION not in VERIFIED_RUNGS
    assert "Confirmed" in answer
    assert "no — the gateway has not confirmed who is sending" in answer


def test_it_says_nobody_when_the_gateway_has_named_no_one():
    module = module_for()

    answer = module.on_me_command()

    assert "Login:      nobody" in answer
    assert "the gateway has not named anyone for this session" in answer


def test_it_never_mentions_a_setting_that_no_longer_exists():
    """HERM-119 removed Settings → Context; the report must not send anyone
    looking for it, confirmed or not."""
    named = module_for(login="self-hosted:7f3c02").on_me_command()
    nobody = module_for().on_me_command()

    for answer in (named, nobody):
        assert "Settings" not in answer
        assert "sharing notice" not in answer


def test_it_adds_nothing_beyond_the_login():
    """No device, no about text, no per-bot note — there is no profile left."""
    answer = module_for(login="self-hosted:7f3c02").on_me_command()

    assert "Device" not in answer
    assert "About" not in answer
    assert "Nothing beyond this login is added to this bot's prompt." in answer


def test_an_absurd_bot_name_cannot_break_the_cap():
    module = module_for(login="self-hosted:7f3c02")
    module.runtime.bot = "b" * 5000

    assert len(module.on_me_command()) <= MAX_CHARS


def test_a_broken_report_is_not_a_broken_session():
    module = module_for(login="self-hosted:7f3c02")
    module.sender_with_source = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no metadata today"))

    assert "could not work out" in module.on_me_command()
