"""Reading the login off the gateway's own table of live sessions."""

import sys
import types

from hermie_plugin.context.live_session import GATEWAY_MODULE, LiveSessions


def record(session_key="key-1", auth_user_id="basic:max", **extra):
    entry = {"session_key": session_key, "auth_user_id": auth_user_id}
    entry.update(extra)
    return entry


def fake_server(sessions, *, stamps=True):
    """The three names this module reads out of the dashboard's server."""
    module = types.ModuleType(GATEWAY_MODULE)
    module._sessions = sessions

    def _session_for_key(session_key):
        return next(
            (item for item in sessions.values() if item.get("session_key") == session_key), None
        )

    module._session_for_key = _session_for_key
    if stamps:
        module._session_auth_user_id = lambda session: (session or {}).get("auth_user_id")
    return module


def test_the_runtime_id_finds_the_record():
    live = LiveSessions(fake_server({"sid-1": record()}))

    assert live.login("sid-1") == "basic:max"


def test_the_session_key_finds_it_too():
    """The table is keyed by the runtime id; a hook mostly carries the key."""
    live = LiveSessions(fake_server({"sid-1": record(session_key="key-1")}))

    assert live.login("key-1") == "basic:max"


def test_the_first_id_that_names_a_session_wins():
    live = LiveSessions(fake_server({"sid-1": record(auth_user_id="oidc:ana")}))

    assert live.login("", "nothing-like-it", "sid-1", "key-1") == "oidc:ana"


def test_an_id_that_names_nothing_is_nobody():
    live = LiveSessions(fake_server({"sid-1": record()}))

    assert live.login("sid-2") == ""


def test_a_session_admitted_under_no_login_is_nobody():
    """An ungated gateway has a record and no identity on it."""
    live = LiveSessions(fake_server({"sid-1": record(auth_user_id=None)}))

    assert live.login("sid-1") == ""


def test_a_gateway_that_does_not_stamp_the_login_is_nobody():
    live = LiveSessions(fake_server({"sid-1": record()}, stamps=False))

    assert live.login("sid-1") == ""


def test_a_server_that_changed_shape_costs_the_sender_not_the_turn():
    module = fake_server({"sid-1": record()})

    def boom(session):
        raise RuntimeError("private module, private rules")

    module._session_auth_user_id = boom
    assert LiveSessions(module).login("sid-1") == ""


def test_a_process_without_a_dashboard_answers_nobody_and_imports_nothing():
    """A messaging gateway or the CLI must not gain a server module for this."""
    assert GATEWAY_MODULE not in sys.modules

    live = LiveSessions()
    assert live.available() is False
    assert live.login("sid-1") == ""
    assert GATEWAY_MODULE not in sys.modules


def test_the_running_gateway_is_found_where_it_already_is(monkeypatch):
    monkeypatch.setitem(sys.modules, GATEWAY_MODULE, fake_server({"sid-1": record()}))

    live = LiveSessions()
    assert live.available() is True
    assert live.login("sid-1") == "basic:max"
