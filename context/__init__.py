"""The context module: who the bot is talking to, said once instead of every turn.

Two Hermes surfaces can carry this, and they are good at opposite things.

`register_system_prompt_section` puts bounded text in the system prompt. It is
rendered ONCE, when a session's prompt is first built, and core then persists it
verbatim and replays it — so it never appears in the transcript as a message and
it never grows with the conversation. What it cannot do is know who is asking:
the mapping a section callable receives carries `session_id`, `model`,
`provider`, `platform`, `profile_name` and `cwd`, and no identity at all.

`pre_llm_call` knows exactly who is asking — it is handed `sender_id`, which is
the agent's `_user_id`, which on a WebSocket session is the `auth_user_id`
stamped from the login. What it cannot do is be free: whatever it returns is
appended to the user's message, on every turn it fires.

So the module uses both, and uses the expensive one as little as possible: the
frozen section carries the resolved default user, and `pre_llm_call` contributes
text only when the person actually sending this turn is somebody else. On the
single-user gateway that is every Hermie install today, the second path never
fires and the per-turn cost is zero.

Two limits are real and are written down in DESIGN.md rather than papered over:
an ungated gateway has no `auth_user_id` at all, so `sender_id` is empty and
only the default applies; and a section frozen at session start does not change
when the person edits their profile, so an edit reaches a long-running Bot Chat
on its next session.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Mapping, Optional

from .. import contract
from .render import ContextSection, read_section, render, resolve

logger = logging.getLogger(__name__)

NAME = "context"

SECTION_ID = "hermie.device"


class ContextModule:
    def __init__(self, runtime):
        self.runtime = runtime
        # session id -> the user id whose context the frozen section describes.
        # Used to answer "is this sender the one the section already covers?".
        self.frozen_for: Dict[str, str] = {}
        self.lock = threading.Lock()

    @property
    def max_chars(self) -> int:
        return int(self.runtime.config("context.max_chars", 1200) or 1200)

    @property
    def configured_default(self) -> str:
        return str(self.runtime.config("context.default_user", "") or "")

    def section(self) -> ContextSection:
        return read_section(self.runtime.app_ui_meta())

    def capabilities(self) -> list:
        found = [contract.CAP_CONTEXT_PROMPT]
        if any(user.per_bot for user in self.section().users.values()):
            found.append(contract.CAP_CONTEXT_PER_BOT)
        return found

    # -- the frozen section --------------------------------------------------

    def render_section(self, session_info: Mapping[str, Any]) -> str:
        """Called by core once per session, while the prompt is being built."""
        try:
            bot = str(session_info.get("profile_name") or "") or self.runtime.bot_name()
            section = self.section()
            user = resolve(section, configured_default=self.configured_default)
            if user is not None:
                with self.lock:
                    self.frozen_for[str(session_info.get("session_id") or "")] = user.user_id
            return render(user, bot=bot, max_chars=self.max_chars)
        except Exception as exc:
            # A raising section callable costs core the whole plugin-section
            # block, so this one never raises.
            logger.warning("hermie: could not render the device context: %s", exc)
            return ""

    # -- the per-turn top-up -------------------------------------------------

    def on_pre_llm_call(self, **kwargs: Any) -> Optional[Dict[str, str]]:
        """Contribute context only for a sender the frozen section does not cover."""
        try:
            sender_id = str(kwargs.get("sender_id") or "")
            if not sender_id:
                # An ungated gateway names nobody. The frozen section is all
                # there is, and it is already in the prompt.
                return None
            session_id = str(kwargs.get("session_id") or "")
            with self.lock:
                already = self.frozen_for.get(session_id, "")
            if already and already == sender_id:
                return None

            section = self.section()
            user = resolve(section, sender_id=sender_id, configured_default=self.configured_default)
            if user is None or user.user_id == already:
                return None
            text = render(user, bot=self.runtime.bot_name(), max_chars=self.max_chars)
            return {"context": text} if text else None
        except Exception as exc:
            logger.warning("hermie: could not add per-sender context: %s", exc)
            return None


def register(ctx, runtime) -> ContextModule:
    module = ContextModule(runtime)
    try:
        ctx.register_system_prompt_section(
            SECTION_ID,
            module.render_section,
            # `after_memory` is the only position core accepts today; naming it
            # keeps this honest if a second one is ever added.
            position="after_memory",
            max_chars=min(module.max_chars, 4000),
        )
    except Exception as exc:
        # An older Hermes without the registrar, or a section id somebody else
        # claimed. The per-turn path still works, so this degrades rather than
        # failing the whole plugin.
        logger.warning("hermie: system prompt section unavailable (%s); per-turn context only", exc)
    ctx.register_hook("pre_llm_call", module.on_pre_llm_call)
    return module
