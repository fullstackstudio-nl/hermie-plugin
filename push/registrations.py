"""The devices that asked to be told, as the app wrote them.

This is the Python side of the schema ADR-0017 defines and Hermie Web's
`packages/hermie-web/src/push/registrations.ts` already reads. The two readers
must agree, because the app writes one shape and either process may be the one
that sends. They agree on the strict parts in particular: `v` is checked rather
than assumed, an entry carrying the fields of both transports is a confusion
rather than a choice, and an unreadable entry costs that entry and nothing else.

The section lives under the app's own key, and there is now one key per person:
`hermie-app:<user id>`, with the older shared `hermie-app` still read for one
version. `read_sections` merges them in the order `uimeta.read_app_sections`
hands them over — legacy first — so the per-user key wins for the same device.
Which person a registration belongs to is no longer a guess: it is the key it
was found under, and it is carried on the registration as `user_id`.

The plugin reads these keys and never writes them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

SECTION_VERSION = 1

# Every event a device can ask about, and every one it can be sent. Absent means
# OFF, so a device that predates a type never starts receiving it.
#
# ADR-0017 also listed a bot-to-bot `dm`. Hermes fires no hook when one arrives,
# so nothing can produce it — the type is gone rather than kept as a switch that
# turns nothing on.
PUSH_TYPES = ("message", "request", "cron", "turn_done", "turn_failed")



@dataclass(frozen=True)
class Registration:
    installation_id: str
    transport: str  # "expo" | "webpush"
    platform: str
    types: Dict[str, bool]
    preview: bool
    updated_at: int
    token: Optional[str] = None
    endpoint: Optional[str] = None
    keys: Dict[str, str] = field(default_factory=dict)
    # Whose device this is: the ui_meta key it was read from. Empty means the
    # legacy shared key, which names nobody.
    user_id: str = ""

    def wants(self, push_type: str) -> bool:
        return self.types.get(push_type, False)


@dataclass(frozen=True)
class Seen:
    """One device saying what it is looking at, and when it last said so."""

    at: int
    # Which chat that device has open. Empty means the device said only that
    # somebody was looking, without naming a bot — the shape older apps wrote.
    bot: str = ""


@dataclass(frozen=True)
class Section:
    registrations: List[Registration] = field(default_factory=list)
    # installation id -> that device's heartbeat.
    seen: Dict[str, Seen] = field(default_factory=dict)
    # user id -> bot -> the second at which the mute lapses, 0 meaning never.
    mutes: Dict[str, Dict[str, int]] = field(default_factory=dict)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _number(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _types(value: Any) -> Dict[str, bool]:
    source = value if isinstance(value, dict) else {}
    return {name: source.get(name) is True for name in PUSH_TYPES}


def seen_of(value: Any) -> Optional[Seen]:
    """One heartbeat, in either shape, or nothing.

    The shape the app writes now says which chat the device has open:

        seen: {"<installation id>": {"bot": "<bot>", "at": <unix second>}}

    A bare number is the older shape and means "looking at some chat", with no
    way to tell which. It is read for one version and it suppresses the same way
    it always did — for every chat on that one device.
    """
    if isinstance(value, dict):
        at = _number(value.get("at"))
        return Seen(at=at, bot=_text(value.get("bot"))) if at > 0 else None
    at = _number(value)
    return Seen(at=at) if at > 0 else None


def _remember(into: Dict[str, Seen], installation_id: str, heartbeat: Optional[Seen]) -> None:
    """Keep the newer of two heartbeats for one device.

    A heartbeat can arrive from the `seen` map and from the registration entry
    itself, and across two keys while the app migrates. Newest wins, because the
    question it answers is "is somebody looking *now*" and an older answer to
    that is simply a worse one.
    """
    if heartbeat is None or not installation_id:
        return
    current = into.get(installation_id)
    if current is None or heartbeat.at >= current.at:
        into[installation_id] = heartbeat


def looking_at(
    section: Section, installation_id: str, bot: str, now: float, window_seconds: int
) -> bool:
    """Is *this device* looking at *this bot's* chat right now?

    The gateway cannot be asked who is watching — `session.active_list` reports
    the calling connection's own session and nobody else's — so this stays what
    ADR-0017 called it: a heartbeat the app writes and this reads. It is a
    heuristic, and it fails towards a redundant notification for a chat somebody
    is already reading, which is the right direction.
    """
    heartbeat = section.seen.get(installation_id)
    if heartbeat is None or now - heartbeat.at > window_seconds:
        return False
    # A heartbeat that names no bot is the older shape: it says a chat is open
    # without saying which, so it still covers every chat on that device.
    return not heartbeat.bot or heartbeat.bot == bot


def _mutes(value: Any) -> Dict[str, int]:
    """One person's mute list: bot -> `until`, dropping what is not a number.

    `until` is a unix second, and `0` means forever. A negative or absent value
    is not a mute: the app is the only writer here, and a mute that cannot be
    read is a bot that keeps notifying, which is the recoverable direction.
    """
    source = value if isinstance(value, dict) else {}
    return {
        str(bot): _number(until)
        for bot, until in source.items()
        if str(bot) and (isinstance(until, int) and not isinstance(until, bool) and until >= 0)
    }


def mutes_of(app_key_value: Any) -> Dict[str, int]:
    """The `mutes` map out of one app-owned bag, as the app writes it:

        mutes: {"<bot>": <until>}

    It lives at the top of the bag, beside `push` and `context`, because a mute
    is a fact about a person and a bot rather than about a transport. A copy
    under `push` is honoured too, for an app that files it with the rest of the
    push settings; the top-level one wins where they disagree.
    """
    if not isinstance(app_key_value, dict):
        return {}
    push = app_key_value.get("push") if isinstance(app_key_value.get("push"), dict) else {}
    return {**_mutes(push.get("mutes")), **_mutes(app_key_value.get("mutes"))}


def is_muted(section: Section, user_id: str, bot: str, now: float) -> bool:
    """Whether this person has silenced this bot right now.

    `0` is forever. An `until` that has passed is not a mute — the app is not
    obliged to come back and tidy up an expired entry, and a gateway that
    treated a lapsed mute as a live one would go quiet for good.
    """
    until = section.mutes.get(user_id, {}).get(bot)
    if until is None:
        return False
    return until == 0 or until > now


def registration_of(installation_id: str, value: Any, user_id: str = "") -> Optional[Registration]:
    """One registration, or nothing."""
    if not installation_id or not isinstance(value, dict):
        return None
    if _number(value.get("v")) != SECTION_VERSION:
        return None

    transport = _text(value.get("transport"))
    token = _text(value.get("token"))
    endpoint = _text(value.get("endpoint"))
    raw_keys = value.get("keys") if isinstance(value.get("keys"), dict) else {}
    p256dh = _text(raw_keys.get("p256dh"))
    auth = _text(raw_keys.get("auth"))

    common = {
        "installation_id": installation_id,
        "user_id": user_id,
        "platform": _text(value.get("platform")) or "unknown",
        "types": _types(value.get("types")),
        "preview": value.get("preview") is True,
        "updated_at": _number(value.get("updatedAt")),
    }

    if transport == "expo":
        return Registration(transport="expo", token=token, **common) if token and not endpoint else None
    if transport == "webpush":
        if endpoint and p256dh and auth and not token:
            return Registration(
                transport="webpush", endpoint=endpoint, keys={"p256dh": p256dh, "auth": auth}, **common
            )
        return None
    return None


def read_section(app_key_value: Any, user_id: str = "") -> Section:
    """The whole ``push`` section out of one app-owned bag."""
    if not isinstance(app_key_value, dict):
        return Section()
    mutes = {user_id: found} if (found := mutes_of(app_key_value)) else {}
    push = app_key_value.get("push")
    if not isinstance(push, dict):
        return Section(mutes=mutes)

    rows = push.get("registrations") if isinstance(push.get("registrations"), dict) else {}
    registrations: List[Registration] = []
    seen: Dict[str, Seen] = {}
    for key, value in rows.items():
        installation_id = str(key)
        parsed = registration_of(installation_id, value, user_id)
        if parsed is not None:
            registrations.append(parsed)
        # A heartbeat filed on the registration itself is honoured too, for an
        # app that keeps a device's "what am I looking at" beside the device.
        if isinstance(value, dict):
            _remember(seen, installation_id, seen_of(value.get("seen")))
    # A stable order, so a run's log and a test read the same twice.
    registrations.sort(key=lambda entry: entry.installation_id)

    raw_seen = push.get("seen") if isinstance(push.get("seen"), dict) else {}
    for key, value in raw_seen.items():
        _remember(seen, str(key), seen_of(value))

    return Section(registrations=registrations, seen=seen, mutes=mutes)


def read_sections(items: Iterable[Tuple[str, Any]]) -> Section:
    """One view over every app-owned bag, as ``(user id, value)`` pairs.

    The pairs arrive in precedence order (legacy first), and a device is a
    device: the same installation id in two keys is one registration, the later
    one. That is what makes the move to per-user keys safe to do gradually —
    while the app writes both, nobody is notified twice.
    """
    by_installation: Dict[str, Registration] = {}
    seen: Dict[str, Seen] = {}
    mutes: Dict[str, Dict[str, int]] = {}
    for user_id, value in items:
        section = read_section(value, user_id)
        for registration in section.registrations:
            by_installation[registration.installation_id] = registration
        for installation_id, heartbeat in section.seen.items():
            _remember(seen, installation_id, heartbeat)
        mutes.update(section.mutes)
    return Section(
        registrations=sorted(by_installation.values(), key=lambda entry: entry.installation_id),
        seen=seen,
        mutes=mutes,
    )
