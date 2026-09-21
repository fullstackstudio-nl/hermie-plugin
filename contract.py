"""What the app is allowed to assume about the plugin it found.

People do not update plugins. A gateway that was set up once and works is a
gateway nobody logs into again, so every version of the app will meet plugins
older than itself for as long as the product exists. The way out is not to
promise that the newest plugin is installed; it is to make the app ask.

The plugin therefore publishes a small, versioned advert into the gateway's own
``ui_meta`` under the ``hermie-plugin`` key, and the app reads it the same way
it reads everything else — through ``profiles.list``, over the connection it
already has. There is no endpoint to dial: Hermes has no extension point for
adding an HTTP route to the gateway (see DESIGN.md), and an advert in ui_meta
needs none.

Two rules keep this honest in both directions:

- **A capability is a string, not a version number.** The app tests for
  ``"push.webpush"``; it does not test for ``version >= "0.4.0"`` and hope. A
  plugin that gains a capability adds a string, and every older app simply does
  not ask for it. `version` is carried for the human-readable "update available"
  line only.
- **An absent advert means an absent plugin.** An old plugin that never wrote
  the key, a plugin that is installed but disabled, and no plugin at all are
  indistinguishable to the app, and all three mean the same thing: do not offer
  the feature.

The other half of the contract is what the **app** writes, under its own key —
one per person, ``hermie-app:<user id>``, with the older shared ``hermie-app``
read for one more version. The readers are ``push/registrations.py`` and
``context/render.py``; this is the shape they agree on::

    hermie-app:<user id>:
      mutes:                        # bot -> until (unix seconds, 0 = forever)
        jurist: 0
        marketing: 1790000000
      push:
        registrations:
          <installation id>:
            v: 1
            transport: expo         # or "webpush"
            token: "..."            # expo only
            endpoint: "..."         # webpush only, with keys.p256dh + keys.auth
            platform: ios
            types: {message: true, request: true, cron: true,
                    turn_done: false, turn_failed: false}
            preview: false
            updatedAt: 1789957143
        seen:                       # the heartbeat, per device
          <installation id>: {bot: jurist, at: 1789957143}
      context:
        v: 1
        default: <user id>
        users:
          <user id>: {displayName: "...", userIdAlt: "...", about: "...",
                      device: {model: "...", os: "...", appVersion: "..."},
                      timezone: "Europe/Amsterdam", locale: "nl-NL",
                      perBot: {<bot>: "..."}, updatedAt: 1789957143}

Two of those are new and both are read tolerantly for one version: a ``seen``
value that is a bare number is the older "looking at some chat" shape, and the
whole bag may still be the shared ``hermie-app``. ``mutes`` is also read from
``push.mutes`` for an app that files it with the push settings.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List

# The plugin's own release version. Also in plugin.yaml; a test keeps them equal.
PLUGIN_VERSION = "0.1.0"

# The shape of the `hermie-plugin` ui_meta key. Bumped only when an existing
# field changes meaning — adding a field does not bump it, because a reader that
# does not know a field ignores it.
CONTRACT_VERSION = 1

# Every capability this build can offer. A module contributes its own subset
# once it is enabled AND its prerequisites are actually present, so the advert
# describes what this gateway can do rather than what the code could do
# somewhere else. That distinction is the whole point: a Web Push capability on
# a gateway with no signing library is a promise the app would act on.
#
# Two of these say which *shape* this gateway understands, and they are here
# because getting them wrong is silent. An app that moves its bag to
# `hermie-app:<user id>` in front of a plugin that only reads `hermie-app`
# notifies nobody, and an app that writes a `{bot, at}` heartbeat to a plugin
# that only reads a number suppresses nothing. Both are questions the app must
# be able to ask before it writes, which is what a capability string is for.
CAP_PUSH_EXPO = "push.expo"
CAP_PUSH_WEBPUSH = "push.webpush"
CAP_PUSH_PREVIEW = "push.preview"
CAP_PUSH_MUTE = "push.mute"
CAP_PUSH_TURN_DONE = "push.type.turn_done"
CAP_PUSH_TURN_FAILED = "push.type.turn_failed"
CAP_PUSH_SEEN_PER_CHAT = "push.seen.per_chat"
CAP_UIMETA_PER_USER = "ui_meta.per_user"
CAP_CONTEXT_PROMPT = "context.system_prompt"
CAP_CONTEXT_PER_BOT = "context.per_bot"

# Modules that exist as a name and a config key but have no implementation yet.
# They are advertised as "planned" rather than silently missing so the app can
# tell "this plugin is too old" from "this gateway has it switched off", and so
# the config surface does not change shape when they land.
PLANNED_MODULES = ("sessions", "presence", "transcripts", "search", "attachments", "usage")


def advert(
    *,
    modules: Dict[str, str],
    capabilities: Iterable[str],
    limits: Dict[str, Any] | None = None,
    now: float | None = None,
) -> Dict[str, Any]:
    """The value written to the ``hermie-plugin`` ui_meta key."""
    return {
        "v": CONTRACT_VERSION,
        "version": PLUGIN_VERSION,
        "capabilities": sorted(set(capabilities)),
        "modules": dict(sorted(modules.items())),
        "limits": dict(limits or {}),
        "updatedAt": int(now if now is not None else time.time()),
    }


def read_capabilities(value: Any) -> List[str]:
    """The capability list out of an advert, for an app-side reader or a test.

    An advert whose ``v`` is newer than this reader understands yields nothing.
    That is deliberate and it is the same rule the registration reader follows:
    a shape you do not know is not a shape you guess at.
    """
    if not isinstance(value, dict):
        return []
    version = value.get("v")
    if not isinstance(version, int) or isinstance(version, bool) or version > CONTRACT_VERSION:
        return []
    raw = value.get("capabilities")
    return sorted({item for item in raw if isinstance(item, str)}) if isinstance(raw, list) else []
