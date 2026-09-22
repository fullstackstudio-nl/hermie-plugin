"""Who is sending the next turn of a shared chat, said by the person sending it.

Hermes names the sender of a turn to a hook as the agent's `_user_id`, and on a
dashboard session that is the login stamped on the session record when the
session was CREATED. A second window attaching to the same session changes the
transport slot into a fan-out that names nobody, and the turn thread rebinds that
slot, so nothing reachable during the turn names the person who pressed send.
Every rung this plugin had — the hook, the live record, the session variables —
therefore names whoever opened the chat, on every turn, whoever typed it.

The one moment the gateway does know who is sending is an authenticated HTTP
request: the dashboard's auth middleware verifies the caller and attaches the
session it verified to `request.state.session`, carrying the provider and the
user id the WebSocket ticket was minted from. So the app says, just before it
submits a prompt, "the next turn of this session is mine", over the dashboard,
as itself. That is a *claim*, and this module is where claims live until a turn
spends one.

The rules, each of which a test holds:

- **The identity is the login's, never the body's.** The route builds it from
  the verified session in the same `<provider>:<user id>` spelling the server
  uses for `auth_user_id`, so a claim and a hook sender compare like with like.
  A request that names no person — a gateway without a login, a service token —
  claims nothing.
- **A claim is for one turn.** `take` removes it. The next turn without a claim
  falls back to what Hermes says, which is the opener.
- **A claim is short-lived.** `TTL_SECONDS` after it was made it is ignored and
  dropped, so a claim whose prompt never ran cannot attach itself to somebody
  else's turn an hour later.
- **Last claim wins.** Two people claiming one session inside the window leave
  the later claim standing. The gateway runs one turn at a time per session and
  the app claims immediately before it submits, so this is the order the turns
  will run in far more often than not; DESIGN.md says where it is not.
- **Bounded, in memory, in-process.** At most `MAX_CLAIMS` sessions are held,
  oldest dropped first; nothing is written to disk and nothing leaves the
  process.

**Which id.** The app knows the runtime session id — the `session_id` that
`session.create` and `session.resume` answer and that `prompt.submit` takes —
and that is the id it claims. The route refuses any id that is not a live
runtime session in the dashboard's own table, so a claim is only ever keyed by
one: a session key or a durable id sent in its place is refused, never stored.
That matters because other platforms' sessions have keys too, and a claim keyed
by a messaging chat's key would reach that chat's turns.

During the turn Hermes binds the runtime id into the session variables as
`HERMES_UI_SESSION_ID`, which is the exact match. The hook itself is handed the
agent's durable session id instead, and a session also has a durable key; the
route reads both off the live record it just found and keeps them beside the
claim as aliases. A turn with no runtime id bound is matched through those
recorded aliases and nothing else — never by comparing its ids with the claim
keys. The fallback never overrides an exact answer either: two windows can hold
two runtime sessions on one stored session, and a turn that knows its own
runtime id must not spend a claim made for the other.

**What spends a claim.** A turn does, in `pre_llm_call`. So does `/me`, which is
answered by the plugin without a turn: a claim sent before a command would
otherwise wait for the next turn from somebody else. The app must not claim for
a slash command at all; a claim is for a `prompt.submit` that starts a model
turn.

**One store per process.** Hermes imports this plugin under a name of its own
for its hooks, and the dashboard imports `dashboard/plugin_api.py` separately,
which loads a second copy of the package. Module state would be two stores that
never meet, so the store is kept in `sys.modules` under a fixed name that both
copies find.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import time
import types
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, FrozenSet, Iterable, Optional

logger = logging.getLogger(__name__)

# How long a claim stands. The app claims immediately before `prompt.submit`,
# so the claim is normally spent within a second or two; the rest is room for a
# slow network between the two requests and for the agent being built. Kept
# short because a claim no turn spent — the prompt was steered into a running
# turn, or never sent — can be spent by the next turn from a client that does
# not claim, and 30 seconds is the whole of that exposure.
TTL_SECONDS = 30.0

# How many sessions can hold a claim at once. A claim is spent within seconds in
# the ordinary case, so this is a ceiling for a misbehaving client, not a size
# the gateway is expected to reach.
MAX_CLAIMS = 256

# A runtime id is eight hex characters today and a durable id is a timestamped
# token; both fit this. Anything carrying a separator, whitespace or a control
# character is refused rather than cleaned, because an id is something the
# gateway minted, not text to be repaired.
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

# The process-wide slot both copies of the plugin find.
REGISTRY = "hermie_turn_claims"

# The shape of the store kept in that slot. The slot holds one store per shape,
# so a copy of the plugin that disagrees about the shape — an update loaded
# beside an older copy that has not been unloaded — gets a fresh store rather
# than methods it does not know. ANY change to `TurnClaims` or `Claim` that
# another copy could observe (a method, its arguments, what it returns) means
# bumping this.
#
# The shape number is what is trusted, never the class: every copy of this
# package defines its own `TurnClaims`, so an `isinstance` check against the
# asking copy's class fails for the store the other copy made, and the two
# copies would silently stop sharing.
SHAPE = 1

# What a store must answer to be used at all. A guard against a slot somebody
# else filled, not a check of which copy made it.
STORE_METHODS = ("claim", "peek", "take", "take_if")


@dataclass(frozen=True)
class Claim:
    identity: str
    at: float
    aliases: FrozenSet[str]


class TurnClaims:
    """Claims by runtime session id, each for one turn and for a short while."""

    def __init__(
        self,
        *,
        ttl: float = TTL_SECONDS,
        cap: int = MAX_CLAIMS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.ttl = ttl
        self.cap = cap
        self.clock = clock
        self.lock = threading.Lock()
        # Insertion order is claim order: a re-claim is moved to the back, so
        # the front is always the oldest, which is what both expiry and the cap
        # drop first.
        self.claims: "OrderedDict[str, Claim]" = OrderedDict()

    def __len__(self) -> int:
        with self.lock:
            self._expire()
            return len(self.claims)

    def claim(self, session_id: str, identity: str, aliases: Iterable[str] = ()) -> None:
        """Say that the next turn of *session_id* is *identity*'s."""
        if not session_id or not identity:
            return
        with self.lock:
            self._expire()
            self.claims.pop(session_id, None)
            self.claims[session_id] = Claim(
                identity=identity,
                at=self.clock(),
                aliases=frozenset(alias for alias in aliases if alias and alias != session_id),
            )
            while len(self.claims) > self.cap:
                self.claims.popitem(last=False)

    def take(self, session_id: str, aliases: Iterable[str] = ()) -> str:
        """The claimed identity for this turn, spent; or "" when there is none."""
        return self._find(session_id, aliases, spend=True)

    def peek(self, session_id: str, aliases: Iterable[str] = ()) -> str:
        """The claimed identity for this turn, left in place."""
        return self._find(session_id, aliases, spend=False)

    def take_if(
        self, session_id: str, aliases: Iterable[str], accept: Callable[[str], bool]
    ) -> str:
        """Spend the claim for this turn only if *accept* says it applies.

        The test and the spend happen under one hold of the lock, so the claim
        that is judged is the claim that is spent: a newer claim landing in
        between cannot be spent on the strength of the older one's test. A
        claim *accept* refuses is left where it is. *accept* must not call back
        into the store.
        """
        with self.lock:
            self._expire()
            key = self._key(session_id, [alias for alias in aliases if alias])
            if key is None or not accept(self.claims[key].identity):
                return ""
            return self.claims.pop(key).identity

    def _find(self, session_id: str, aliases: Iterable[str], *, spend: bool) -> str:
        with self.lock:
            self._expire()
            key = self._key(session_id, [alias for alias in aliases if alias])
            if key is None:
                return ""
            found = self.claims.pop(key) if spend else self.claims[key]
            return found.identity

    def _key(self, session_id: str, aliases: list) -> Optional[str]:
        if session_id:
            # The exact answer. A turn that knows its runtime id is matched by
            # it and by nothing else.
            return session_id if session_id in self.claims else None
        # Only the aliases the route recorded off the live record. A durable id
        # is never compared with a claim KEY: keys are runtime ids, and a match
        # there would be a coincidence between two id spaces, not a session.
        # Newest first: the latest claim on a stored session is the one the
        # last-claim-wins rule keeps.
        for key in reversed(self.claims):
            if self.claims[key].aliases.intersection(aliases):
                return key
        return None

    def _expire(self) -> None:
        cutoff = self.clock() - self.ttl
        while self.claims:
            oldest = next(iter(self.claims))
            if self.claims[oldest].at > cutoff:
                break
            del self.claims[oldest]


# Used only when the slot holds something this code cannot read at all.
_FALLBACK = TurnClaims()


def shared() -> TurnClaims:
    """The one store of this `SHAPE` in this process, made by whoever asks first."""
    holder = sys.modules.get(REGISTRY)
    if holder is None:
        fresh = types.ModuleType(REGISTRY)
        fresh.stores = {}
        # `setdefault` on a dict is atomic, so two copies racing here agree on
        # whichever holder landed first.
        holder = sys.modules.setdefault(REGISTRY, fresh)
    stores = getattr(holder, "stores", None)
    if not isinstance(stores, dict):
        return _fallback("the shared slot holds no store table")
    store = stores.get(SHAPE)
    if store is None:
        store = stores.setdefault(SHAPE, TurnClaims())
    if not all(callable(getattr(store, name, None)) for name in STORE_METHODS):
        return _fallback("the shared store of this shape does not answer to it")
    return store


def _fallback(reason: str) -> TurnClaims:
    """A store only this copy sees, said out loud: claims will not cross copies."""
    logger.warning(
        "hermie: using a private turn-claim store (%s); a claim made through the "
        "dashboard will not reach a turn",
        reason,
    )
    return _FALLBACK


def valid_session_id(value: Any) -> Optional[str]:
    """*value* when it is a well-formed session id, otherwise None."""
    if not isinstance(value, str) or not SESSION_ID_PATTERN.match(value):
        return None
    return value


def identity_of(session: Any) -> str:
    """`<provider>:<user id>` for a verified dashboard session, or "".

    The same spelling the server gives `auth_user_id`, stripped the same way, so
    the claim and every other rung compare as the same kind of string.
    """
    if session is None:
        return ""
    provider = str(getattr(session, "provider", "") or "").strip()
    user_id = str(getattr(session, "user_id", "") or "").strip()
    if not provider or not user_id:
        return ""
    return f"{provider}:{user_id}"
