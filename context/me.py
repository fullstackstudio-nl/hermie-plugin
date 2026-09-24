"""`/me`: who this bot thinks it is talking to, and how it worked that out.

Until HERM-119 this also reported a profile the app had written about the
person — their name, device, timezone, locale, what they had written about
themselves. That went with the app's own "Context about you"; this report of
it goes with it too, because there is nothing left to report: no profile is
added to a bot's prompt any more, on any gateway, whatever the app sends.

So the whole answer is the sender resolution: the login the gateway worked a
turn's sender out to be, which rung answered, and whether that rung is one
this build actually treats as confirmed (`render.VERIFIED_RUNGS`) or merely
assumed. It does not call the model: there is nothing here a model could add,
and a person checking whether the gateway knows who they are should not have
to pay for a turn to find out.

The answer names a login, and deliberately nothing else. It is the person's
own and is the entire point of the report; no token, endpoint, key or
configuration value goes anywhere near it.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .render import (
    BY_APP_DEFAULT,
    BY_CLAIM,
    BY_CONFIGURED,
    BY_HOOK,
    BY_LIVE_SESSION,
    BY_ONLY_USER,
    BY_PLATFORM,
    BY_SESSION_VARS,
    VERIFIED_RUNGS,
)

logger = logging.getLogger(__name__)

COMMAND = "me"
DESCRIPTION = "Who this bot thinks it is talking to, and how it worked that out."

# The answer is read in a terminal, so it is bounded like everything else here.
MAX_CHARS = 2000

# One sentence per rung of the resolution order. `BY_NOBODY` has none of its
# own — see the "nobody" branch below, which is worded for that case directly
# rather than through this table.
RUNGS = {
    BY_CLAIM: "a turn claim from the app, signed in to the dashboard",
    BY_HOOK: "the sender Hermes handed the hook, which on a shared chat is whoever opened it",
    BY_PLATFORM: "the sender Hermes handed the hook, from a platform that names one per message",
    BY_LIVE_SESSION: "the login the gateway admitted this session under",
    BY_SESSION_VARS: "the login bound into this session's variables",
    BY_CONFIGURED: "the configured default (context.default_user)",
    BY_APP_DEFAULT: "the app default",
    BY_ONLY_USER: "the only person registered on this gateway",
}

# And when a rung was not established at all. A caller that resolved a sender
# without saying where it came from gets this rather than a guessed rung.
UNSAID = "somewhere this build cannot name"


def _line(label: str, value: str) -> str:
    return f"{label + ':':<12}{value}"


def answer(module: Any, raw_args: str = "") -> Optional[str]:
    """The whole command. Never raises: a broken report is not a broken session."""
    try:
        bot = module.runtime.bot_name()
        sender_id, source = module.sender_with_source()
        lines = [f"Hermie context for {bot}", ""]
        if sender_id:
            lines.append(_line("Login", sender_id))
            lines.append(_line("Worked out", f"from {RUNGS.get(source) or UNSAID}"))
            lines.append(
                _line(
                    "Confirmed",
                    "yes" if source in VERIFIED_RUNGS else "no — the gateway has not confirmed who is sending",
                )
            )
        else:
            lines.append(_line("Login", "nobody"))
            lines.append(_line("Because", "the gateway has not named anyone for this session"))
        lines.append("")
        lines.append("Nothing beyond this login is added to this bot's prompt.")
        text = "\n".join(lines)
        return text if len(text) <= MAX_CHARS else text[: MAX_CHARS - 1].rstrip() + "…"
    except Exception as exc:
        logger.warning("hermie: could not report the resolved sender: %s", exc)
        return "hermie: could not work out who this bot is talking to; see the gateway log."
