"""Knowing which build is installed, and whether a newer one exists.

`hermes plugins update hermie` git-pulls the installed tree, so the update path
was always one command. What the app could not do was say the command is worth
running.

Two halves are tested here and they are deliberately unequal. Reading the
installed commit is local, free and always on. Asking the repository what the
newest release is reaches the network, so it is off unless the operator asked
— and when it is off the app is not left blind, because the advert still
carries the version and the commit and a phone can compare those itself.
"""

import json

import pytest

import hermie_plugin
from hermie_plugin import contract, uimeta, update
from hermie_plugin.state import MIGRATIONS, STATE_VERSION, State, migrate

from test_plugin import FakeCtx, app_meta_with, gateway  # noqa: F401


class FakeStore:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.writes = 0

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.writes += 1
        self.data[key] = json.loads(json.dumps(value))


# -- what is installed -------------------------------------------------------


def git_tree(root, *, head="ref: refs/heads/main", ref=None, packed=None):
    git = root / ".git"
    git.mkdir(parents=True, exist_ok=True)
    (git / "HEAD").write_text(head)
    if ref is not None:
        path = git / "refs" / "heads" / "main"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(ref + "\n")
    if packed is not None:
        (git / "packed-refs").write_text(packed)
    return root


def test_the_installed_commit_is_read_off_a_loose_ref(tmp_path):
    sha = "a" * 40
    assert update.installed_ref(git_tree(tmp_path, ref=sha)) == sha


def test_the_installed_commit_is_read_off_a_packed_ref(tmp_path):
    """A fresh clone packs its refs, which is exactly what an install is."""
    sha = "b" * 40
    root = git_tree(tmp_path, packed=f"# pack-refs with: peeled\n{sha} refs/heads/main\n")

    assert update.installed_ref(root) == sha


def test_a_pinned_install_is_read_off_a_detached_head(tmp_path):
    """`hermes plugins install --ref <sha>` takes a 40-character SHA only."""
    sha = "c" * 40
    assert update.installed_ref(git_tree(tmp_path, head=sha)) == sha


def test_a_tree_that_is_not_a_checkout_answers_nothing(tmp_path):
    assert update.installed_ref(tmp_path) == ""
    assert update.installed_ref(git_tree(tmp_path)) == ""


def test_a_head_that_is_not_a_sha_is_not_taken_for_one(tmp_path):
    assert update.installed_ref(git_tree(tmp_path, head="not a commit")) == ""


# -- what is newest ----------------------------------------------------------


def state_with(entry=None):
    state = State(FakeStore()).load()
    if entry is not None:
        state.data[update.STATE_KEY] = entry
    return state


def test_the_answer_is_cached_for_an_hour():
    state = state_with()
    calls = []

    first = update.check(state, now=1000, fetch=lambda: calls.append(1) or "v0.9.0")
    again = update.check(state, now=1000 + update.CACHE_SECONDS - 1, fetch=lambda: calls.append(1) or "v9")

    assert (first["latest"], again["latest"]) == ("v0.9.0", "v0.9.0")
    assert len(calls) == 1


def test_the_cache_lapses():
    state = state_with()
    update.check(state, now=1000, fetch=lambda: "v0.9.0")

    later = update.check(state, now=1000 + update.CACHE_SECONDS, fetch=lambda: "v1.0.0")

    assert later["latest"] == "v1.0.0"


def test_a_failed_ask_is_cached_too():
    """A gateway with no outbound route must not try on every single load."""
    state = state_with()
    calls = []

    update.check(state, now=1000, fetch=lambda: calls.append(1) or "")
    update.check(state, now=1030, fetch=lambda: calls.append(1) or "")

    assert len(calls) == 1


def test_a_cache_entry_from_the_future_is_not_trusted():
    """A clock that jumped backwards must not pin the answer forever."""
    state = state_with({"latest": "v0.1.0", "at": 9999})

    assert update.check(state, now=1000, fetch=lambda: "v1.0.0")["latest"] == "v1.0.0"


def test_a_junk_cache_entry_is_ignored_rather_than_believed():
    for junk in (None, 7, "v1", {}, {"latest": "v1"}, {"latest": "v1", "at": "soon"}):
        assert update.cached(state_with(junk), now=1000) is None


def test_a_transport_failure_is_the_same_answer_as_no_newer_release(monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError("no route to host")

    monkeypatch.setattr(update.urllib.request, "urlopen", boom)
    assert update.fetch_latest() == ""


def test_the_url_is_this_plugin_s_own_repository():
    """A plugin that can be pointed anywhere to ask about itself is a hazard."""
    assert update.LATEST_URL.startswith("https://api.github.com/repos/")
    assert contract.REPO in update.LATEST_URL


# -- the advert --------------------------------------------------------------


def test_the_advert_says_what_is_installed_without_reaching_anywhere(tmp_path, monkeypatch):
    home, ctx = gateway(tmp_path, app_meta=app_meta_with())
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)
    monkeypatch.setattr(hermie_plugin.update, "installed_ref", lambda _root: "d" * 40)

    def refuse():
        raise AssertionError("the gateway asked the network without being told to")

    monkeypatch.setattr(hermie_plugin.update, "fetch_latest", refuse)
    hermie_plugin.register(ctx)

    advert = uimeta.read_key(uimeta.PLUGIN_KEY, home)
    assert advert["source"] == {"repo": contract.REPO, "ref": "d" * 40}
    assert advert["version"] == contract.PLUGIN_VERSION
    assert advert["minAppVersion"] == contract.MIN_APP_VERSION
    assert advert["maxContract"] == contract.MAX_CONTRACT
    assert contract.CAP_UPDATE_CHECK not in contract.read_capabilities(advert)


def test_the_newest_release_rides_the_advert_once_it_is_switched_on(tmp_path, monkeypatch):
    home, ctx = gateway(tmp_path, app_meta=app_meta_with(), settings={"update.check": True})
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)
    monkeypatch.setattr(hermie_plugin.update, "installed_ref", lambda _root: "e" * 40)
    monkeypatch.setattr(hermie_plugin.update, "fetch_latest", lambda: "v9.9.9")

    hermie_plugin.register(ctx)

    advert = uimeta.read_key(uimeta.PLUGIN_KEY, home)
    assert advert["source"]["latest"] == "v9.9.9"
    assert contract.CAP_UPDATE_CHECK in contract.read_capabilities(advert)


def test_a_check_that_answered_nothing_claims_no_capability(tmp_path, monkeypatch):
    """A capability names what is there, never what was attempted."""
    home, ctx = gateway(tmp_path, app_meta=app_meta_with(), settings={"update.check": True})
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)
    monkeypatch.setattr(hermie_plugin.update, "fetch_latest", lambda: "")

    hermie_plugin.register(ctx)

    advert = uimeta.read_key(uimeta.PLUGIN_KEY, home)
    assert "latest" not in advert["source"]
    assert contract.CAP_UPDATE_CHECK not in contract.read_capabilities(advert)


def test_an_advert_reader_still_only_reads_strings():
    """The new fields must not change what an older app is able to parse."""
    advert = contract.advert(modules={}, capabilities=["push.expo"], installed_ref="f" * 40)

    assert contract.read_capabilities(advert) == ["push.expo"]
    assert advert["v"] == contract.CONTRACT_VERSION


# -- the upgrade must not cost state -----------------------------------------


def test_a_version_one_file_keeps_everything_it_held():
    """A lost `retired` entry talks to a dead device; a lost `sent` one buzzes twice."""
    old = {
        "v": 1,
        "sent": {"message:abc": 1789957143},
        "retired": {"i1": {"reason": "DeviceNotRegistered", "at": 1789957000}},
    }

    migrated = migrate(dict(old))

    assert migrated["v"] == STATE_VERSION
    assert migrated["sent"] == old["sent"]
    assert migrated["retired"] == old["retired"]
    assert migrated["update"] == {}


def test_the_migration_survives_a_round_trip_through_the_store():
    store = FakeStore({"hermie": {"v": 1, "sent": {"a": 1}, "retired": {"i1": {"reason": "x", "at": 2}}}})

    loaded = State(store).load()
    loaded.save()

    assert store.data["hermie"]["v"] == STATE_VERSION
    assert store.data["hermie"]["sent"] == {"a": 1}
    assert store.data["hermie"]["retired"]["i1"]["reason"] == "x"


def test_a_retirement_still_holds_across_the_upgrade():
    """The behaviour the migration exists to protect, not just the key's shape."""
    state = State(FakeStore({"hermie": {"v": 1, "sent": {}, "retired": {"i1": {"reason": "gone", "at": 500}}}})).load()

    assert state.is_retired("i1", updated_at=400)
    assert not state.is_retired("i1", updated_at=600)


def test_a_dedupe_claim_still_holds_across_the_upgrade():
    state = State(FakeStore({"hermie": {"v": 1, "sent": {"message:abc": 1000}, "retired": {}}})).load()

    assert not state.claim("message:abc", now=1001)


def test_every_version_up_to_this_one_has_a_way_forward():
    """A gap in the table is a state file that silently starts over."""
    for version in range(1, STATE_VERSION):
        assert version in MIGRATIONS, f"no migration from state version {version}"
        assert MIGRATIONS[version][0] == version + 1


def test_a_file_from_the_future_is_left_alone(tmp_path):
    """Overwriting a newer file costs a downgrade its data; this costs one buzz."""
    store = FakeStore({"hermie": {"v": STATE_VERSION + 1, "sent": {"a": 1}, "something-new": True}})

    State(store).load()

    assert store.data["hermie"]["something-new"] is True
    assert store.writes == 0


@pytest.mark.parametrize("junk", [None, 7, "v2", [], {"v": 0}, {"v": True}, {"no": "version"}])
def test_an_unreadable_file_starts_from_empty_rather_than_guessing(junk):
    started = migrate(junk)

    assert started["v"] == STATE_VERSION
    assert (started["sent"], started["retired"], started["update"]) == ({}, {}, {})
