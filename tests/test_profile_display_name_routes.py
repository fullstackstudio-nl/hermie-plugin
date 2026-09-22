"""`PATCH /api/plugins/hermie/profiles/{name}`: what it writes, refuses, and gates.

Same shape as `test_memory_routes.py`: FastAPI is a Hermes runtime dependency,
not this plugin's, so these skip on a bare checkout. The `gateway` fixture
fakes the same three Hermes seams (`hermes_constants`, `hermes_cli.profiles`,
`hermes_cli.config`) memory's own fixture fakes, plus a `write_profile_meta`
that really writes `profile.yaml` under `tmp_path` — atomically, keeping
whatever mode the file already had — so what a test pins is the answer a
caller gets and what actually landed on disk, the same way `raw`'s tests do
for memory.

**Why some "this is really a path" names come back 404, not 400.** This
route's profile is a path parameter, not a query string the way memory's is.
A name containing `/` never matches the single `{name}` segment at all, and
`..`/`.` are collapsed by ordinary URL dot-segment handling before the request
is even sent — both come back as Starlette's own "no such route" 404, never
reaching this module's validation. That is still a refusal; it just does not
go through `profile_name.ProfileRefused`, so it is pinned separately from the
400s that do.
"""

import importlib.util
import os
import stat
import sys
import types
from pathlib import Path

import pytest
import yaml

pytest.importorskip("fastapi", reason="FastAPI ships with the Hermes runtime, not with this plugin")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

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


def client(routes):
    app = FastAPI()
    app.include_router(routes.router, prefix=PREFIX)
    return TestClient(app, raise_server_exceptions=False)


def patch(routes, name, display_name):
    return client(routes).patch(f"{PREFIX}/profiles/{name}", json={"display_name": display_name})


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    """One fake gateway: real profile directories under `tmp_path`, a switch to
    flip. `write_profile_meta` mirrors Hermes' own contract closely enough to
    pin against: only the passed field changes, the write is temp-file-and-
    rename, and an existing file's permission bits survive it.
    """
    state = {"settings": {"edit": True}}

    def set_override(_path):
        return object()

    def reset_override(_token):
        return None

    constants = types.ModuleType("hermes_constants")
    constants.set_hermes_home_override = set_override
    constants.reset_hermes_home_override = reset_override

    def home_of(name):
        home = tmp_path / "homes" / name
        home.mkdir(parents=True, exist_ok=True)
        return home

    def write_profile_meta(profile_dir, *, description=None, description_auto=None, display_name=None):
        path = Path(profile_dir) / "profile.yaml"
        existing = {}
        mode = None
        if path.exists():
            existing = yaml.safe_load(path.read_text()) or {}
            mode = stat.S_IMODE(path.stat().st_mode)
        if description is not None:
            existing["description"] = description
        if description_auto is not None:
            existing["description_auto"] = description_auto
        if display_name is not None:
            if display_name.strip():
                existing["display_name"] = display_name.strip()
            else:
                existing.pop("display_name", None)
        tmp_file = path.with_name(f".{path.name}.tmp")
        tmp_file.write_text(yaml.safe_dump(existing, sort_keys=False))
        if mode is not None:
            os.chmod(tmp_file, mode)
        tmp_file.replace(path)

    profiles = types.ModuleType("hermes_cli.profiles")
    profiles.list_profile_names = lambda: ["default", "jurist"]
    profiles.get_profile_dir = lambda name: home_of(name)
    profiles.write_profile_meta = write_profile_meta

    config = types.ModuleType("hermes_cli.config")
    config.load_config = lambda: {
        "plugins": {"entries": {"hermie": {"settings": {"profiles": dict(state["settings"])}}}}
    }

    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.profiles = profiles
    hermes_cli.config = config

    for name, module in (
        ("hermes_constants", constants),
        ("hermes_cli", hermes_cli),
        ("hermes_cli.profiles", profiles),
        ("hermes_cli.config", config),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    state["home_of"] = home_of
    return state


def write_yaml(path, data, *, mode=None):
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    if mode is not None:
        os.chmod(path, mode)


# -- the happy path -----------------------------------------------------


def test_the_happy_path_writes_the_key_and_answers_the_contract_shape(routes, gateway):
    home = gateway["home_of"]("jurist")
    write_yaml(home / "profile.yaml", {"description": "a lawyer", "description_auto": False}, mode=0o640)

    response = patch(routes, "jurist", "Jurist")

    assert response.status_code == 200
    assert response.json() == {"name": "jurist", "display_name": "Jurist"}
    document = yaml.safe_load((home / "profile.yaml").read_text())
    assert document == {"description": "a lawyer", "description_auto": False, "display_name": "Jurist"}
    assert stat.S_IMODE((home / "profile.yaml").stat().st_mode) == 0o640


def test_surrounding_whitespace_is_trimmed_before_it_is_written(routes, gateway):
    gateway["home_of"]("jurist")

    response = patch(routes, "jurist", "  Jurist  ")

    assert response.json()["display_name"] == "Jurist"


def test_a_profile_with_no_profile_yaml_yet_gets_one(routes, gateway):
    home = gateway["home_of"]("jurist")

    response = patch(routes, "jurist", "Jurist")

    assert response.status_code == 200
    assert yaml.safe_load((home / "profile.yaml").read_text()) == {"display_name": "Jurist"}


# -- validation: 400 ----------------------------------------------------


@pytest.mark.parametrize("display_name", ["", "   "])
def test_an_empty_display_name_is_refused(routes, gateway, display_name):
    gateway["home_of"]("jurist")

    assert patch(routes, "jurist", display_name).status_code == 400


def test_a_display_name_over_sixty_characters_is_refused(routes, gateway):
    gateway["home_of"]("jurist")

    assert patch(routes, "jurist", "x" * 61).status_code == 400


def test_a_display_name_of_exactly_sixty_characters_is_accepted(routes, gateway):
    gateway["home_of"]("jurist")

    response = patch(routes, "jurist", "x" * 60)

    assert response.status_code == 200


def test_a_control_character_is_refused(routes, gateway):
    gateway["home_of"]("jurist")

    assert patch(routes, "jurist", "Ju\x07rist").status_code == 400


@pytest.mark.parametrize("name", ["~", "a\\b", "c:name", "  jurist"])
def test_a_name_that_is_really_a_path_is_refused_by_this_module(routes, gateway, name):
    """Single-segment bad names: they reach the handler and are refused there."""
    assert patch(routes, name, "Jurist").status_code == 400


@pytest.mark.parametrize("name", ["a/b", "..", "."])
def test_a_multi_segment_or_dot_name_is_not_routed_here_at_all(routes, gateway, name):
    """See the module docstring: refused by routing before this module's own
    validation ever runs — still not a 200, and not this module's 400 either."""
    assert patch(routes, name, "Jurist").status_code == 404


# -- unknown profile: 404 -------------------------------------------------


def test_an_unknown_profile_is_404_not_400(routes, gateway):
    response = patch(routes, "somebody-elses", "Jurist")

    assert response.status_code == 404


def test_an_unknown_profile_writes_nothing(routes, gateway, tmp_path):
    """Membership is checked before a home is even resolved, so nothing for
    this name gets created along the way."""
    patch(routes, "somebody-elses", "Jurist")

    assert not (tmp_path / "homes" / "somebody-elses").exists()


# -- permission: 403 -----------------------------------------------------


def test_editing_switched_off_for_that_profile_is_refused(routes, gateway):
    gateway["home_of"]("jurist")
    gateway["settings"]["edit"] = False

    response = patch(routes, "jurist", "Jurist")

    assert response.status_code == 403


def test_editing_switched_off_writes_nothing(routes, gateway):
    home = gateway["home_of"]("jurist")
    write_yaml(home / "profile.yaml", {"description": "a lawyer"})
    gateway["settings"]["edit"] = False

    patch(routes, "jurist", "Jurist")

    assert yaml.safe_load((home / "profile.yaml").read_text()) == {"description": "a lawyer"}


def test_editing_stays_on_for_a_profile_that_did_not_switch_it_off(routes, gateway):
    """The setting is per profile, not global: only the one being edited counts."""
    gateway["home_of"]("jurist")
    gateway["home_of"]("default")
    gateway["settings"]["edit"] = True

    assert patch(routes, "jurist", "Jurist").status_code == 200


# -- only the one key, and the file's own permissions ----------------------


def test_only_the_display_name_key_changes_everything_else_is_untouched(routes, gateway):
    home = gateway["home_of"]("jurist")
    write_yaml(
        home / "profile.yaml",
        {"description": "a lawyer", "description_auto": True, "some_future_key": [1, 2, 3]},
        mode=0o600,
    )

    response = patch(routes, "jurist", "Jurist")

    assert response.status_code == 200
    document = yaml.safe_load((home / "profile.yaml").read_text())
    assert document["description"] == "a lawyer"
    assert document["description_auto"] is True
    assert document["some_future_key"] == [1, 2, 3]
    assert document["display_name"] == "Jurist"
    assert stat.S_IMODE((home / "profile.yaml").stat().st_mode) == 0o600


def test_the_write_is_a_replace_no_temp_file_left_behind(routes, gateway):
    home = gateway["home_of"]("jurist")
    write_yaml(home / "profile.yaml", {"description": "a lawyer"})

    patch(routes, "jurist", "Jurist")

    leftovers = [item.name for item in home.iterdir() if item.name != "profile.yaml"]
    assert leftovers == []
