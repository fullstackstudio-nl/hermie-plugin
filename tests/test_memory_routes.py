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
"""

import importlib.util
import sys
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
    already checked. It could not identify a person, because no route on this
    gateway is given one.
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


def test_the_four_routes_are_the_four_that_were_asked_for(routes):
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
        ("/memory/edit", ("POST",)),
    }
