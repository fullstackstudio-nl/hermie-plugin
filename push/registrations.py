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

# Every event a device can ask about. The first four are ADR-0017's; the last
# two are this plugin's additions, and they follow the same rule as the rest —
# absent means OFF, so a device that predates a type never starts receiving it.
PUSH_TYPES = ("message", "request", "dm", "cron", "turn_done", "turn_failed")


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
class Section:
    registrations: List[Registration] = field(default_factory=list)
    seen: Dict[str, int] = field(default_factory=dict)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _number(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _types(value: Any) -> Dict[str, bool]:
    source = value if isinstance(value, dict) else {}
    return {name: source.get(name) is True for name in PUSH_TYPES}


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
    push = app_key_value.get("push")
    if not isinstance(push, dict):
        return Section()

    rows = push.get("registrations") if isinstance(push.get("registrations"), dict) else {}
    registrations = [
        parsed
        for installation_id, value in rows.items()
        if (parsed := registration_of(str(installation_id), value, user_id)) is not None
    ]
    # A stable order, so a run's log and a test read the same twice.
    registrations.sort(key=lambda entry: entry.installation_id)

    raw_seen = push.get("seen") if isinstance(push.get("seen"), dict) else {}
    seen = {str(key): _number(at) for key, at in raw_seen.items() if _number(at) > 0}

    return Section(registrations=registrations, seen=seen)


def read_sections(items: Iterable[Tuple[str, Any]]) -> Section:
    """One view over every app-owned bag, as ``(user id, value)`` pairs.

    The pairs arrive in precedence order (legacy first), and a device is a
    device: the same installation id in two keys is one registration, the later
    one. That is what makes the move to per-user keys safe to do gradually —
    while the app writes both, nobody is notified twice.
    """
    by_installation: Dict[str, Registration] = {}
    seen: Dict[str, int] = {}
    for user_id, value in items:
        section = read_section(value, user_id)
        for registration in section.registrations:
            by_installation[registration.installation_id] = registration
        seen.update(section.seen)
    return Section(
        registrations=sorted(by_installation.values(), key=lambda entry: entry.installation_id),
        seen=seen,
    )


def someone_attached(section: Section, now: float, window_seconds: int) -> bool:
    """Was any device looking at a chat within the window?

    The gateway cannot be asked who is watching — `session.active_list` reports
    the calling connection's own session and nobody else's — so this stays what
    ADR-0017 called it: a heartbeat the app writes and this reads. It is a
    heuristic, and it fails towards a redundant notification for a chat somebody
    is already reading, which is the right direction.
    """
    return any(now - at <= window_seconds for at in section.seen.values())
