"""Asking the dashboard who it admitted, because it tells a hook nobody.

A dashboard session is created from an authenticated WebSocket upgrade and the
login is stamped on the session record as `auth_user_id`. It is not, however,
handed to a plugin: `pre_llm_call` gets the agent's `_user_id`, which that route
leaves empty, and a system prompt section gets a mapping with no identity in it
at all. The gateway knows; nothing it passes us says so.

But the plugin runs *inside* `hermes serve`. The table of live sessions is a
module global in the same interpreter, and the record for this turn is one
lookup away. So when nothing else names a sender, this module asks it.

Three rules keep that from being a liberty:

- **It never imports the gateway.** It reads `sys.modules`, so a process where
  the dashboard server is not already loaded — a messaging gateway, the CLI, a
  test — answers "nobody" rather than importing a server module and executing
  it to ask a question it was never going to answer.
- **It never takes the server's locks.** Reading `_sessions` by key is a plain
  dict lookup, and the fallback goes through `_session_for_key`, the server's
  own helper, which takes the session lock only to snapshot and hands back.
  Nothing here holds anything while it works.
- **It reads and returns.** No record is mutated, nothing is cached, and every
  attribute is checked for rather than assumed, because this is a private
  module belonging to somebody else (see DESIGN.md — it is the honest cost of
  the feature, and the day the gateway names the sender itself, this stops
  being reached at all).

The value it returns carries the provider that issued the login — the server
builds `<provider>:<user id>` — while the app registers the bare id. Matching
the two is `render.same_user`'s job, not this module's.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

# The dashboard's WebSocket server. Looked up, never imported.
GATEWAY_MODULE = "tui_gateway.server"


class LiveSessions:
    """The gateway's own table of live sessions, read from beside it."""

    def __init__(self, module: Any = None):
        # A module passed in is used as-is (the tests do this). Otherwise the
        # running gateway is looked up per call: a plugin can be loaded before
        # the server module finishes importing itself.
        self.module = module

    def server(self) -> Optional[Any]:
        if self.module is not None:
            return self.module
        return sys.modules.get(GATEWAY_MODULE)

    def available(self) -> bool:
        return self.server() is not None

    def record(self, server: Any, session_id: str) -> Optional[Mapping[str, Any]]:
        """The session record this id names, by runtime id and then by key.

        Both are tried because the two id spaces meet here: the table is keyed
        by the runtime id the dashboard minted, while a hook and Hermes' own
        session variables mostly carry the durable session key.
        """
        sessions = getattr(server, "_sessions", None)
        if isinstance(sessions, dict):
            found = sessions.get(session_id)
            if isinstance(found, dict):
                return found
        for_key = getattr(server, "_session_for_key", None)
        if callable(for_key):
            found = for_key(session_id)
            if isinstance(found, dict):
                return found
        return None

    def login(self, *session_ids: str) -> str:
        """The login the first of these sessions was admitted under, or ""."""
        server = self.server()
        if server is None:
            return ""
        read = getattr(server, "_session_auth_user_id", None)
        if not callable(read):
            # A gateway old enough not to stamp the login is not an error; it
            # is a gateway that cannot answer, which is what "" means here.
            return ""
        tried = set()
        try:
            for session_id in session_ids:
                if not session_id or session_id in tried:
                    continue
                tried.add(session_id)
                found = self.record(server, session_id)
                if found is None:
                    continue
                login = str(read(found) or "")
                if login:
                    return login
        except Exception as exc:
            # Somebody else's private module changed shape under us. That costs
            # the sender, not the turn.
            logger.warning("hermie: could not read the live session: %s", exc)
        return ""

    def runtime_record(self, session_id: str) -> Optional[Mapping[str, Any]]:
        """The live record keyed by this RUNTIME id, or None.

        Only the runtime id: this is how the turn-claim route decides whether an
        id is a live dashboard session at all, and a durable key or a session id
        must not pass that test by way of `_session_for_key`. A plain lookup and
        nothing more — it runs on the dashboard's event loop, where taking the
        server's session lock would make one request wait on every turn.
        """
        server = self.server()
        if server is None or not session_id:
            return None
        try:
            sessions = getattr(server, "_sessions", None)
            found = sessions.get(session_id) if isinstance(sessions, dict) else None
            return found if isinstance(found, dict) else None
        except Exception as exc:
            logger.warning("hermie: could not read the live session table: %s", exc)
            return None

    def admitted_as(self, record: Optional[Mapping[str, Any]]) -> str:
        """The login THIS record was admitted under, or "".

        `login` above asks the same question of a session id; this asks it of a
        record already in hand, which is what the turn-claim route has after it
        has checked that the id is live. The route needs both answers about one
        record and must not race a second lookup between them.

        An empty answer is a gateway that does not stamp the login, or a record
        admitted without one. It is never an authorisation: see the route.
        """
        server = self.server()
        read = getattr(server, "_session_auth_user_id", None) if server is not None else None
        if not callable(read) or not isinstance(record, Mapping):
            return ""
        try:
            return str(read(record) or "")
        except Exception as exc:
            logger.warning("hermie: could not read the live session's login: %s", exc)
            return ""

    @staticmethod
    def durable_ids(record: Optional[Mapping[str, Any]]) -> Tuple[str, ...]:
        """The durable key and agent session id carried by a live record."""
        if not isinstance(record, Mapping):
            return ()
        try:
            ids = (
                str(record.get("session_key") or ""),
                str(getattr(record.get("agent"), "session_id", "") or ""),
            )
        except Exception as exc:
            logger.warning("hermie: could not read the live session's ids: %s", exc)
            return ()
        return tuple(item for item in ids if item)


# Hermes' registry of dashboard sign-in providers. Looked up, never imported.
AUTH_REGISTRY_MODULE = "hermes_cli.dashboard_auth.registry"


def dashboard_providers(module: Any = None) -> Tuple[str, ...]:
    """The names of the dashboard's interactive sign-in providers, or ().

    These are the prefixes a dashboard login is spelled with. Anything else a
    hook might be handed as a sender — a messaging platform's user id, a bot's
    name — is not a dashboard login, and a turn claim must not stand in for it.
    """
    registry = module if module is not None else sys.modules.get(AUTH_REGISTRY_MODULE)
    lister = getattr(registry, "list_session_providers", None)
    if not callable(lister):
        return ()
    try:
        names = (str(getattr(provider, "name", "") or "").strip() for provider in lister())
        return tuple(name for name in names if name)
    except Exception as exc:
        logger.warning("hermie: could not list the dashboard's sign-in providers: %s", exc)
        return ()
