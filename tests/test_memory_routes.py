"""The routes themselves: what they refuse, and what gates them.

These need FastAPI, which is a Hermes runtime dependency and not one of this
plugin's — `python_dependencies` in the manifest is still empty and stays that
way. So they skip on a bare checkout and run wherever Hermes is installed, which
is every machine that could actually serve them.

The auth tests are the ones worth reading twice. This plugin's router carries no
authentication of its own, deliberately: Hermes gates `/api/plugins/...` with
process-wide middleware that answers 401 before a handler runs, and there is no
per-route dependency to add. What can still go wrong is the plugin *escaping*
that gate — by landing a path outside the prefix, or by opening a WebSocket,
which HTTP middleware does not gate at all. Both are asserted here, and the
third test asserts the gate really covers us by checking core's own public-path
allowlist.

The `raw` tests are the only ones here that reach a filesystem, because that
route is the only one that reads a file rather than asking the store. They run
against a fake gateway built in `gateway` below — two profiles with real
directories under `tmp_path`, switches to flip and a provider list to fill in —
so what they pin is the answer a caller gets: which documents are there, which
are absent, what a backend says when it cannot be listed, and that nothing on
disk moved.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="FastAPI ships with the Hermes runtime, not with this plugin")

from fastapi import FastAPI  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PREFIX = "/api/plugins/hermie"


def load_route_module():
    """Import the api file the way core does, by path and not as a package."""
    name = "hermes_dashboard_plugin_hermie"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def routes():
    return load_route_module()


# -- the manifest core reads -------------------------------------------------


def test_the_manifest_points_at_the_api_file_that_exists():
    import json

    manifest = json.loads((ROOT / "dashboard" / "manifest.json").read_text())

    assert manifest["name"] == "hermie", "the manifest name is the URL prefix"
    assert (ROOT / "dashboard" / manifest["api"]).is_file()
    # Core refuses an absolute or escaping api path outright.
    assert not Path(manifest["api"]).is_absolute() and ".." not in manifest["api"]


def test_the_entry_core_will_fetch_is_actually_there():
    """Core defaults `entry` to dist/index.js whether or not a plugin ships UI."""
    import json

    manifest = json.loads((ROOT / "dashboard" / "manifest.json").read_text())

    assert (ROOT / "dashboard" / manifest.get("entry", "dist/index.js")).is_file()


# -- auth ---------------------------------------------------------------------


def test_the_plugin_opens_no_websocket(routes):
    """HTTP middleware does not gate an upgrade, so a WS here would be unguarded.

    Core's own kanban plugin has to hand-roll a check for exactly this reason.
    The cheapest way not to get that wrong is to have no socket at all.
    """
    kinds = {type(route).__name__ for route in routes.router.routes}

    assert "APIWebSocketRoute" not in kinds


def test_every_path_lands_inside_the_gated_prefix(routes):
    """A path outside /api/plugins/hermie/ would be a route nothing is gating."""
    app = FastAPI()
    app.include_router(routes.router, prefix=PREFIX)

    paths = [route.path for route in app.routes if isinstance(route, APIRoute)]

    assert paths, "no routes were mounted at all"
    for path in paths:
        assert path.startswith(PREFIX + "/"), path


def test_the_prefix_is_not_on_hermes_own_public_allowlist():
    """The gate only bites if /api/plugins is outside the unauthenticated set."""
    public = pytest.importorskip(
        "hermes_cli.dashboard_auth.public_paths", reason="needs Hermes itself"
    )
    allowed = {
        str(item)
        for name in dir(public)
        if name.isupper()
        for item in (getattr(public, name) or ())
        if isinstance(item, str)
    }

    assert not any(path.startswith("/api/plugins") for path in allowed)


def test_the_router_adds_no_auth_of_its_own(routes):
    """Stated as a test so nobody "fixes" it by inventing a check that lies.

    A dependency here could only re-read the same shared token the middleware
    already checked. The one route that needs to know who is calling reads the
    session the middleware already verified; it checks no permission with it.
    """
    assert not routes.router.dependencies


# -- profile ------------------------------------------------------------------


def client(routes):
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(routes.router, prefix=PREFIX)
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    "profile",
    ["../../etc", "a/b", "..", "", "/etc/passwd", "a\\b"],
)
def test_a_profile_that_is_really_a_path_is_refused(routes, profile):
    response = client(routes).get(f"{PREFIX}/memory/list", params={"profile": profile})

    assert response.status_code == 400, response.text


def test_a_missing_profile_is_refused_rather_than_defaulted(routes):
    """A plugin route is handed no profile; guessing one reads the wrong memory."""
    assert client(routes).get(f"{PREFIX}/memory/list").status_code == 400


def test_an_edit_names_its_profile_too(routes):
    response = client(routes).post(
        f"{PREFIX}/memory/edit",
        json={"profile": "../../etc", "target": "memory", "op": "add", "content": "x"},
    )

    assert response.status_code == 400


# -- the request shape --------------------------------------------------------


def test_only_the_two_real_targets_are_accepted(routes):
    response = client(routes).post(
        f"{PREFIX}/memory/edit",
        json={"profile": "default", "target": "notes", "op": "add", "content": "x"},
    )

    assert response.status_code == 400
    assert "memory" in response.json()["detail"] and "user" in response.json()["detail"]


def test_only_the_three_real_operations_are_accepted(routes):
    response = client(routes).post(
        f"{PREFIX}/memory/edit",
        json={"profile": "default", "target": "memory", "op": "drop", "content": "x"},
    )

    assert response.status_code == 400
    assert "add, replace or remove" in response.json()["detail"]


def test_a_search_without_a_query_is_refused(routes):
    assert client(routes).get(f"{PREFIX}/memory/search", params={"profile": "default"}).status_code == 400


def test_the_mounted_routes_are_the_ones_that_were_asked_for(routes):
    app = FastAPI()
    app.include_router(routes.router, prefix=PREFIX)
    found = {
        (route.path.replace(PREFIX, ""), tuple(sorted(route.methods - {"HEAD"})))
        for route in app.routes
        if isinstance(route, APIRoute)
    }

    assert found == {
        ("/memory/list", ("GET",)),
        ("/memory/search", ("GET",)),
        ("/memory/graph", ("GET",)),
        ("/memory/raw", ("GET",)),
        ("/memory/edit", ("POST",)),
        ("/profiles/{name}", ("PATCH",)),

        ("/context/turn", ("POST",)),
    }


# -- a backend as it is stored ------------------------------------------------


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    """One fake gateway: profiles with real directories, switches, providers.

    The fakes stand in for the four Hermes modules these routes reach — the home
    override, the profile list, the config and the provider discovery — because
    Hermes is not importable here. What they deliberately do NOT fake is the
    filesystem: `raw` reads real files out of a real directory, so a test can
    tell a missing file from an empty one, which is the distinction the route
    exists to report.

    `get_memory_dir` refuses to answer unless a home is bound, the way Hermes'
    own does by resolving per call. A route that read the files outside the
    profile scope would be reading whichever profile the process started under,
    and here it fails instead.
    """
    state = {"home": None, "settings": {"browse": True, "edit": True}, "providers": []}

    def set_override(path):
        state["home"] = str(path)
        return object()

    def reset_override(_token):
        state["home"] = None

    constants = types.ModuleType("hermes_constants")
    constants.set_hermes_home_override = set_override
    constants.reset_hermes_home_override = reset_override

    profiles = types.ModuleType("hermes_cli.profiles")
    profiles.list_profile_names = lambda: ["default", "jurist"]
    profiles.get_profile_dir = lambda name: tmp_path / "homes" / name

    config = types.ModuleType("hermes_cli.config")
    config.load_config = lambda: {
        "plugins": {"entries": {"hermie": {"settings": {"memory": dict(state["settings"])}}}}
    }

    web_memory = types.ModuleType("hermes_cli.web_server_memory")
    web_memory._discover_memory_provider_statuses = lambda: list(state["providers"])

    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.profiles = profiles
    hermes_cli.config = config
    hermes_cli.web_server_memory = web_memory

    def memory_dir():
        assert state["home"], "the memory directory was asked for outside the profile scope"
        return Path(state["home"]) / "memories"

    memory_tool = types.ModuleType("tools.memory_tool")
    memory_tool.get_memory_dir = memory_dir
    tools = types.ModuleType("tools")
    tools.memory_tool = memory_tool

    for name, module in (
        ("hermes_constants", constants),
        ("hermes_cli", hermes_cli),
        ("hermes_cli.profiles", profiles),
        ("hermes_cli.config", config),
        ("hermes_cli.web_server_memory", web_memory),
        ("tools", tools),
        ("tools.memory_tool", memory_tool),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    def write(profile, **files):
        """Write a profile's memory files, as the store would leave them."""
        directory = tmp_path / "homes" / profile / "memories"
        directory.mkdir(parents=True, exist_ok=True)
        for target, content in files.items():
            name = "USER.md" if target == "user" else "MEMORY.md"
            (directory / name).write_text(content, encoding="utf-8")
        return directory

    state["write"] = write
    return state


# A file as the store really leaves it: entries joined by the delimiter, and a
# heading somebody added by hand which the parse throws away.
STORED_MEMORY = "# notes\n\nfirst entry\n§\nsecond entry\n"
STORED_USER = "prefers short answers\n"


def raw(routes, **params):
    return client(routes).get(f"{PREFIX}/memory/raw", params=params)


def test_raw_hands_back_both_files_as_they_are_stored(routes, gateway):
    gateway["write"]("jurist", memory=STORED_MEMORY, user=STORED_USER)

    answer = raw(routes, profile="jurist").json()

    assert answer["profile"] == "jurist"
    builtin = answer["backends"][0]
    assert (builtin["name"], builtin["label"]) == ("builtin", "MEMORY.md and USER.md")
    assert (builtin["available"], builtin["editable"], builtin["note"]) == (True, True, None)
    assert builtin["documents"] == [
        {
            "id": "memory",
            "label": "MEMORY.md",
            "content": STORED_MEMORY,
            "chars": len(STORED_MEMORY),
            "truncated": False,
        },
        {
            "id": "user",
            "label": "USER.md",
            "content": STORED_USER,
            "chars": len(STORED_USER),
            "truncated": False,
        },
    ]


def test_the_heading_and_the_delimiter_a_listing_hides_are_in_the_answer(routes, gateway):
    """The whole reason for the route: a parse cannot give either back."""
    gateway["write"]("jurist", memory=STORED_MEMORY)

    content = raw(routes, profile="jurist").json()["backends"][0]["documents"][0]["content"]

    assert "# notes" in content and "\n§\n" in content


def test_a_file_that_is_not_there_is_left_out_rather_than_sent_empty(routes, gateway):
    gateway["write"]("jurist", memory=STORED_MEMORY)

    documents = raw(routes, profile="jurist").json()["backends"][0]["documents"]

    assert [document["id"] for document in documents] == ["memory"]


def test_a_file_that_is_there_and_bare_is_sent_as_empty(routes, gateway):
    """The other half of the one above: cleared and never written differ."""
    gateway["write"]("jurist", memory=STORED_MEMORY, user="")

    documents = raw(routes, profile="jurist").json()["backends"][0]["documents"]

    assert [document["id"] for document in documents] == ["memory", "user"]
    assert (documents[1]["content"], documents[1]["chars"]) == ("", 0)


def test_a_profile_with_no_memory_directory_at_all_answers_rather_than_failing(routes, gateway):
    answer = raw(routes, profile="jurist")

    assert answer.status_code == 200
    assert answer.json()["backends"][0]["documents"] == []


def test_a_document_is_capped_and_says_so_with_its_real_length(routes, gateway):
    from hermie_plugin.memory import browse

    oversized = "x" * (browse.RAW_DOCUMENT_CHARS + 500)
    gateway["write"]("jurist", memory=oversized)

    document = raw(routes, profile="jurist").json()["backends"][0]["documents"][0]

    assert len(document["content"]) == browse.RAW_DOCUMENT_CHARS
    assert (document["chars"], document["truncated"]) == (len(oversized), True)


def test_raw_reads_the_profile_it_was_asked_about(routes, gateway):
    """The one mistake that would show somebody another person's memory."""
    gateway["write"]("jurist", memory="the jurist's own note")
    gateway["write"]("default", memory="somebody else's note")

    content = raw(routes, profile="jurist").json()["backends"][0]["documents"][0]["content"]

    assert content == "the jurist's own note"


def test_raw_writes_nothing(routes, gateway):
    directory = gateway["write"]("jurist", memory=STORED_MEMORY, user=STORED_USER)
    before = {path.name: (path.read_text(), path.stat().st_mtime_ns) for path in directory.iterdir()}

    assert raw(routes, profile="jurist").status_code == 200

    after = {path.name: (path.read_text(), path.stat().st_mtime_ns) for path in directory.iterdir()}
    assert after == before


def test_a_provider_that_is_set_up_is_available_and_says_it_cannot_be_listed(routes, gateway):
    gateway["providers"] = [
        {"name": "mem0", "description": "mem0 (cloud)", "available": True, "configured": True}
    ]
    gateway["write"]("jurist", memory=STORED_MEMORY)

    backends = {row["name"]: row for row in raw(routes, profile="jurist").json()["backends"]}

    assert backends["mem0"]["label"] == "mem0 (cloud)"
    assert (backends["mem0"]["available"], backends["mem0"]["documents"]) == (True, [])
    assert "no call that lists" in backends["mem0"]["note"]
    assert backends["mem0"]["editable"] is False


def test_a_provider_that_is_not_configured_is_not_available(routes, gateway):
    """An empty card that claimed to be available would read as the answer."""
    gateway["providers"] = [
        {"name": "mem0", "description": "mem0 (cloud)", "available": True, "configured": False}
    ]

    backends = {row["name"]: row for row in raw(routes, profile="jurist").json()["backends"]}

    assert backends["mem0"]["available"] is False
    assert backends["mem0"]["documents"] == [] and backends["mem0"]["note"]


def test_a_gateway_with_no_provider_at_all_still_answers_for_its_files(routes, gateway):
    gateway["write"]("jurist", memory=STORED_MEMORY)

    assert [row["name"] for row in raw(routes, profile="jurist").json()["backends"]] == ["builtin"]


def test_a_backend_can_be_asked_for_by_name(routes, gateway):
    gateway["providers"] = [{"name": "mem0", "description": "mem0", "available": True, "configured": True}]

    answer = raw(routes, profile="jurist", backend="mem0").json()

    assert [row["name"] for row in answer["backends"]] == ["mem0"]


def test_a_backend_this_gateway_does_not_have_is_refused(routes, gateway):
    """Not an empty list: a mistyped name would read as a gateway with nothing."""
    response = raw(routes, profile="jurist", backend="zep")

    assert response.status_code == 400
    assert "zep" in response.json()["detail"]


def test_raw_refuses_a_profile_that_is_really_a_path(routes, gateway):
    assert raw(routes, profile="../../etc").status_code == 400
    assert raw(routes).status_code == 400


def test_raw_is_refused_with_the_sentence_list_uses_when_browsing_is_off(routes, gateway):
    gateway["settings"]["browse"] = False
    gateway["write"]("jurist", memory=STORED_MEMORY)

    refused = raw(routes, profile="jurist")
    listed = client(routes).get(f"{PREFIX}/memory/list", params={"profile": "jurist"})

    assert refused.status_code == 403 and listed.status_code == 403
    assert refused.json()["detail"] == listed.json()["detail"]


def test_raw_is_read_only_where_editing_is_switched_off(routes, gateway):
    """`editable` is the switch's answer, not a constant, for the day a write exists."""
    gateway["settings"]["edit"] = False
    gateway["write"]("jurist", memory=STORED_MEMORY)

    assert raw(routes, profile="jurist").json()["backends"][0]["editable"] is False
