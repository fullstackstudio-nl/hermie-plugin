"""`/me`: who this bot thinks it is talking to, and how it worked that out.

The context module decides whose notes go into a prompt, and until now the only
way to find out what it decided was to read the prompt. That is a bad way to
debug a feature whose whole job is to be invisible, and a worse way to answer
the question a person actually has, which is "does my bot know who I am?".

So the plugin answers it directly. `/me` runs in the session, resolves exactly
what a turn would resolve, and prints it. It does not call the model: there is
nothing here a model could add, and a person checking whether their identity
reached the gateway should not have to pay for a turn to find out.

The answer names ids, and deliberately nothing else. The login id and the
registered id are the person's own and are the entire point of the report; no
token, endpoint, key or configuration value goes anywhere near it.
"""

from __future__ import annotations

import logging
import time
from typing import Any, List, Optional

from .render import (
    BY_APP_DEFAULT,
    BY_CONFIGURED,
    BY_HOOK,
    BY_LIVE_SESSION,
    BY_NOBODY,
    BY_ONLY_USER,
    BY_SESSION_VARS,
    ContextSection,
    UserContext,
    resolve_with_reason,
)

logger = logging.getLogger(__name__)

COMMAND = "me"
DESCRIPTION = "Who this bot thinks it is talking to, and how it worked that out."

# The answer is read in a terminal, so it is bounded like everything else here.
MAX_CHARS = 2000
ABOUT_CHARS = 240

# One sentence per rung of the resolution order.
RUNGS = {
    BY_HOOK: "the sender Hermes handed the hook",
    BY_SESSION_VARS: "the login bound into this session's variables",
    BY_LIVE_SESSION: "the login the gateway admitted this session under",
    BY_CONFIGURED: "the configured default (context.default_user)",
    BY_APP_DEFAULT: "the default the app set",
    BY_ONLY_USER: "the only person registered on this gateway",
    BY_NOBODY: "nobody",
}

# What to do about an answer of "nobody". It is one line because it is one
# action, and the person reading this is standing in front of the app.
FIX = "To fix: accept the sharing notice in Hermie's Settings → Context, then send a message."


def _line(label: str, value: str) -> str:
    return f"{label + ':':<12}{value}"


def _when(stamp: int) -> str:
    if not stamp:
        return "no update time"
    try:
        return "updated " + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(stamp))
    except Exception:
        return "no update time"


def _origin(section: ContextSection, user: UserContext) -> str:
    """Which of the app's own ui_meta keys this entry was read out of."""
    from .. import uimeta

    owner = section.origins.get(user.user_id)
    if owner is None:
        return "the app's metadata"
    key = uimeta.app_key_for(owner)
    return key if owner else f"{key} (the shared key, which the app is moving away from)"


def _person(
    section: ContextSection, user: UserContext, rung: str, sender_id: str, bot: str, by_sender: bool
) -> List[str]:
    lines = [_line("Talking to", user.display_name or "(no display name set)")]
    lines.append(_line("Worked out", f"from {RUNGS.get(rung, rung)}"))

    if sender_id and by_sender:
        if sender_id == user.user_id:
            lines.append(_line("Login", sender_id))
        else:
            lines.append(_line("Login", f"{sender_id} → matched the registered id {user.user_id}"))
    else:
        lines.append(_line("Registered", user.user_id))
        if sender_id:
            # A sender that named nobody is worth seeing: it is the difference
            # between "the gateway said nothing" and "it said someone we have
            # never heard of", and only one of those is the app's problem.
            lines.append(_line("Sender", f"{sender_id} (no entry for this id)"))

    device = " running ".join(part for part in (user.device_model, user.device_os) if part)
    if device:
        lines.append(_line("Device", device + (f", app {user.app_version}" if user.app_version else "")))
    where = ", ".join(part for part in (user.timezone, user.locale) if part)
    if where:
        lines.append(_line("Dates", where))
    if user.about:
        about = user.about if len(user.about) <= ABOUT_CHARS else user.about[: ABOUT_CHARS - 1].rstrip() + "…"
        lines.append(_line("About", about))
    note = user.per_bot.get(bot) if bot else ""
    if note:
        lines.append(_line("This bot", note))
    lines.append(_line("From", f"{_origin(section, user)}, {_when(user.updated_at)}"))
    return lines


def _nobody(section: ContextSection, sender_id: str) -> List[str]:
    if not section.users:
        why = "the app has registered nobody on this gateway"
    elif sender_id:
        why = f"the gateway named {sender_id}, which matches none of the {len(section.users)} people registered here"
    else:
        why = (
            f"the gateway named nobody and {len(section.users)} people are registered here, "
            "so there is no way to tell which of them is asking"
        )
    return [
        _line("Talking to", "nobody"),
        _line("Because", why),
        "",
        "No context is added to this bot's prompt.",
        FIX,
    ]


def answer(module: Any, raw_args: str = "") -> Optional[str]:
    """The whole command. Never raises: a broken report is not a broken session."""
    try:
        bot = module.runtime.bot_name()
        section = module.section()
        sender_id, source = module.sender_with_source()
        user, reason = resolve_with_reason(
            section, sender_id=sender_id, configured_default=module.configured_default
        )
        # The sender rung reports *where* the sender came from, which is the
        # part a person debugging this actually needs.
        rung = source if (user is not None and reason == BY_HOOK and source) else reason
        lines = [f"Hermie context for {bot}", ""]
        lines += (
            _person(section, user, rung, sender_id, bot, reason == BY_HOOK)
            if user is not None
            else _nobody(section, sender_id)
        )
        text = "\n".join(lines)
        return text if len(text) <= MAX_CHARS else text[: MAX_CHARS - 1].rstrip() + "…"
    except Exception as exc:
        logger.warning("hermie: could not report the resolved context: %s", exc)
        return "hermie: could not work out who this bot is talking to; see the gateway log."
