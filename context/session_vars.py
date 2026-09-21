"""Telling Hermes who is asking, when Hermes could not work it out itself.

Hermes carries the identity of a turn in session variables — `ContextVar`s named
after the old `HERMES_SESSION_*` environment variables, read through
`gateway.session_context.get_session_env`, which falls back to the real
environment only while nothing has bound them. Tools read them: a cron job's
`user_id`, a kanban card's author, a background watcher's owner.

On the paths Hermie uses they are sometimes empty while the plugin *does* know
who is asking, because `pre_llm_call` is handed `sender_id` and the app's own
metadata names the registered person. This module fills in that gap, and only
that gap:

- it writes nothing when `HERMES_SESSION_USER_ID` already holds a value, so it
  becomes a no-op the day Hermes fills them in on this path;
- it writes each variable only when that variable is empty;
- it writes them the way Hermes does, through the same `ContextVar`s, rather
  than through the process environment — a process-wide environment write would outlive
  the turn and reach every other session in the gateway.

**What this cannot do, written down because it is the whole limit of the
feature.** `pre_llm_call` is one of the hooks Hermes runs under
`plugins.hook_callback_timeout` (30 seconds by default), and a bounded callback
runs on a worker thread through `contextvars.copy_context().run(...)`. A
`ContextVar` set inside a copied context is discarded when that context ends, so
with the default timeout the write never reaches the turn. Set
`plugins.hook_callback_timeout: 0` and the callback runs on the caller's own
thread, where the write lands in the context the rest of the turn uses. The
shim is written to be harmless either way: it is one lookup and one set on a
path that already reads the same metadata.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

USER_ID = "HERMES_SESSION_USER_ID"
USER_ID_ALT = "HERMES_SESSION_USER_ID_ALT"
USER_NAME = "HERMES_SESSION_USER_NAME"

NAMES = (USER_ID, USER_ID_ALT, USER_NAME)

# The ids of the turn itself, read (never written) to find this session in the
# gateway's own table. The dashboard keys that table by the runtime id it
# minted, which is the first of these; the other two are the durable ones a
# hook is more likely to carry.
UI_SESSION_ID = "HERMES_UI_SESSION_ID"
SESSION_ID = "HERMES_SESSION_ID"
SESSION_KEY = "HERMES_SESSION_KEY"

SESSION_NAMES = (UI_SESSION_ID, SESSION_ID, SESSION_KEY)


def hermes_session_context() -> Optional[Any]:
    """Hermes' own session-variable module, or ``None`` outside a gateway."""
    try:
        from gateway import session_context  # type: ignore

        return session_context
    except Exception:
        return None


class SessionVars:
    """The three user variables, read and written where Hermes keeps them.

    The setter reaches for `_VAR_MAP`, which is the name-to-`ContextVar` map
    `get_session_env` itself reads. The public `set_session_vars` is not usable
    here: it rebinds *every* session variable in one call, so using it to fill
    in a user id would blank the platform, the chat id and the session key of a
    turn that is already running.
    """

    def __init__(self, module: Any = None):
        self.module = module if module is not None else hermes_session_context()

    def available(self) -> bool:
        return self.module is not None and self.variable(USER_ID) is not None

    def variable(self, name: str) -> Optional[Any]:
        try:
            return getattr(self.module, "_VAR_MAP", {}).get(name)
        except Exception:
            return None

    def read(self, name: str) -> str:
        try:
            return str(self.module.get_session_env(name, "") or "")
        except Exception:
            return ""

    def fill(self, values: Dict[str, str]) -> Dict[str, str]:
        """Set each named variable that is empty. Returns what was written."""
        written: Dict[str, str] = {}
        for name, value in values.items():
            if not value or self.read(name):
                continue
            variable = self.variable(name)
            if variable is None:
                continue
            try:
                variable.set(value)
            except Exception as exc:
                logger.warning("hermie: could not set %s: %s", name, exc)
                continue
            written[name] = value
        return written
