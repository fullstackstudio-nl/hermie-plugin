"""A person saying "this next turn is mine" in a chat somebody else opened.

Hermes names the login that OPENED a session to every hook for the life of that
session, so on a shared Bot Chat every turn looks like the opener's. The app
closes that gap by claiming the turn over the dashboard, where the request is
authenticated as the person sending it, just before it submits the prompt. These
tests pin both ends: the store the claim lands in, the route that writes it, and
the hook that spends it.
"""

import types

import pytest

from hermie_plugin import contract
from hermie_plugin.context import ContextModule
from hermie_plugin.context import turn_claim
from hermie_plugin.context.live_session import GATEWAY_MODULE, LiveSessions
from hermie_plugin.context.render import BY_CLAIM, BY_HOOK, INTRODUCED, SUPERSEDES_BY_SENDER
from hermie_plugin.context.session_vars import (
    SESSION_ID,
    SESSION_KEY,
    UI_SESSION_ID,
    USER_ID,
    USER_NAME,
    SessionVars,
)
from hermie_plugin.context.turn_claim import TurnClaims

from hermie_plugin.context.session_vars import USER_ID_ALT

OPENER = "oidc:opener-sub"
SENDER = "oidc:sender-sub"
SID = "a1b2c3d4"


class FakeVariable:
    def __init__(self, value=""):
        self.value = value

    def set(self, value):
        self.value = value


class FakeSessionContext:
    """The two things the module uses out of `gateway.session_context`."""

    def __init__(self, **values):
        names = (USER_ID, USER_ID_ALT, USER_NAME, UI_SESSION_ID, SESSION_ID, SESSION_KEY)
        self._VAR_MAP = {name: FakeVariable(values.get(name, "")) for name in names}

    def get_session_env(self, name, default=""):
        variable = self._VAR_MAP.get(name)
        return variable.value if variable is not None else default

    def snapshot(self):
        return {name: variable.value for name, variable in self._VAR_MAP.items() if variable.value}


class FakeRuntime:
    def __init__(self, sections):
        self.sections = sections

    def app_stamp(self):
        return (1, 1)

    def config(self, key, default=None):
        return default

    def bot_name(self):
        return "jurist"

    def app_sections(self):
        return self.sections


def bag(users, default=""):
    return {"context": {"v": 1, "default": default, "users": users}}


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def people():
    """Both people have rows, and the two bags disagree about the default."""
    return [
        ("opener-sub", bag({"opener-sub": {"displayName": "Otto"}}, default="opener-sub")),
        ("sender-sub", bag({"sender-sub": {"displayName": "Sam"}}, default="sender-sub")),
    ]


def module_with(claims, hermes=None):
    hermes = hermes if hermes is not None else FakeSessionContext(**{UI_SESSION_ID: SID, USER_ID: OPENER})
    return ContextModule(
        FakeRuntime(people()),
        session_vars=SessionVars(hermes),
        live_sessions=LiveSessions(types.ModuleType("absent")),
        claims=claims,
    )


def opened_by_the_opener(module):
    """The section is built at session start for whoever opened the chat."""
    text = module.render_section({"session_id": "durable-1", "profile_name": "jurist"})
    return text


# -- the store -----------------------------------------------------------------


def test_a_claim_is_taken_once():
    clock = Clock()
    claims = TurnClaims(clock=clock)
    claims.claim(SID, SENDER)

    assert claims.take(SID) == SENDER
    assert claims.take(SID) == "", "a claim applies to one turn only"


def test_peeking_does_not_spend_the_claim():
    claims = TurnClaims(clock=Clock())
    claims.claim(SID, SENDER)

    assert claims.peek(SID) == SENDER
    assert claims.take(SID) == SENDER


def test_an_expired_claim_is_ignored():
    clock = Clock()
    claims = TurnClaims(clock=clock)
    claims.claim(SID, SENDER)

    clock.now += turn_claim.TTL_SECONDS + 1

    assert claims.take(SID) == ""
    assert len(claims) == 0, "an expired claim is dropped, not kept"


def test_a_claim_just_inside_the_window_still_counts():
    clock = Clock()
    claims = TurnClaims(clock=clock)
    claims.claim(SID, SENDER)

    clock.now += turn_claim.TTL_SECONDS - 1

    assert claims.take(SID) == SENDER


def test_a_claim_for_another_session_does_not_apply():
    claims = TurnClaims(clock=Clock())
    claims.claim("ffff0000", SENDER)

    assert claims.take(SID) == ""
    assert claims.take("ffff0000") == SENDER


def test_the_last_claim_wins():
    """Two people claiming one session inside the window: the later one counts."""
    claims = TurnClaims(clock=Clock())
    claims.claim(SID, OPENER)
    claims.claim(SID, SENDER)

    assert claims.take(SID) == SENDER
    assert claims.take(SID) == ""


def test_the_store_stays_bounded():
    clock = Clock()
    claims = TurnClaims(clock=clock)
    for number in range(turn_claim.MAX_CLAIMS * 3):
        clock.now += 0.001
        claims.claim(f"s{number:06d}", SENDER)

    assert len(claims) == turn_claim.MAX_CLAIMS
    assert claims.take("s000000") == "", "the oldest claim is the one that went"
    assert claims.take(f"s{turn_claim.MAX_CLAIMS * 3 - 1:06d}") == SENDER


def test_an_alias_is_matched_only_when_the_runtime_id_is_unknown():
    """The durable ids are a fallback; the runtime id is the exact match.

    Two windows can hold two runtime sessions on one stored session. A turn that
    knows its runtime id must never spend a claim made for the other one just
    because they share a session key.
    """
    claims = TurnClaims(clock=Clock())
    claims.claim(SID, SENDER, aliases=("key-1", "durable-1"))

    assert claims.take("other-sid", aliases=("key-1",)) == ""
    assert claims.take("", aliases=("durable-1",)) == SENDER


def test_the_store_is_shared_by_every_copy_of_the_plugin():
    """The dashboard imports the plugin under one name, Hermes under another."""
    assert turn_claim.shared() is turn_claim.shared()
    assert isinstance(turn_claim.shared(), TurnClaims)


@pytest.mark.parametrize(
    "value",
    ["", "   ", None, 12, ["a1b2c3d4"], "a" * 200, "../etc", "a b", "a\nb", "a/b"],
)
def test_a_malformed_session_id_is_refused(value):
    assert turn_claim.valid_session_id(value) is None


@pytest.mark.parametrize("value", [SID, "20260922_101112_abc123", "key-1", "x.y:z"])
def test_a_well_formed_session_id_is_accepted(value):
    assert turn_claim.valid_session_id(value) == value


def test_the_identity_is_spelled_the_way_the_hook_spells_it():
    session = types.SimpleNamespace(provider=" oidc ", user_id=" sender-sub ")

    assert turn_claim.identity_of(session) == SENDER


@pytest.mark.parametrize(
    "session",
    [None, types.SimpleNamespace(provider="", user_id="x"), types.SimpleNamespace(provider="oidc", user_id="")],
)
def test_no_signed_in_person_is_no_identity(session):
    assert turn_claim.identity_of(session) == ""


# -- the hook ------------------------------------------------------------------


def test_a_claim_makes_the_turn_resolve_to_the_claimer_although_the_hook_names_the_opener():
    claims = TurnClaims(clock=Clock())
    hermes = FakeSessionContext(**{UI_SESSION_ID: SID, USER_ID: OPENER})
    module = module_with(claims, hermes)
    assert "Otto" in opened_by_the_opener(module)

    claims.claim(SID, SENDER)
    added = module.on_pre_llm_call(session_id="durable-1", sender_id=OPENER)

    assert added is not None, "the claimer never reached the bot"
    assert added["context"].startswith(SUPERSEDES_BY_SENDER)
    assert "Sam" in added["context"] and "Otto" not in added["context"]
    assert hermes.snapshot()[USER_ID] == SENDER
    assert hermes.snapshot()[USER_NAME] == "Sam"


def test_the_claim_is_spent_by_the_turn_that_used_it():
    claims = TurnClaims(clock=Clock())
    hermes = FakeSessionContext(**{UI_SESSION_ID: SID, USER_ID: OPENER})
    module = module_with(claims, hermes)
    opened_by_the_opener(module)

    claims.claim(SID, SENDER)
    module.on_pre_llm_call(session_id="durable-1", sender_id=OPENER)
    assert len(claims) == 0

    # The next turn carries no claim, so it is the opener's again, and says so.
    added = module.on_pre_llm_call(session_id="durable-1", sender_id=OPENER)
    assert added is not None and "Otto" in added["context"] and "Sam" not in added["context"]


def test_an_expired_claim_leaves_the_turn_to_the_hook():
    clock = Clock()
    claims = TurnClaims(clock=clock)
    module = module_with(claims)
    opened_by_the_opener(module)

    claims.claim(SID, SENDER)
    clock.now += turn_claim.TTL_SECONDS + 5

    assert module.on_pre_llm_call(session_id="durable-1", sender_id=OPENER) is None
    assert module.sender_with_source(OPENER, "durable-1") == (OPENER, BY_HOOK)


def test_a_claim_for_another_session_leaves_this_turn_alone():
    claims = TurnClaims(clock=Clock())
    module = module_with(claims)
    opened_by_the_opener(module)

    claims.claim("ffff0000", SENDER)

    assert module.on_pre_llm_call(session_id="durable-1", sender_id=OPENER) is None
    assert claims.peek("ffff0000") == SENDER, "somebody else's claim was spent"


def test_the_claim_is_found_by_the_durable_id_where_no_runtime_id_is_bound():
    claims = TurnClaims(clock=Clock())
    hermes = FakeSessionContext(**{SESSION_KEY: "key-1", SESSION_ID: "durable-1"})
    module = module_with(claims, hermes)

    claims.claim(SID, SENDER, aliases=("key-1", "durable-1"))

    assert module.sender_with_source(OPENER, "durable-1", take=True) == (SENDER, BY_CLAIM)


def test_building_the_prompt_reads_the_claim_without_spending_it():
    """The section built for this turn describes the claimer; the hook then agrees."""
    claims = TurnClaims(clock=Clock())
    hermes = FakeSessionContext(**{UI_SESSION_ID: SID, USER_ID: OPENER})
    module = module_with(claims, hermes)

    claims.claim(SID, SENDER)
    text = opened_by_the_opener(module)
    assert "Sam" in text and "Otto" not in text
    assert claims.peek(SID) == SENDER

    assert module.on_pre_llm_call(session_id="durable-1", sender_id=OPENER) is None
    assert len(claims) == 0


def test_me_names_the_claim_as_the_rung_that_answered():
    from hermie_plugin.context.me import RUNGS

    claims = TurnClaims(clock=Clock())
    module = module_with(claims)
    claims.claim(SID, SENDER)

    answer = module.on_me_command("")

    assert "Sam" in answer and RUNGS[BY_CLAIM] in answer
    assert claims.peek(SID) == SENDER, "/me spent the claim meant for the turn"


def test_a_turn_with_no_claim_is_exactly_what_it_was():
    module = module_with(TurnClaims(clock=Clock()))
    opened_by_the_opener(module)

    assert module.on_pre_llm_call(session_id="durable-1", sender_id=OPENER) is None


def test_the_advert_carries_the_capability():
    module = module_with(TurnClaims(clock=Clock()))

    assert contract.CAP_CONTEXT_TURN_CLAIM == "context.turn_claim"
    assert contract.CAP_CONTEXT_TURN_CLAIM in module.capabilities()


# -- the route -----------------------------------------------------------------

fastapi = pytest.importorskip("fastapi", reason="FastAPI ships with the Hermes runtime")

import importlib.util  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

PREFIX = "/api/plugins/hermie"


def load_route_module():
    """Import the api file the way core does, by path and not as a package."""
    name = "hermes_dashboard_plugin_hermie"
    if name in sys.modules:
        return sys.modules[name]
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(name, root / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

PATH = f"{PREFIX}/context/turn"


def app_signed_in_as(routes, session=None, *, gateway=None):
    """The route behind a stand-in for Hermes' auth middleware.

    The middleware attaches the verified session to `request.state.session`;
    this does the same, or nothing, which is what a gateway without a login does.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.middleware("http")
    async def signed_in(request, call_next):
        if session is not None:
            request.state.session = session
        return await call_next(request)

    app.include_router(routes.router, prefix=PREFIX)
    return TestClient(app, raise_server_exceptions=False)


def person(provider="oidc", user_id="sender-sub"):
    return types.SimpleNamespace(provider=provider, user_id=user_id, email="", display_name="")


@pytest.fixture
def store(monkeypatch):
    claims = TurnClaims(clock=Clock())
    monkeypatch.setattr(turn_claim, "shared", lambda: claims)
    return claims


@pytest.fixture
def routes():
    return load_route_module()


def test_the_route_records_the_signed_in_person(routes, store):
    response = app_signed_in_as(routes, person()).post(PATH, json={"session_id": SID})

    assert response.status_code == 204, response.text
    assert response.content == b""
    assert store.peek(SID) == SENDER


def test_the_identity_comes_from_the_login_and_never_from_the_body(routes, store):
    response = app_signed_in_as(routes, person()).post(
        PATH,
        json={"session_id": SID, "identity": OPENER, "user_id": "opener-sub", "provider": "oidc"},
    )

    assert response.status_code == 204
    assert store.peek(SID) == SENDER


@pytest.mark.parametrize(
    "body",
    [{}, {"session_id": ""}, {"session_id": 7}, {"session_id": "../x"}, {"session_id": "a" * 300}, [SID], "x"],
)
def test_the_route_refuses_bad_input(routes, store, body):
    response = app_signed_in_as(routes, person()).post(PATH, json=body)

    assert response.status_code == 400, response.text
    assert len(store) == 0


def test_a_body_that_is_not_json_is_refused(routes, store):
    response = app_signed_in_as(routes, person()).post(
        PATH, content=b"{not json", headers={"content-type": "application/json"}
    )

    assert response.status_code == 400


def test_a_request_that_names_no_person_is_refused(routes, store):
    """A gateway with no login, or a service token: there is nobody to claim for."""
    response = app_signed_in_as(routes, None).post(PATH, json={"session_id": SID})

    assert response.status_code == 403
    assert len(store) == 0


def test_the_route_records_the_durable_ids_beside_the_runtime_one(routes, store, monkeypatch):
    server = types.ModuleType(GATEWAY_MODULE)
    server._sessions = {
        SID: {"session_key": "key-1", "agent": types.SimpleNamespace(session_id="durable-1")}
    }
    monkeypatch.setitem(sys.modules, GATEWAY_MODULE, server)

    assert app_signed_in_as(routes, person()).post(PATH, json={"session_id": SID}).status_code == 204
    assert store.take("", aliases=("durable-1",)) == SENDER


def test_the_route_is_inside_the_gated_prefix(routes):
    from fastapi import FastAPI
    from fastapi.routing import APIRoute

    app = FastAPI()
    app.include_router(routes.router, prefix=PREFIX)
    methods = {
        route.path: route.methods for route in app.routes if isinstance(route, APIRoute)
    }

    assert methods[PATH] == {"POST"}
