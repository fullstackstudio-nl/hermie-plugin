"""Turning what a hook says into what a device is told, or into nothing at all.

Everything here is a pure function of a hook's kwargs and the current
registrations, so the whole decision — notify or not, whom, saying what — is
testable without a gateway, a network, or a clock.

Three rules shape it, all of them from ADR-0017:

- **The payload says who, not what.** A bot's name and an event type. The text
  rides only when that device turned `preview` on AND the gateway allows it, and
  the gateway's setting is the stricter of the two.
- **A message is suppressed on the device that is looking at that chat.** Not on
  the others, and not for another bot: a phone in a pocket should still buzz
  while the same person reads that chat on a laptop. Requests are not
  suppressed at all — a question with a countdown on it is worth a buzz even if
  the chat is open in another room.
- **A muted bot is silent on every device that person owns.** That one is not a
  heuristic and not per type: somebody said no.
- **Every notification is a hint, never an instruction.** Nothing in a payload
  is an id the app acts on without re-reading the gateway first.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .registrations import Registration, Section, is_muted, looking_at

PAYLOAD_VERSION = 1

# Everything the app is told about. `dm` is in ADR-0017 and is NOT here: Hermes
# fires no hook when a bot-to-bot DM arrives (see DESIGN.md), so the plugin does
# not advertise it rather than advertising a type that never arrives.
TYPES = ("message", "request", "cron", "turn_done", "turn_failed")

# Types a device is told about even when it says somebody is watching.
NEVER_SUPPRESSED = ("request", "cron", "turn_failed")


@dataclass(frozen=True)
class Notification:
    type: str
    bot: str
    title: str
    body: str
    event_id: str
    session_id: str = ""
    at: int = 0
    text: str = ""  # the previewable content, never sent unless preview is on
    extra: Dict[str, Any] = field(default_factory=dict)

    def payload(self, *, preview: bool) -> Dict[str, Any]:
        """What travels. Keep this small: APNs caps at ~4KB and so does the rest."""
        body = {
            "v": PAYLOAD_VERSION,
            "type": self.type,
            "bot": self.bot,
            "at": self.at,
            "eventId": self.event_id,
        }
        if self.session_id:
            body["sessionId"] = self.session_id
        for key, value in self.extra.items():
            body[key] = value
        if preview and self.text:
            body["preview"] = self.text[:200]
        return body

    def rendered(self, *, preview: bool) -> tuple[str, str]:
        """The two strings a lock screen shows."""
        if preview and self.text:
            return self.bot, self.text[:200]
        return self.title, self.body


def event_id(kind: str, *parts: Any) -> str:
    """A stable id for one fact, so two hooks describing it buzz once.

    Built from the identity of the event rather than from a counter, because the
    same turn can be described by `post_llm_call` and by `on_session_end` and
    those two must collide on purpose.
    """
    digest = hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()
    return f"{kind}:{digest[:32]}"


def _clean(text: Any, limit: int = 200) -> str:
    return " ".join(str(text or "").split())[:limit]


def from_assistant_message(
    *, bot: str, session_id: str, turn_id: str, assistant_response: Any, at: int
) -> Optional[Notification]:
    """A bot wrote something (`post_llm_call`)."""
    text = _clean(assistant_response, 400)
    if not text:
        return None
    return Notification(
        type="message",
        bot=bot,
        title=bot,
        body="New message",
        text=text,
        session_id=session_id,
        at=at,
        event_id=event_id("message", session_id, turn_id),
    )


def from_approval(
    *, bot: str, session_key: str, description: Any, request_id: Any, turn_id: Any, at: int
) -> Notification:
    """An agent stopped to ask (`pre_approval_request`).

    The request id travels so the app can find the request again — and it finds
    it by asking the gateway, not by trusting this. A notification that names a
    request which is already answered opens an app that says so.
    """
    return Notification(
        type="request",
        bot=bot,
        title=bot,
        body="Needs your approval",
        text=_clean(description, 200),
        session_id=str(session_key or ""),
        at=at,
        event_id=event_id("request", session_key, request_id or turn_id or description),
        extra={"requestId": str(request_id or "")} if request_id else {},
    )


def from_clarify(*, bot: str, session_id: str, tool_call_id: Any, question: Any, at: int) -> Notification:
    """An agent asked a question (`pre_tool_call` for the `clarify` tool).

    Hermes fires no clarify-specific hook, so this rides the generic tool hook.
    The clarify request id is minted inside the gateway's blocking prompt and is
    not visible here, so the payload carries no id at all: the app opens the
    chat and finds the open question itself.
    """
    return Notification(
        type="request",
        bot=bot,
        title=bot,
        body="Asked you a question",
        text=_clean(question, 200),
        session_id=session_id,
        at=at,
        event_id=event_id("clarify", session_id, tool_call_id),
    )


def from_session_end(
    *, bot: str, session_id: str, turn_id: Any, completed: Any, failed: Any, interrupted: Any, at: int
) -> Optional[Notification]:
    """A turn finished, one way or another (`on_session_end`)."""
    if interrupted:
        # Somebody pressed stop. They know.
        return None
    if failed or not completed:
        return Notification(
            type="turn_failed",
            bot=bot,
            title=bot,
            body="A turn failed",
            session_id=session_id,
            at=at,
            event_id=event_id("turn_failed", session_id, turn_id),
        )
    return Notification(
        type="turn_done",
        bot=bot,
        title=bot,
        body="Finished working",
        session_id=session_id,
        at=at,
        event_id=event_id("turn_done", session_id, turn_id),
    )


def recipients(
    notification: Notification,
    section: Section,
    *,
    now: float,
    attached_window_seconds: int,
    enabled_types: tuple,
    gateway_preview: str,
    retired,
) -> List[tuple[Registration, bool]]:
    """Who gets this, and whether their copy may carry the text.

    `retired` is a predicate over (installation id, updatedAt) so the state file
    can veto a registration a transport already told us is dead, without this
    function needing to know what a state file is.
    """
    if notification.type not in enabled_types:
        return []
    suppressible = notification.type not in NEVER_SUPPRESSED

    out: List[tuple[Registration, bool]] = []
    for registration in section.registrations:
        if not registration.wants(notification.type):
            continue
        # A mute is the person's decision about a bot, so it outranks every
        # per-type switch: it silences this bot on every device that person
        # registered, including the types that are never suppressed.
        if is_muted(section, registration.user_id, notification.bot, now):
            continue
        # Suppression is per device and per chat: this one says it is reading
        # this bot right now, so it is told nothing. Every other device of the
        # same person still is.
        if suppressible and looking_at(
            section, registration.installation_id, notification.bot, now, attached_window_seconds
        ):
            continue
        if retired(registration.installation_id, registration.updated_at):
            continue
        # The gateway's policy is a ceiling, never a floor: `never` overrides a
        # device that asked for previews, and `device` never turns one on.
        preview = gateway_preview == "device" and registration.preview
        out.append((registration, preview))
    return out
