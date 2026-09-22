"""Telling Hermes who is asking, when Hermes could not work it out itself.

Hermes carries the identity of a turn in session variables — `ContextVar`s named
after the old `HERMES_SESSION_*` environment variables, read through
`gateway.session_context.get_session_env`, which falls back to the real
environment only while nothing has bound them. Tools read them: a cron job's
`user_id`, a kanban card's author, a background watcher's owner.

On the paths Hermie uses they are sometimes empty while the plugin *does* know
who is asking: `pre_llm_call` may be handed `sender_id`, the gateway's own
session record names the login it admitted (`live_session.py`), and the app's
metadata names the registered person. This module fills in that gap, and only
that gap:

- it writes nothing when `HERMES_SESSION_USER_ID` already names the person
  sending this turn, so it becomes a no-op the day Hermes fills them in on this
  path;
- it writes each variable only when that variable is empty — with one exception,
  which is the whole reason the shim earns its keep on a SHARED chat: a value
  bound at session start names whoever started the session, and on a turn from
  somebody else it is not a value to preserve, it is a value that contradicts
  the person actually typing. Then, and only then, the trio is rewritten for the
  sender this module resolved (`fill(..., replace=True)`);
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

    def fill(self, values: Dict[str, str], *, replace: bool = False) -> Dict[str, str]:
        """Set each named variable that is empty. Returns what was written.

        `replace` is for the one case where a value that is already there is
        WRONG rather than merely present: a session started by one login and
        typed into by another keeps the first login bound for the life of the
        session, so `HERMES_SESSION_USER_NAME` goes on naming somebody who left.
        Then the whole trio is rewritten for the person sending this turn,
        including back to empty — a name belonging to the previous person is not
        a value to keep, it is the contradiction being fixed. The caller decides
        this, and only after establishing that the two ids are different people
        (`render.same_user`), so an id merely spelled with its provider prefix
        is left exactly as the gateway wrote it.

        Either way a variable that already holds what is wanted is not written,
        so the shim stays a no-op on every gateway that fills these in itself.
        """
        written: Dict[str, str] = {}
        for name, value in values.items():
            current = self.read(name)
            if current == value:
                continue
            if not replace and (not value or current):
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
