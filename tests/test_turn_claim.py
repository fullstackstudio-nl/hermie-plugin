"""A person saying "this next turn is mine" in a chat somebody else opened.

Hermes names the login that OPENED a session to every hook for the life of that
session, so on a shared Bot Chat every turn looks like the opener's. The app
closes that gap by claiming the turn over the dashboard, where the request is
authenticated as the person sending it, just before it submits the prompt. These
tests pin both ends: the store the claim lands in, the route that writes it, and
the hook that spends it.
"""

import importlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest

from hermie_plugin import contract
from hermie_plugin.context import ContextModule
from hermie_plugin.context import turn_claim
from hermie_plugin.context.live_session import GATEWAY_MODULE, LiveSessions
from hermie_plugin.context.render import (
    BY_CLAIM,
    BY_HOOK,
    BY_PLATFORM,
    INTRODUCED,
    SENDER_VERIFIED,
    SUPERSEDES_BY_SENDER,
)
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
        auth_providers=lambda: ("oidc", "basic"),
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


def load_copy(name):
    """The whole package again, under another module name, as Hermes loads it."""
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        name, root / "__init__.py", submodule_search_locations=[str(root)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return importlib.import_module(f"{name}.context.turn_claim")


@pytest.fixture
def two_copies(monkeypatch):
    """The hooks' copy and the dashboard's copy, in one process, with an empty slot."""
    monkeypatch.delitem(sys.modules, turn_claim.REGISTRY, raising=False)
    names = ("hermie_hooks_copy", "hermie_dashboard_copy")
    yield tuple(load_copy(name) for name in names)
    for key in [key for key in sys.modules if key.split(".")[0] in names]:
        del sys.modules[key]


def test_a_claim_made_through_one_copy_is_spent_through_the_other(two_copies):
    """The dashboard's route and Hermes' hook run different copies of this package.

    Each copy has its own `TurnClaims` class. A store that only one of them
    accepts is two stores, and a claim made through the route never reaches the
    turn.
    """
    hooks, dashboard = two_copies
    assert hooks.TurnClaims is not dashboard.TurnClaims

    dashboard.shared().claim(SID, SENDER)

    assert hooks.shared() is dashboard.shared()
    assert hooks.shared().peek(SID) == SENDER
    assert hooks.shared().take(SID) == SENDER
    assert dashboard.shared().peek(SID) == ""


def test_me_on_todays_gateway_leaves_the_claim_where_it_is():
    """No session bound during a command: nothing to find, nothing spent."""
    claims = TurnClaims(clock=Clock())
    module = module_with(claims, FakeSessionContext(**{USER_ID: OPENER}))
    claims.claim(SID, SENDER)

    module.on_me_command("")

    assert claims.peek(SID) == SENDER


def test_a_reloaded_copy_still_finds_the_store(two_copies):
    """Hermes evicts and re-executes a plugin module on reload: yet another class."""
    hooks, dashboard = two_copies
    dashboard.shared().claim(SID, SENDER)

    for key in [key for key in sys.modules if key.split(".")[0] == "hermie_hooks_copy"]:
        del sys.modules[key]
    reloaded = load_copy("hermie_hooks_copy")

    assert reloaded.TurnClaims is not hooks.TurnClaims
    assert reloaded.shared().take(SID) == SENDER


def test_the_copies_agree_whichever_asks_first(two_copies):
    hooks, dashboard = two_copies
    hooks.shared().claim(SID, SENDER)

    assert dashboard.shared().take(SID) == SENDER


def test_the_private_store_is_said_out_loud(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, turn_claim.REGISTRY, types.ModuleType(turn_claim.REGISTRY))

    with caplog.at_level("WARNING"):
        turn_claim.shared()

    assert "private turn-claim store" in caplog.text


def test_an_empty_sender_is_stood_in_for_only_on_a_dashboard_turn():
    """A scheduled or background turn has no runtime id and no sender: not claimed."""
    claims = TurnClaims(clock=Clock())
    hermes = FakeSessionContext(**{SESSION_KEY: "key-1"})
    module = module_with(claims, hermes)
    claims.claim(SID, SENDER, aliases=("key-1",))

    assert module.sender_with_source("", "key-1", take=True) == ("", "")
    assert claims.peek(SID) == SENDER


def test_an_empty_sender_on_a_dashboard_turn_is_the_claimer():
    claims = TurnClaims(clock=Clock())
    module = module_with(claims, FakeSessionContext(**{UI_SESSION_ID: SID}))
    claims.claim(SID, SENDER)

    assert module.sender_with_source("", "durable-1", take=True) == (SENDER, BY_CLAIM)


def test_a_newer_claim_is_judged_on_its_own_before_it_is_spent():
    """The claim spent is the claim judged, so a refused one is never spent."""
    claims = TurnClaims(clock=Clock())
    claims.claim(SID, SENDER)

    assert claims.take_if(SID, (), lambda identity: False) == ""
    assert claims.peek(SID) == SENDER
    assert claims.take_if(SID, (), lambda identity: identity == SENDER) == SENDER
    assert len(claims) == 0


def test_a_store_of_another_shape_is_not_used(monkeypatch):
    holder = types.ModuleType(turn_claim.REGISTRY)
    older = object()
    holder.stores = {turn_claim.SHAPE - 1: older}
    monkeypatch.setitem(sys.modules, turn_claim.REGISTRY, holder)

    store = turn_claim.shared()

    assert isinstance(store, TurnClaims) and store is not older
    assert holder.stores[turn_claim.SHAPE] is store
    assert holder.stores[turn_claim.SHAPE - 1] is older, "the other shape's store was left alone"


def test_a_slot_this_code_cannot_read_falls_back_to_a_fresh_store(monkeypatch):
    monkeypatch.setitem(sys.modules, turn_claim.REGISTRY, types.ModuleType(turn_claim.REGISTRY))
    assert isinstance(turn_claim.shared(), TurnClaims)

    holder = types.ModuleType(turn_claim.REGISTRY)
    holder.stores = {turn_claim.SHAPE: object()}
    monkeypatch.setitem(sys.modules, turn_claim.REGISTRY, holder)
    assert isinstance(turn_claim.shared(), TurnClaims)


def test_a_claim_lasts_thirty_seconds():
    assert turn_claim.TTL_SECONDS == 30.0


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


def test_a_turn_without_a_runtime_id_never_matches_a_claim_key():
    """The takeover: a messaging turn whose session key equals a claim's key.

    Keys are runtime ids and the route only accepts live ones, but the store
    must not rely on that: without a runtime id a turn is matched through the
    aliases recorded off the live record and nothing else.
    """
    claims = TurnClaims(clock=Clock())
    hermes = FakeSessionContext(**{SESSION_KEY: "agent:main:telegram:dm:12345"})
    module = module_with(claims, hermes)

    claims.claim("agent:main:telegram:dm:12345", SENDER)

    assert module.sender_with_source("", "agent:main:telegram:dm:12345", take=True) == ("", "")
    assert claims.take("", aliases=("agent:main:telegram:dm:12345",)) == ""


def test_building_the_prompt_ignores_a_claim_that_is_already_sitting_there():
    """The frozen section never asks the claim store (Task 1 of the plan): a
    claim answers for a submit, and this render runs before any turn of the
    session has, so a claim already sitting there proves nothing about it. The
    prompt is built for the opener the session variables name, cautioned,
    exactly as if no claim had been made at all — and the claim itself is left
    untouched for the turn it actually was made for.
    """
    claims = TurnClaims(clock=Clock())
    hermes = FakeSessionContext(**{UI_SESSION_ID: SID, USER_ID: OPENER})
    module = module_with(claims, hermes)

    claims.claim(SID, SENDER)
    text = opened_by_the_opener(module)
    assert "Otto" in text and "Sam" not in text
    assert claims.peek(SID) == SENDER, "the frozen section spent or read a claim it must not touch"

    # The turn that follows still finds the claim waiting and spends it,
    # resolving to the claimer — that half of the feature is untouched. What
    # is gone is the assertion: nothing today may say the gateway checked who
    # sent this turn (VERIFIED_RUNGS is empty until a claim is bound to the
    # exact submitted text, see DESIGN.md "Decision (2026-09-22)").
    added = module.on_pre_llm_call(session_id="durable-1", sender_id=OPENER)
    assert added is not None
    assert "Sam" in added["context"] and "Otto" not in added["context"]
    assert SENDER_VERIFIED.split("{")[0] not in added["context"]
    assert len(claims) == 0


def test_me_names_the_claim_where_the_session_is_bound():
    """Only where a gateway binds the session for a command.

    Hermes today calls a plugin command with no session variables bound, so on
    a real dashboard `/me` sees no claim at all. This pins what the report says
    on a gateway that does bind them, not what today's gateway does.
    """
    from hermie_plugin.context.me import RUNGS

    claims = TurnClaims(clock=Clock())
    module = module_with(claims)
    claims.claim(SID, SENDER)

    answer = module.on_me_command("")

    assert "Sam" in answer and RUNGS[BY_CLAIM] in answer


def test_me_spends_a_claim_only_where_the_session_is_bound():
    """Best effort, and only on a gateway that binds the session for a command.

    Hermes today calls a plugin command with no session variables bound, so the
    discard finds nothing on a real dashboard. The guarantee that a command
    leaves no claim behind is the app's: it never claims for one.
    """
    claims = TurnClaims(clock=Clock())
    hermes = FakeSessionContext(**{UI_SESSION_ID: SID, USER_ID: OPENER})
    module = module_with(claims, hermes)
    opened_by_the_opener(module)

    claims.claim(SID, SENDER)
    module.on_me_command("")

    assert len(claims) == 0
    assert module.on_pre_llm_call(session_id="durable-1", sender_id=OPENER) is None
    assert hermes.snapshot()[USER_ID] == OPENER


def test_the_session_variables_are_rewritten_for_the_claimer_not_the_opener():
    claims = TurnClaims(clock=Clock())
    hermes = FakeSessionContext(**{UI_SESSION_ID: SID, USER_ID: OPENER, USER_NAME: "Otto"})
    module = module_with(claims, hermes)
    opened_by_the_opener(module)

    claims.claim(SID, SENDER)
    module.on_pre_llm_call(session_id="durable-1", sender_id=OPENER)

    assert hermes.snapshot()[USER_ID] == SENDER
    assert hermes.snapshot()[USER_NAME] == "Sam"


@pytest.mark.parametrize(
    "named,rung",
    [
        # No provider at all: this gateway cannot place it, so it is neither
        # overridden by a claim nor reported as a sender it checked.
        ("12345", BY_HOOK),
        ("jurist", BY_HOOK),
        # A provider the dashboard does not sign people in with. The platform
        # named this sender for this message, so it is the one hook sender
        # worth reporting as verified.
        ("telegram:12345", BY_PLATFORM),
    ],
)
def test_a_sender_the_dashboard_did_not_admit_is_not_overridden(named, rung):
    """A platform user or a bot handing a turn over was named correctly by Hermes."""
    claims = TurnClaims(clock=Clock())
    hermes = FakeSessionContext(**{UI_SESSION_ID: SID, USER_ID: OPENER})
    module = module_with(claims, hermes)

    claims.claim(SID, SENDER)

    assert module.sender_with_source(named, "durable-1", take=True) == (named, rung)
    assert claims.peek(SID) == SENDER, "a claim that did not apply was spent"


def test_a_sender_spelled_as_another_dashboard_login_is_overridden():
    claims = TurnClaims(clock=Clock())
    module = module_with(claims)

    claims.claim(SID, SENDER)

    assert module.sender_with_source("basic:opener", "durable-1", take=True) == (SENDER, BY_CLAIM)


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


@pytest.fixture(autouse=True)
def live(monkeypatch):
    """The dashboard's session table, with one live runtime session in it."""
    server = types.ModuleType(GATEWAY_MODULE)
    server._sessions = {
        SID: {
            "session_key": "key-1",
            "agent": types.SimpleNamespace(session_id="durable-1"),
            # The login the dashboard stamped on the record when it admitted
            # the WebSocket. The route checks a claimer against this.
            "auth_user_id": SENDER,
        }
    }
    server._session_auth_user_id = lambda record: str(record.get("auth_user_id") or "")
    monkeypatch.setitem(sys.modules, GATEWAY_MODULE, server)
    return server


@pytest.mark.parametrize("session_id", ["key-1", "durable-1", "agent:main:telegram:dm:12345", "ffff0000"])
def test_an_id_that_is_not_a_live_runtime_session_is_refused(routes, store, session_id):
    """A session key, a durable id, another platform's key, a closed session."""
    response = app_signed_in_as(routes, person()).post(PATH, json={"session_id": session_id})

    assert response.status_code == 404, response.text
    assert len(store) == 0


def test_a_gateway_with_no_session_table_refuses_every_claim(routes, store, monkeypatch):
    monkeypatch.delitem(sys.modules, GATEWAY_MODULE)

    assert app_signed_in_as(routes, person()).post(PATH, json={"session_id": SID}).status_code == 404
    assert len(store) == 0


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", ""])
def test_a_body_not_sent_as_json_is_refused(routes, store, content_type):
    headers = {"content-type": content_type} if content_type else {}
    response = app_signed_in_as(routes, person()).post(
        PATH, content=b'{"session_id": "a1b2c3d4"}', headers=headers
    )

    assert response.status_code == 415, response.text
    assert len(store) == 0


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


def test_a_signed_in_stranger_cannot_claim_somebody_elses_session(routes, store):
    """Being signed in is not being signed in to THAT session.

    Hermes hands every authenticated dashboard caller every route with nothing
    for a route to check an owner against, so a runtime session id learned by
    any means would otherwise be enough to claim that session's next turn — and
    what a claim buys is a sentence telling the model the gateway VERIFIED who
    sent that turn.
    """
    response = app_signed_in_as(routes, person(user_id="opener-sub")).post(
        PATH, json={"session_id": SID}
    )

    assert response.status_code == 403, response.text
    assert len(store) == 0


def test_a_session_admitted_under_nobody_authorises_nobody(routes, store, live):
    """Nothing to check the caller against is not the same as anybody will do."""
    live._sessions[SID]["auth_user_id"] = ""

    assert app_signed_in_as(routes, person()).post(PATH, json={"session_id": SID}).status_code == 403
    assert len(store) == 0


def test_a_gateway_that_does_not_stamp_the_login_authorises_nobody(routes, store, live):
    del live._session_auth_user_id

    assert app_signed_in_as(routes, person()).post(PATH, json={"session_id": SID}).status_code == 403
    assert len(store) == 0


def test_the_claimer_is_matched_across_the_provider_prefix(routes, store, live):
    """The one comparison this repo has, so the two spellings of an id agree."""
    live._sessions[SID]["auth_user_id"] = "sender-sub"

    assert app_signed_in_as(routes, person()).post(PATH, json={"session_id": SID}).status_code == 204
    assert store.peek(SID) == SENDER


def test_the_route_records_the_durable_ids_beside_the_runtime_one(routes, store):
    assert app_signed_in_as(routes, person()).post(PATH, json={"session_id": SID}).status_code == 204
    assert store.take("", aliases=("durable-1",)) == SENDER


def test_the_claim_is_keyed_by_the_runtime_id_only(routes, store):
    assert app_signed_in_as(routes, person()).post(PATH, json={"session_id": SID}).status_code == 204
    assert set(store.claims) == {SID}


def test_the_route_is_inside_the_gated_prefix(routes):
    from fastapi import FastAPI
    from fastapi.routing import APIRoute

    app = FastAPI()
    app.include_router(routes.router, prefix=PREFIX)
    methods = {
        route.path: route.methods for route in app.routes if isinstance(route, APIRoute)
    }

    assert methods[PATH] == {"POST"}
