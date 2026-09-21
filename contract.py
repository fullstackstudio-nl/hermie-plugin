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
CAP_PUSH_EXPO = "push.expo"
CAP_PUSH_WEBPUSH = "push.webpush"
CAP_PUSH_PREVIEW = "push.preview"
CAP_PUSH_TURN_DONE = "push.type.turn_done"
CAP_PUSH_TURN_FAILED = "push.type.turn_failed"
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
