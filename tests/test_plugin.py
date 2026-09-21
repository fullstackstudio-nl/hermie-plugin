"""The plugin loaded the way a gateway loads it, against a fake event source.

`FakeCtx` is the whole Hermes surface this plugin uses. Keeping it small is the
point: if this class has to grow, the plugin reached for something new, and that
is worth noticing in review rather than discovering on somebody's gateway.
"""

import json
from pathlib import Path

import yaml

import hermie_plugin
from hermie_plugin import contract, uimeta
from hermie_plugin.push import events
from hermie_plugin.state import FileStore


class FakeState:
    """Stands in for `ctx.state`, which is a get/set JSON store."""

    def __init__(self, directory: Path):
        self.data_dir = directory
        self._store = FileStore(directory / "state.json")

    def get(self, key, default=None):
        return self._store.get(key, default)

    def set(self, key, value):
        self._store.set(key, value)


class FakeCtx:
    def __init__(self, home: Path, settings=None, profile="jurist"):
        self.hooks = {}
        self.sections = {}
        self.unloads = []
        self.profile_name = profile
        self.state = FakeState(home / "plugin-data")
        self._settings = settings or {}

    def get_config(self, key, default=None):
        return self._settings.get(key, default)

    def register_hook(self, name, callback):
        self.hooks.setdefault(name, []).append(callback)

    def register_system_prompt_section(self, section_id, content, *, position="after_memory", max_chars=4000):
        assert position == "after_memory", "core accepts no other position today"
        assert max_chars <= 4000, "core caps a section at 4000 characters"
        self.sections[section_id] = content

    def on_unload(self, callback):
        self.unloads.append(callback)

    def fire(self, name, **kwargs):
        return [callback(**kwargs) for callback in self.hooks.get(name, [])]


def gateway(tmp_path, *, app_meta=None, settings=None, profile="jurist"):
    """A HERMES_HOME with a profile.yaml, the way an install looks."""
    home = tmp_path / "hermes"
    home.mkdir(parents=True, exist_ok=True)
    (home / "profile.yaml").write_text(
        yaml.safe_dump({"ui_meta": {"hermie-app": app_meta or {}}, "_ui_meta_revisions": {"hermie-app": 7}})
    )
    return home, FakeCtx(home, settings=settings, profile=profile)


def app_meta_with(*, registrations=None, seen=None, context_users=None):
    meta = {"v": 1, "push": {"registrations": registrations or {}, "seen": seen or {}}}
    if context_users is not None:
        meta["context"] = {"v": 1, "users": context_users}
    return meta


def expo_registration(**overrides):
    entry = {
        "v": 1,
        "transport": "expo",
        "token": "ExponentPushToken[abcdefghijklmnopqrstuv]",
        "platform": "ios",
        "types": {name: True for name in events.TYPES},
        "preview": False,
        "updatedAt": 1789957143,
    }
    entry.update(overrides)
    return entry


# -- loading and advertising -------------------------------------------------


def test_loading_registers_the_hooks_a_gateway_will_fire(tmp_path, monkeypatch):
    home, ctx = gateway(tmp_path, app_meta=app_meta_with())
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    hermie_plugin.register(ctx)

    for name in ("post_llm_call", "on_session_end", "pre_approval_request", "pre_tool_call", "pre_llm_call"):
        assert name in ctx.hooks, f"{name} was never registered"
    assert "hermie.device" in ctx.sections


def test_loading_publishes_an_advert_the_app_can_read(tmp_path, monkeypatch):
    home, ctx = gateway(tmp_path, app_meta=app_meta_with())
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    hermie_plugin.register(ctx)

    advert = uimeta.read_key(uimeta.PLUGIN_KEY, home)
    caps = contract.read_capabilities(advert)
    assert contract.CAP_PUSH_EXPO in caps
    assert contract.CAP_CONTEXT_PROMPT in caps
    assert advert["version"] == contract.PLUGIN_VERSION
    assert advert["modules"]["push"] == "on"
    assert advert["modules"]["presence"] == "planned"


def test_a_switched_off_module_claims_nothing(tmp_path, monkeypatch):
    home, ctx = gateway(tmp_path, app_meta=app_meta_with(), settings={"modules.push": False})
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    hermie_plugin.register(ctx)

    advert = uimeta.read_key(uimeta.PLUGIN_KEY, home)
    assert advert["modules"]["push"] == "off"
    assert contract.CAP_PUSH_EXPO not in contract.read_capabilities(advert)
    assert "post_llm_call" not in ctx.hooks


def test_the_advert_never_touches_the_app_key(tmp_path, monkeypatch):
    """`hermie-app` is the app's, and it holds a compare-and-swap revision for it."""
    home, ctx = gateway(tmp_path, app_meta=app_meta_with(registrations={"i1": expo_registration()}))
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    hermie_plugin.register(ctx)

    document = yaml.safe_load((home / "profile.yaml").read_text())
    assert document["ui_meta"]["hermie-app"]["push"]["registrations"]["i1"]["v"] == 1
    assert document["_ui_meta_revisions"]["hermie-app"] == 7  # untouched
    assert document["_ui_meta_revisions"][uimeta.PLUGIN_KEY] == 1


def test_unloading_takes_the_advert_down(tmp_path, monkeypatch):
    home, ctx = gateway(tmp_path, app_meta=app_meta_with())
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    hermie_plugin.register(ctx)
    for callback in ctx.unloads:
        callback()

    assert uimeta.read_key(uimeta.PLUGIN_KEY, home) is None


def test_writing_the_app_key_is_refused_outright():
    try:
        uimeta.write_key(uimeta.APP_KEY, {"anything": True})
    except ValueError as exc:
        assert "belongs to the app" in str(exc)
    else:
        raise AssertionError("writing hermie-app should be refused")


# -- push, end to end --------------------------------------------------------


def test_an_approval_becomes_a_notification(tmp_path, monkeypatch):
    home, ctx = gateway(
        tmp_path, app_meta=app_meta_with(registrations={"i1": expo_registration()})
    )
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    import hermie_plugin.push as push_pkg

    captured = []
    monkeypatch.setattr(push_pkg.expo, "send", lambda batch: captured.append(batch) or [
        push_pkg.expo.Ticket(token=message["to"], status="ok", receipt_id="r") for message in batch
    ])

    runtime = hermie_plugin.Runtime(ctx, home=home)
    module = push_pkg.PushModule(runtime)

    # Take the decision the hook would take, then deliver it on this thread:
    # the worker is what the queue test covers, and racing it here would only
    # make this assertion flaky.
    built = []
    monkeypatch.setattr(module, "offer", lambda note, delay=False: built.append(note))
    module.on_pre_approval_request(
        surface="gateway", session_key="s1", description="delete the build directory",
        request_id="req-1", turn_id="t1", command="remove-build",
    )
    assert len(built) == 1
    assert module.deliver(built[0]) == 1

    assert captured, "nothing reached the transport"
    message = captured[0][0]
    assert message["data"]["type"] == "request"
    assert message["data"]["bot"] == "jurist"
    assert message["title"] == "jurist"
    # The default payload says who and what kind, never what was said.
    assert "preview" not in message["data"]
    assert "remove-build" not in json.dumps(message)


def test_the_same_approval_twice_buzzes_once(tmp_path, monkeypatch):
    home, ctx = gateway(tmp_path, app_meta=app_meta_with(registrations={"i1": expo_registration()}))
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    import hermie_plugin.push as push_pkg

    monkeypatch.setattr(
        push_pkg.expo, "send",
        lambda batch: [push_pkg.expo.Ticket(token=m["to"], status="ok", receipt_id="r") for m in batch],
    )

    module = push_pkg.PushModule(hermie_plugin.Runtime(ctx, home=home))
    note = events.from_approval(
        bot="jurist", session_key="s1", description="d", request_id="req-1", turn_id="t1", at=10
    )
    assert module.deliver(note) == 1
    assert module.deliver(note) == 0


def test_a_dead_device_is_retired_and_then_skipped(tmp_path, monkeypatch):
    home, ctx = gateway(tmp_path, app_meta=app_meta_with(registrations={"i1": expo_registration()}))
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    import hermie_plugin.push as push_pkg

    monkeypatch.setattr(
        push_pkg.expo, "send",
        lambda batch: [
            push_pkg.expo.Ticket(token=m["to"], status="error", error="DeviceNotRegistered") for m in batch
        ],
    )

    module = push_pkg.PushModule(hermie_plugin.Runtime(ctx, home=home))
    first = events.from_approval(bot="b", session_key="s", description="d", request_id="r1", turn_id="t", at=10)
    assert module.deliver(first) == 0

    second = events.from_approval(bot="b", session_key="s", description="d", request_id="r2", turn_id="t", at=11)
    assert module.deliver(second) == 0

    # The plugin records the retirement in its own state; it never edits the
    # app's registration, which the app owns.
    assert module.runtime.state.is_retired("i1", updated_at=1789957143) is True
    document = yaml.safe_load((home / "profile.yaml").read_text())
    assert "i1" in document["ui_meta"]["hermie-app"]["push"]["registrations"]


def test_an_unregistered_gateway_sends_nothing(tmp_path, monkeypatch):
    home, ctx = gateway(tmp_path, app_meta=app_meta_with())
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    import hermie_plugin.push as push_pkg

    def explode(batch):
        raise AssertionError("nothing should be sent")

    monkeypatch.setattr(push_pkg.expo, "send", explode)
    module = push_pkg.PushModule(hermie_plugin.Runtime(ctx, home=home))
    note = events.from_approval(bot="b", session_key="s", description="d", request_id="r", turn_id="t", at=10)
    assert module.deliver(note) == 0


# -- context, end to end -----------------------------------------------------


def test_the_frozen_section_renders_the_only_registered_person(tmp_path, monkeypatch):
    home, ctx = gateway(
        tmp_path,
        app_meta=app_meta_with(
            context_users={"u1": {"displayName": "Sebas", "timezone": "Europe/Amsterdam"}}
        ),
    )
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)
    hermie_plugin.register(ctx)

    rendered = ctx.sections["hermie.device"](
        {"session_id": "s1", "profile_name": "jurist", "model": "m", "provider": "p", "platform": "", "cwd": ""}
    )
    assert "Sebas" in rendered
    assert "Europe/Amsterdam" in rendered


def test_the_per_turn_path_stays_silent_for_the_person_already_in_the_prompt(tmp_path, monkeypatch):
    """The common case must cost nothing per turn."""
    home, ctx = gateway(
        tmp_path, app_meta=app_meta_with(context_users={"u1": {"displayName": "Sebas"}})
    )
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)
    hermie_plugin.register(ctx)

    ctx.sections["hermie.device"]({"session_id": "s1", "profile_name": "jurist"})
    assert ctx.fire("pre_llm_call", session_id="s1", sender_id="u1") == [None]


def test_an_ungated_gateway_names_nobody_and_adds_nothing(tmp_path, monkeypatch):
    home, ctx = gateway(
        tmp_path, app_meta=app_meta_with(context_users={"u1": {"displayName": "Sebas"}})
    )
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)
    hermie_plugin.register(ctx)

    assert ctx.fire("pre_llm_call", session_id="s1", sender_id="") == [None]


def test_a_second_person_on_the_same_session_gets_their_own_context(tmp_path, monkeypatch):
    home, ctx = gateway(
        tmp_path,
        app_meta=app_meta_with(
            context_users={"u1": {"displayName": "Sebas"}, "u2": {"displayName": "Ana"}}
        ),
        settings={"context.default_user": "u1"},
    )
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)
    hermie_plugin.register(ctx)

    ctx.sections["hermie.device"]({"session_id": "s1", "profile_name": "jurist"})
    result = ctx.fire("pre_llm_call", session_id="s1", sender_id="u2")[0]
    assert result is not None and "Ana" in result["context"]


def test_a_smart_approval_asks_nobody_and_so_tells_nobody(tmp_path, monkeypatch):
    """The smart path answers itself; there is no question on a lock screen."""
    home, ctx = gateway(tmp_path, app_meta=app_meta_with(registrations={"i1": expo_registration()}))
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    import hermie_plugin.push as push_pkg

    module = push_pkg.PushModule(hermie_plugin.Runtime(ctx, home=home))
    built = []
    monkeypatch.setattr(module, "offer", lambda note, delay=False: built.append(note))
    module.on_pre_approval_request(surface="smart", session_key="s1", description="d", request_id="r")
    assert built == []


def test_the_worker_delivers_what_was_offered(tmp_path, monkeypatch):
    """The one test that does exercise the thread, since everything rides it."""
    import time

    home, ctx = gateway(tmp_path, app_meta=app_meta_with(registrations={"i1": expo_registration()}))
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    import hermie_plugin.push as push_pkg

    seen = []
    monkeypatch.setattr(
        push_pkg.expo, "send",
        lambda batch: seen.append(batch) or [
            push_pkg.expo.Ticket(token=m["to"], status="ok", receipt_id="r") for m in batch
        ],
    )

    module = push_pkg.PushModule(hermie_plugin.Runtime(ctx, home=home))
    module.offer(
        events.from_approval(bot="b", session_key="s", description="d", request_id="r", turn_id="t", at=10)
    )

    deadline = time.time() + 5
    while not seen and time.time() < deadline:
        time.sleep(0.01)
    module.stop()
    assert seen, "the worker never delivered"


def test_a_full_queue_drops_rather_than_slowing_the_turn(tmp_path, monkeypatch):
    """Every hook this plugin registers sits on the agent's own path."""
    home, ctx = gateway(tmp_path, app_meta=app_meta_with())
    monkeypatch.setattr(uimeta, "hermes_home", lambda: home)

    import hermie_plugin.push as push_pkg

    module = push_pkg.PushModule(hermie_plugin.Runtime(ctx, home=home))
    monkeypatch.setattr(module, "_ensure_worker", lambda: None)
    note = events.from_approval(bot="b", session_key="s", description="d", request_id="r", turn_id="t", at=10)
    for _ in range(push_pkg.QUEUE_SIZE + 10):
        module.offer(note)  # must never raise
    assert module.queue.full()
