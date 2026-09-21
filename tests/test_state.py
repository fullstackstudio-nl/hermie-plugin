"""State survives an upgrade, and refuses to destroy a downgrade's file."""

import json

from hermie_plugin import state as state_mod
from hermie_plugin.state import FileStore, State


def test_a_fresh_state_is_empty(tmp_path):
    kept = State(FileStore(tmp_path / "s.json")).load()
    assert kept.data["v"] == state_mod.STATE_VERSION
    assert kept.data["sent"] == {}


def test_dedupe_claims_once(tmp_path):
    kept = State(FileStore(tmp_path / "s.json")).load()
    assert kept.claim("request:abc", now=1000) is True
    assert kept.claim("request:abc", now=1001) is False


def test_a_claim_expires(tmp_path):
    kept = State(FileStore(tmp_path / "s.json")).load()
    kept.claim("request:abc", now=1000)
    assert kept.claim("request:abc", now=1000 + state_mod.DEDUPE_TTL_SECONDS + 1) is True


def test_state_round_trips_through_the_store(tmp_path):
    store = FileStore(tmp_path / "s.json")
    first = State(store).load()
    first.claim("message:1", now=1000)
    assert first.save() is True

    second = State(store).load()
    assert second.claim("message:1", now=1001) is False


def test_an_unknown_future_version_is_not_overwritten(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"hermie": {"v": 99, "sent": {"keep": 1}}}))

    kept = State(FileStore(path)).load()
    assert kept.data["sent"] == {}

    # The file on disk still holds the newer version's data: a downgrade that
    # runs for an hour must not cost the upgrade its history.
    assert json.loads(path.read_text())["hermie"]["sent"] == {"keep": 1}


def test_migration_runs_from_an_older_version(tmp_path, monkeypatch):
    """A registered migration is applied and the version moves."""
    monkeypatch.setattr(state_mod, "STATE_VERSION", 2)
    monkeypatch.setitem(
        state_mod.MIGRATIONS, 1, (2, lambda data: {**data, "sent": {}, "retired": {}, "migrated": True})
    )
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"hermie": {"v": 1, "sent": {"old": 1}}}))

    kept = State(FileStore(path)).load()
    assert kept.data["v"] == 2
    assert kept.data["migrated"] is True


def test_a_retired_registration_revives_when_the_app_rewrites_it(tmp_path):
    kept = State(FileStore(tmp_path / "s.json")).load()
    kept.retire("i1", "DeviceNotRegistered", now=5000)
    assert kept.is_retired("i1", updated_at=4000) is True
    assert kept.is_retired("i1", updated_at=6000) is False
