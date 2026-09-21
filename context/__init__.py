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

On the dashboard's WebSocket route `_user_id` is often empty while the gateway
plainly does know who logged in. Two places can still say so, and both paths
use them in turn: `HERMES_SESSION_USER_ID`, when the gateway binds the login
into the session variables, and failing that the gateway's own table of live
sessions, which stamps the login on the record and sits in this very process
(`live_session.py`). Either is an identity a frozen section can reach, which
`session_info` is not.

So the module uses both, and uses the expensive one as little as possible: the
frozen section carries the resolved default user, and `pre_llm_call` contributes
text only when the person actually sending this turn is somebody else. On the
single-user gateway that is every Hermie install today, the second path never
fires and the per-turn cost is zero.

Two limits are real and are written down in DESIGN.md rather than papered over:
an ungated gateway names nobody anywhere — no `auth_user_id`, nothing bound in
the session variables — so only the default applies; and a section frozen at
session start does not change when the person edits their profile, so an edit
reaches a long-running Bot Chat on its next session.

`pre_llm_call` does one more thing, in `session_vars.py`: when Hermes' own
`HERMES_SESSION_USER_*` variables are empty and this module can say who is
asking, it fills them in for the call, so a tool that reads them sees the person
rather than nobody. It is off with `context.session_vars: false` and it is a
no-op on any gateway that already fills them.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .. import contract
from .render import (
    BY_HOOK,
    BY_LIVE_SESSION,
    BY_SESSION_VARS,
    RETRACTED,
    ContextSection,
    read_sections,
    render,
    resolve,
    same_user,
)
from . import me as me_command
from .live_session import LiveSessions
from .session_vars import (
    SESSION_NAMES,
    USER_ID,
    USER_ID_ALT,
    USER_NAME,
    SessionVars,
)

logger = logging.getLogger(__name__)

NAME = "context"

SECTION_ID = "hermie.device"

# How many sessions' frozen sections are remembered. A gateway that is up for
# months sees an unbounded number of session ids, and a record per session is a
# leak with a slow fuse. Losing the oldest costs a redundant re-injection on a
# chat nobody has touched in a thousand sessions, which is the cheap direction.
FROZEN_SESSIONS = 512


@dataclass(frozen=True)
class Frozen:
    """What went into one session's system prompt, and when.

    `stamp` is the app's `profile.yaml` as it stood when the section was
    rendered, so a turn can ask "could this have changed?" with one `stat`
    rather than a YAML parse. `text` is what was actually frozen, so the answer
    to "did it change?" is a comparison rather than a guess — a write that
    touches somebody else's push registration moves the stamp without changing
    a word of this, and that must not cost a turn an injection.
    """

    user_id: str
    text: str
    stamp: Tuple[int, int]


class ContextModule:
    def __init__(
        self,
        runtime,
        session_vars: Optional[SessionVars] = None,
        live_sessions: Optional[LiveSessions] = None,
    ):
        self.runtime = runtime
        self.session_vars = session_vars if session_vars is not None else SessionVars()
        self.live_sessions = live_sessions if live_sessions is not None else LiveSessions()
        # session id -> what the frozen section says, as it was frozen. It
        # answers two questions: "is this sender the one the section already
        # covers?" and "does the section still say what the app says?".
        self.frozen: Dict[str, Frozen] = {}
        self.lock = threading.Lock()
        # Set at registration: a capability is advertised once the thing it
        # names is actually there, never because the code for it shipped.
        self.command_registered = False

    @property
    def max_chars(self) -> int:
        return int(self.runtime.config("context.max_chars", 1200) or 1200)

    @property
    def fills_session_vars(self) -> bool:
        return self.runtime.config("context.session_vars", True) is not False

    def session_sender(self) -> str:
        """The login the gateway bound into the session variables, or nothing.

        This is the second source of a sender, used wherever the first one is
        empty. It is a read, not a write, so `context.session_vars` does not
        gate it: that switch is about filling the variables in, and a gateway
        that has asked not to be written to can still be asked who it let in.
        """
        return self.session_vars.read(USER_ID)

    def live_sender(self, session_id: str = "") -> str:
        """The login the gateway admitted this session under, or nothing.

        The third source, and the only one that works on a dashboard gateway
        nobody has patched: the id of the turn goes to the gateway's own table
        of live sessions, which stamped the login on the record when the
        WebSocket authenticated. The id may arrive as the hook's `session_id`
        or in Hermes' own session variables, and the table can be keyed by
        either shape, so everything that might name this session is offered.
        """
        return self.live_sessions.login(
            session_id, *(self.session_vars.read(name) for name in SESSION_NAMES)
        )

    def sender_with_source(self, named: str = "", session_id: str = "") -> Tuple[str, str]:
        """Who is asking, and which of the three rungs answered.

        This is the first half of the resolution order; `resolve` in
        `render.py` carries the rest (the configured default, the app's own,
        and the only registered person). Each rung costs more than the one
        above it and none of them is reached while a cheaper one answers.
        """
        if named:
            return named, BY_HOOK
        found = self.session_sender()
        if found:
            return found, BY_SESSION_VARS
        found = self.live_sender(session_id)
        if found:
            return found, BY_LIVE_SESSION
        return "", ""

    def sender(self, named: str = "", session_id: str = "") -> str:
        """Who is asking, by the same order, when the rung does not matter."""
        return self.sender_with_source(named, session_id)[0]

    @property
    def configured_default(self) -> str:
        return str(self.runtime.config("context.default_user", "") or "")

    def section(self) -> ContextSection:
        return read_sections(self.runtime.app_sections())

    def capabilities(self) -> list:
        # `context.live` is claimed unconditionally once this module is on: the
        # per-turn hook is registered below whatever else failed, and it is the
        # half that carries an edit. A gateway whose prompt section never
        # registered at all is a gateway where every turn is a live one.
        found = [contract.CAP_CONTEXT_PROMPT, contract.CAP_CONTEXT_LIVE]
        if any(user.per_bot for user in self.section().users.values()):
            found.append(contract.CAP_CONTEXT_PER_BOT)
        if self.command_registered:
            found.append(contract.CAP_COMMAND_ME)
        return found

    # -- the command ---------------------------------------------------------

    def on_me_command(self, raw_args: str = "") -> Optional[str]:
        """`/me`, answered here rather than by the model. See `me.py`."""
        return me_command.answer(self, raw_args)

    # -- the frozen section --------------------------------------------------

    def remember(self, session_id: str, user_id: str, text: str, stamp: Tuple[int, int]) -> None:
        """Record what this session's prompt now says about a person."""
        with self.lock:
            # Re-inserting rather than assigning moves this session to the back
            # of the queue, so the one evicted is the one nothing has touched
            # for longest rather than the one that merely started earliest.
            self.frozen.pop(session_id, None)
            self.frozen[session_id] = Frozen(user_id=user_id, text=text, stamp=stamp)
            while len(self.frozen) > FROZEN_SESSIONS:
                self.frozen.pop(next(iter(self.frozen)))

    def frozen_section(self, session_id: str) -> Optional[Frozen]:
        with self.lock:
            return self.frozen.get(session_id)

    def render_section(self, session_info: Mapping[str, Any]) -> str:
        """Called by core once per session, while the prompt is being built.

        `session_info` names no sender — it carries `session_id`, `model`,
        `provider`, `platform`, `profile_name` and `cwd`, and core builds it
        out of the agent alone — so the sender here is one the module goes and
        asks for. Both sources it can ask answer here: the session variables
        are bound before the prompt is built, and the session record exists
        from the moment the WebSocket was admitted. So the right person is
        frozen into the prompt, and the per-turn top-up has nothing left to
        add.
        """
        try:
            bot = str(session_info.get("profile_name") or "") or self.runtime.bot_name()
            # The stamp is taken BEFORE the read, so an edit that lands between
            # the two is remembered as older than it is and is noticed on the
            # next turn. The other order would swallow it for the session.
            stamp = self.runtime.app_stamp()
            section = self.section()
            user = resolve(
                section,
                sender_id=self.sender(session_id=str(session_info.get("session_id") or "")),
                configured_default=self.configured_default,
            )
            text = render(user, bot=bot, max_chars=self.max_chars)
            if user is not None:
                self.remember(str(session_info.get("session_id") or ""), user.user_id, text, stamp)
            return text
        except Exception as exc:
            # A raising section callable costs core the whole plugin-section
            # block, so this one never raises.
            logger.warning("hermie: could not render the device context: %s", exc)
            return ""

    # -- telling Hermes who is asking ----------------------------------------

    def fill_session_vars(self, sender_id: str, section_of: Callable[[], ContextSection]) -> Dict[str, str]:
        """Fill in `HERMES_SESSION_USER_*` for this call when they are empty.

        Whose turn it is comes from the same order the rest of the module
        uses: the sender the caller worked out — handed to the hook, bound into
        the variables, or read off the live session record — and then the
        resolved default. A gateway that already knows is left alone: this is a
        shim over a gap, and it goes quiet the day the gap closes. See
        `session_vars.py` for what the write can and cannot reach.

        `section_of` is a callable rather than a section because every gate
        above it is free and reading the section is a file read on the agent's
        own path. On a gateway that already names its user, this costs one
        `ContextVar` lookup a turn and touches no disk.
        """
        if not self.fills_session_vars or not self.session_vars.available():
            return {}
        if self.session_vars.read(USER_ID):
            return {}
        section = section_of()
        user = resolve(section, sender_id=sender_id, configured_default=self.configured_default)
        user_id = sender_id or (user.user_id if user is not None else "")
        if not user_id:
            return {}
        # The id goes in as the gateway spelled it, prefix and all — it is a
        # fact about this turn. The name only goes in when it is that same
        # person's, which a provider prefix does not change.
        named = user if user is not None and same_user(user.user_id, user_id) else None
        return self.session_vars.fill({
            USER_ID: user_id,
            USER_ID_ALT: named.user_id_alt if named is not None else "",
            USER_NAME: named.display_name if named is not None else "",
        })

    # -- the per-turn top-up -------------------------------------------------

    def refresh(
        self, session_id: str, frozen: Frozen, sender_id: str, section_of: Callable[[], ContextSection]
    ) -> Optional[Dict[str, str]]:
        """The same person, after they edited their profile mid-chat.

        The section in the system prompt was rendered once and core will not
        render it again until the session is rebuilt, so an edit made during a
        long Bot Chat would otherwise reach the bot on its *next* chat. This is
        the turn-shaped half of the same answer: the newer text rides the user
        message, saying which of the two copies wins.

        It is gated on the app's `profile.yaml` having actually moved, which is
        one `stat`. On the overwhelmingly common turn — nobody edited anything
        — this costs that one syscall and reads nothing.
        """
        stamp = self.runtime.app_stamp()
        if stamp == frozen.stamp:
            return None

        user = resolve(section_of(), sender_id=sender_id, configured_default=self.configured_default)
        text = render(user, bot=self.runtime.bot_name(), max_chars=self.max_chars)
        if text == frozen.text:
            # The file moved for something else entirely — a push registration,
            # a heartbeat, another person's bag. Remember the new stamp so this
            # is looked at once rather than on every turn from here on.
            self.remember(session_id, frozen.user_id, frozen.text, stamp)
            return None

        self.remember(session_id, user.user_id if user is not None else frozen.user_id, text, stamp)
        if text:
            return {"context": render(
                user, bot=self.runtime.bot_name(), max_chars=self.max_chars, supersedes=True
            )}
        # They emptied it. The frozen copy cannot be taken back out of the
        # system prompt, so the only honest thing left is to say it is gone.
        return {"context": RETRACTED}

    def on_pre_llm_call(self, **kwargs: Any) -> Optional[Dict[str, str]]:
        """Contribute context the frozen section does not already carry.

        Two ways it can fail to: the person sending this turn is not the one
        the section describes, or they are that person and have since changed
        what it says.
        """
        try:
            session_id = str(kwargs.get("session_id") or "")
            sender_id = self.sender(str(kwargs.get("sender_id") or ""), session_id)
            # One read of the app's metadata per turn at most, shared by the
            # shim and the top-up, and skipped entirely when neither needs it.
            cache: List[ContextSection] = []

            def section_of() -> ContextSection:
                if not cache:
                    cache.append(self.section())
                return cache[0]

            self.fill_session_vars(sender_id, section_of)

            frozen = self.frozen_section(session_id)
            # "Covers this turn" includes the turn that names nobody: an
            # ungated gateway is the single-user install, where the section
            # resolved a person without being told one and an edit to that
            # person is exactly the edit that must get through.
            if frozen is not None and (not sender_id or same_user(frozen.user_id, sender_id)):
                return self.refresh(session_id, frozen, sender_id, section_of)

            if not sender_id:
                # Nobody named, and no section to be stale. There is nothing
                # this path could work out that the frozen one did not.
                return None
            already = frozen.user_id if frozen is not None else ""

            section = section_of()
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
    try:
        # Returns None rather than raising when the name is taken, so the
        # capability follows the handle and not the attempt.
        handle = ctx.register_command(
            me_command.COMMAND, module.on_me_command, description=me_command.DESCRIPTION
        )
        module.command_registered = handle is not None
    except Exception as exc:
        # An older Hermes without in-session commands. Everything else works.
        logger.warning("hermie: /%s unavailable (%s)", me_command.COMMAND, exc)
    ctx.register_hook("pre_llm_call", module.on_pre_llm_call)
    return module
