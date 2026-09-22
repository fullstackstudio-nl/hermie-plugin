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
plainly does know who logged in. Two places can still say so, and both paths use
them in turn: the gateway's own table of live sessions, which stamps a login on
the record a turn runs on and sits in this very process (`live_session.py`), and
failing that `HERMES_SESSION_USER_ID`, when the gateway binds a login into the
session variables. Either is an identity a frozen section can reach, which
`session_info` is not.

**That order is a correction and it is worth the sentence.** The session
variables are bound when a session is created and are never rebound, so they
name whoever STARTED the chat — and a Bot Chat is shared, so on any turn after
the first that may be somebody who left hours ago. The live session record is
the one the turn runs on. Asking it first is what makes "who is sending this
turn" a question about the turn rather than about the session's history.

So the module uses both, and reads the app's metadata as little as possible: the
frozen section carries whoever the session started under, and `pre_llm_call`
contributes text only where that section cannot cover the turn — somebody else is
sending it, what it says has changed underneath them, or there is no frozen
section at all because the chat is older than the plugin or because the section
resolved nobody when it was built. On the single-user gateway that is every
Hermie install today, the last of those fires once on a chat that predates the
install and the others never fire, so the per-turn cost settles back to zero.

One limit is real and is written down in DESIGN.md rather than papered over: an
ungated gateway names nobody anywhere — no `auth_user_id`, nothing bound in the
session variables — so only the default applies.

The other thing a frozen section cannot do is change, and core will not render
one twice. The per-turn path carries that instead, in two shapes: `top_up` says
what a chat's copy no longer says — because the person edited their profile, or
because the person typing is no longer the one it describes — and `introduce`
says all of it once to a session that never had a copy at all, which is the chat
that was already open when the plugin arrived and the chat whose section resolved
nobody when its prompt was built.

What the text says is `render.py`'s business, including the paragraph that tells
a bot where any of this came from and where to look for more. This module
decides only which parts of that paragraph this gateway can stand behind, in
`orientation` below.

`pre_llm_call` does one more thing, in `session_vars.py`: when Hermes' own
`HERMES_SESSION_USER_*` variables do not name the person sending this turn, it
puts them right for the call — filled in when they are empty, and rewritten when
they name somebody else, which on a shared chat is the login that opened it. A
tool that reads them then sees the person, and Hermes' own "User:" line stops
contradicting what the context section says. It is off with
`context.session_vars: false` and it is a no-op on any gateway that fills them in
itself.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .. import contract
from .render import (
    BY_CLAIM,
    BY_HOOK,
    BY_LIVE_SESSION,
    BY_SESSION_VARS,
    INTRODUCED,
    RETRACTED,
    RETRACTED_BY_SENDER,
    RETRACTED_BY_SENDER_IN_CHAT,
    RETRACTED_IN_CHAT,
    SUPERSEDES,
    SUPERSEDES_BY_SENDER,
    SUPERSEDES_BY_SENDER_IN_CHAT,
    SUPERSEDES_IN_CHAT,
    PROVIDER_PREFIX,
    ContextSection,
    Orientation,
    read_sections,
    render,
    resolve,
    same_user,
)
from . import me as me_command
from .live_session import LiveSessions, dashboard_providers
from .session_vars import (
    SESSION_ID,
    SESSION_KEY,
    SESSION_NAMES,
    UI_SESSION_ID,
    USER_ID,
    USER_ID_ALT,
    USER_NAME,
    SessionVars,
)
from . import turn_claim
from .turn_claim import TurnClaims

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

    `introduced` says the text never reached the system prompt at all: it was
    said in the chat, either by `introduce` on a session whose prompt was built
    without a section or by a top-up on a session whose section rendered empty.
    The record is otherwise identical, and the flag is only read to word the next
    note correctly — "it replaces what the system prompt says" is a sentence that
    must not be said about a prompt that says nothing.

    `sender` is who this record was last worked out FOR, which is not always the
    person it describes: a sender the app has no row for describes nobody, and
    remembering only the nobody would have every following turn resolve that same
    stranger again from scratch. It is what makes "this sender is already
    answered for" a question the module can ask without a file read.
    """

    user_id: str
    text: str
    stamp: Tuple[int, int]
    introduced: bool = False
    sender: str = ""


class ContextModule:
    def __init__(
        self,
        runtime,
        session_vars: Optional[SessionVars] = None,
        live_sessions: Optional[LiveSessions] = None,
        claims: Optional[TurnClaims] = None,
        auth_providers: Optional[Callable[[], Tuple[str, ...]]] = None,
    ):
        self.runtime = runtime
        self.session_vars = session_vars if session_vars is not None else SessionVars()
        self.live_sessions = live_sessions if live_sessions is not None else LiveSessions()
        # Where the dashboard route leaves "the next turn is mine". Shared with
        # the route's own copy of this package; see `turn_claim.py`.
        self.claims = claims if claims is not None else turn_claim.shared()
        # The dashboard's sign-in provider names, asked per use: a claim only
        # ever stands in for a sender spelled as a dashboard login.
        self.auth_providers = auth_providers if auth_providers is not None else dashboard_providers
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

    @property
    def memory_readable(self) -> bool:
        """Whether this gateway's memory browser will actually answer.

        Both switches, read the way `memory/__init__.py` reads them. It gates
        one sentence of the orientation, by the rule a capability follows: a bot
        is pointed at the memory browser only where there is one.
        """
        return (
            self.runtime.config("modules.memory", True) is not False
            and self.runtime.config("memory.browse", True) is not False
        )

    @property
    def orientation(self) -> Orientation:
        """Which orientation sentences this gateway can stand behind.

        Every path that renders passes this, so the paragraph is written once
        and a bot reads the same one however the text reached it.
        """
        return Orientation(command=self.command_registered, memory=self.memory_readable)

    def session_sender(self) -> str:
        """The login the gateway bound into the session variables, or nothing.

        The last source of a sender, and the oldest: these are bound when the
        session is created and are never rebound, so on a chat several people
        share they name the one who started it. It is a read, not a write, so
        `context.session_vars` does not gate it: that switch is about filling the
        variables in, and a gateway that has asked not to be written to can still
        be asked who it let in.
        """
        return self.session_vars.read(USER_ID)

    def live_sender(self, session_id: str = "") -> str:
        """The login the gateway admitted this session under, or nothing.

        The only source that works on a dashboard gateway nobody has patched:
        the id of the turn goes to the gateway's own table of live sessions,
        which stamps a login on the record it runs the turn from. That record is
        younger than the session variables — a resumed or reopened chat gets a
        new one — so it is asked before them. The id may arrive as the hook's
        `session_id` or in Hermes' own session variables, and the table can be
        keyed by either shape, so everything that might name this session is
        offered.
        """
        return self.live_sessions.login(
            session_id, *(self.session_vars.read(name) for name in SESSION_NAMES)
        )

    def turn_ids(self, session_id: str = "") -> Tuple[str, Tuple[str, ...]]:
        """The runtime id bound for this turn, and the durable ids it carries.

        A claim is matched by the runtime id when one is bound, and only when
        none is by the durable ids — the hook's own `session_id`, the session
        id and the session key — against the aliases the route recorded off
        the live record. See `turn_claim.py`.
        """
        runtime_id = self.session_vars.read(UI_SESSION_ID)
        durable = (session_id, self.session_vars.read(SESSION_ID), self.session_vars.read(SESSION_KEY))
        return runtime_id, durable

    def claim_may_stand_in_for(self, named: str, claimed: str, session_id: str = "") -> bool:
        """Whether a claim may replace the sender the hook was handed.

        A claim exists to correct one thing: the dashboard naming the login that
        OPENED a session as the sender of every turn. So it stands in for no
        sender at all, and for a sender spelled as a dashboard login — a
        provider prefix the dashboard signs people in with, the claimer's own
        or the opener's. A sender spelled any other way is somebody the
        dashboard did not admit — a messaging platform's user, a bot handing a
        turn to another — and Hermes named them correctly; a claim made from a
        browser is not evidence against that.
        """
        if not named:
            return True
        match = PROVIDER_PREFIX.match(named)
        if match is None:
            return False
        providers = set(self.auth_providers() or ())
        for login in (claimed, self.live_sender(session_id)):
            found = PROVIDER_PREFIX.match(login or "")
            if found is not None:
                providers.add(found.group(1))
        return match.group(1) in providers

    def claimed_sender(self, named: str = "", session_id: str = "", *, take: bool = False) -> str:
        """The person who claimed this turn from the app, or nothing.

        `take` spends the claim, and only the turn itself does that. Building
        the prompt and `/me` look without spending, so the turn that follows
        still finds it. A claim that may not stand in for the hook's sender is
        left where it is, unspent: the turn it was made for has not run yet.
        """
        runtime_id, durable = self.turn_ids(session_id)
        claimed = self.claims.peek(runtime_id, durable)
        if not claimed or not self.claim_may_stand_in_for(named, claimed, session_id):
            return ""
        if not take:
            return claimed
        taken = self.claims.take(runtime_id, durable)
        # Another claim may have landed between the look and the take; it is
        # the newer one, and it passes the same test or is not used.
        if taken and (taken == claimed or self.claim_may_stand_in_for(named, taken, session_id)):
            return taken
        return ""

    def discard_claim(self, session_id: str = "") -> None:
        """Spend this session's claim without using it.

        For work the plugin answers itself without a model turn, `/me` first
        among it. A claim sent before a command is a claim no turn will spend,
        and left alone it would be spent by the next turn from a client that
        does not claim at all.
        """
        runtime_id, durable = self.turn_ids(session_id)
        self.claims.take(runtime_id, durable)

    def sender_with_source(
        self, named: str = "", session_id: str = "", *, take: bool = False
    ) -> Tuple[str, str]:
        """Who is asking, and which of the four rungs answered.

        This is the first half of the resolution order; `resolve` in
        `render.py` carries the rest (the configured default, the app's own,
        and the only registered person). None of them is reached while a rung
        above it answers.

        **The order is by how recent the answer is, not by what it costs**, and
        that is a correction rather than a preference. The session variables are
        bound once, when the session is created, and go on naming the login that
        created it for as long as it lives — which on a SHARED chat is whoever
        opened it, not whoever is typing now. The gateway's own session record
        is the one this turn runs on. So the record is asked first: a stale answer that is cheap is still the wrong person,
        and reading the record is a dict lookup in this very process rather than
        anything that touches a disk or a socket.

        **A claim comes before all three**, including a hook sender spelled as
        a dashboard login (`claim_may_stand_in_for`), for the same
        reason taken one step further: on a dashboard session the hook's sender
        IS the session's creator (`_user_id` is set once from `auth_user_id`
        when the agent is built), so on a shared chat it names the opener on
        every turn. A claim is the only thing that names the person who pressed
        send, because it was made by their own authenticated request moments
        before this turn was submitted.
        """
        claimed = self.claimed_sender(named, session_id, take=take)
        if claimed:
            return claimed, BY_CLAIM
        if named:
            return named, BY_HOOK
        found = self.live_sender(session_id)
        if found:
            return found, BY_LIVE_SESSION
        found = self.session_sender()
        if found:
            return found, BY_SESSION_VARS
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
        found = [
            contract.CAP_CONTEXT_PROMPT,
            contract.CAP_CONTEXT_LIVE,
            # Rendered by every path this module has, so it is claimed wherever
            # the module is on at all.
            contract.CAP_CONTEXT_ORIENTATION,
            # The store and the rung are part of this module, so wherever it is
            # on a claim is honoured. Whether the route that writes one is
            # reachable is the dashboard's business: an app that sees this and
            # gets a 404 is talking to a gateway whose dashboard did not mount
            # the plugin's routes, and goes on without a claim.
            contract.CAP_CONTEXT_TURN_CLAIM,
        ]
        if any(user.per_bot for user in self.section().users.values()):
            found.append(contract.CAP_CONTEXT_PER_BOT)
        if self.command_registered:
            found.append(contract.CAP_COMMAND_ME)
        return found

    # -- the command ---------------------------------------------------------

    def on_me_command(self, raw_args: str = "") -> Optional[str]:
        """`/me`, answered here rather than by the model. See `me.py`.

        It reads a claim like any other path and then spends it: no model turn
        follows a command, so the claim would otherwise wait for somebody
        else's.
        """
        try:
            return me_command.answer(self, raw_args)
        finally:
            try:
                self.discard_claim()
            except Exception as exc:
                logger.warning("hermie: could not spend the turn claim after /me: %s", exc)

    # -- the frozen section --------------------------------------------------

    def remember(
        self,
        session_id: str,
        user_id: str,
        text: str,
        stamp: Tuple[int, int],
        introduced: bool = False,
        sender: str = "",
    ) -> None:
        """Record what this session now knows about a person."""
        with self.lock:
            # Re-inserting rather than assigning moves this session to the back
            # of the queue, so the one evicted is the one nothing has touched
            # for longest rather than the one that merely started earliest.
            self.frozen.pop(session_id, None)
            self.frozen[session_id] = Frozen(
                user_id=user_id, text=text, stamp=stamp, introduced=introduced, sender=sender
            )
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
        from the moment the WebSocket was admitted.

        What this cannot be is the last word. It runs once, at session start,
        for whoever started the session — and a Bot Chat is shared, so the next
        turn may well come from somebody else. The person it resolved is
        remembered along with the sender it resolved for, and the per-turn path
        takes over from there.
        """
        try:
            bot = str(session_info.get("profile_name") or "") or self.runtime.bot_name()
            # The stamp is taken BEFORE the read, so an edit that lands between
            # the two is remembered as older than it is and is noticed on the
            # next turn. The other order would swallow it for the session.
            stamp = self.runtime.app_stamp()
            section = self.section()
            sender_id = self.sender(session_id=str(session_info.get("session_id") or ""))
            user = resolve(
                section,
                sender_id=sender_id,
                configured_default=self.configured_default,
            )
            text = render(user, bot=bot, max_chars=self.max_chars, orientation=self.orientation)
            if user is not None:
                self.remember(
                    str(session_info.get("session_id") or ""),
                    user.user_id,
                    text,
                    stamp,
                    sender=sender_id,
                )
            return text
        except Exception as exc:
            # A raising section callable costs core the whole plugin-section
            # block, so this one never raises.
            logger.warning("hermie: could not render the device context: %s", exc)
            return ""

    # -- telling Hermes who is asking ----------------------------------------

    def fill_session_vars(self, sender_id: str, section_of: Callable[[], ContextSection]) -> Dict[str, str]:
        """Say who is asking in `HERMES_SESSION_USER_*`, for this call.

        Whose turn it is comes from the same order the rest of the module
        uses: the sender the caller worked out — handed to the hook, read off
        the live session record, or bound into the variables — and then the
        resolved default. See `session_vars.py` for what the write can and
        cannot reach.

        Two cases, and the second is why this is not only a shim over empty
        variables. When nothing is bound, the trio is filled in. When something
        IS bound and it names a DIFFERENT person from the one sending this turn,
        it is rewritten: a value bound at session start names whoever opened the
        session, and on a chat several people share that is exactly the line
        Hermes puts in front of the model as "User:" — contradicting, out loud,
        the person the context section describes. A gateway that already names
        the sender is left alone, because then there is nothing to correct.

        `section_of` is a callable rather than a section because every gate
        above it is free and reading the section is a file read on the agent's
        own path. On a gateway that already names its user, this costs one
        `ContextVar` lookup a turn and touches no disk — the bound value is
        itself one of the rungs the sender came from, so agreement is the
        ordinary case and it is settled before anything is read.
        """
        if not self.fills_session_vars or not self.session_vars.available():
            return {}
        bound = self.session_vars.read(USER_ID)
        if bound and (not sender_id or same_user(bound, sender_id)):
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
        return self.session_vars.fill(
            {
                USER_ID: user_id,
                USER_ID_ALT: named.user_id_alt if named is not None else "",
                USER_NAME: named.display_name if named is not None else "",
            },
            # Only where something already bound names somebody else. Then the
            # name and the alternative id go too, empty included: they belong to
            # the person who is no longer typing.
            replace=bool(bound),
        )

    # -- the per-turn top-up -------------------------------------------------

    def covers(self, frozen: Frozen, sender_id: str) -> bool:
        """Whether what this chat already knows is about the person now typing.

        Three ways it can be. The turn names nobody, so the person the record
        describes is the only answer there is — the ungated single-user gateway,
        which is most installs. The turn names the person the record describes,
        in either spelling of the id. Or the turn names the same sender the
        record was last worked out for, which matters most when that sender
        resolved to NOBODY: without it, a login the app has no row for would be
        resolved again, out of the app's metadata, on every turn for the rest of
        the chat.
        """
        if not sender_id:
            return True
        return same_user(frozen.user_id, sender_id) or same_user(frozen.sender, sender_id)

    def top_up(
        self,
        session_id: str,
        frozen: Frozen,
        sender_id: str,
        section_of: Callable[[], ContextSection],
        changed_sender: bool = False,
    ) -> Optional[Dict[str, str]]:
        """Say what this chat's copy no longer says, in one of two situations.

        **The same person edited their profile.** The section in the system
        prompt was rendered once and core will not render it again until the
        session is rebuilt, so an edit made during a long Bot Chat would
        otherwise reach the bot on its *next* chat. This is the turn-shaped half
        of the same answer: the newer text rides the user message, saying which
        of the two copies wins. It is gated on the app's `profile.yaml` having
        actually moved, which is one `stat` — so on the overwhelmingly common
        turn, where nobody edited anything, it costs that one syscall and reads
        nothing.

        **Somebody else is sending the turn** (`changed_sender`). A Bot Chat is
        shared: whoever opened it is not whoever is typing now, and the copy the
        chat holds describes the person it was rendered for. There is no stamp
        that moves for this — the app's metadata is exactly as it was — so the
        gate is the sender having changed, and the note says the person has
        changed rather than that they have edited anything.

        Everything after that is one piece of work, because the four endings are
        the same four either way: nothing to say, a newer copy, a first copy, and
        a copy withdrawn.
        """
        stamp = self.runtime.app_stamp()
        if not changed_sender and stamp == frozen.stamp:
            return None

        user = resolve(section_of(), sender_id=sender_id, configured_default=self.configured_default)
        text = render(
            user, bot=self.runtime.bot_name(), max_chars=self.max_chars, orientation=self.orientation
        )
        # Whom the record now describes. On an edit a person who has stopped
        # resolving is kept — they are still the one in the chat, and the
        # retraction below is about them. On a change of sender nobody is kept:
        # the previous person is not who this turn came from, and saying they
        # are is how their notes reach the next reader.
        described = user.user_id if user is not None else ("" if changed_sender else frozen.user_id)
        if text == frozen.text:
            # Nothing new to say. On an edit this is the file having moved for
            # something else entirely — a push registration, a heartbeat,
            # another person's bag — and remembering the new stamp is what keeps
            # that from being looked at again on every turn from here on.
            self.remember(session_id, described or frozen.user_id, frozen.text, stamp, frozen.introduced, sender_id)
            return None

        # Anything said from here rides the user message, so the newest copy this
        # chat holds is one said IN the chat whatever the system prompt carries.
        # The next note has to point at that one: a model sent back to the
        # system prompt is sent to older words, or to none at all.
        self.remember(session_id, described, text, stamp, True if text else frozen.introduced, sender_id)
        if text:
            # "It replaces what you were told" is only true when something was.
            # A chat that was never told anything is being told for the first
            # time, and claiming otherwise points a model at nothing. Where the
            # older copy sits decides the rest of the sentence: one said in the
            # chat was never in the system prompt at all.
            return {"context": render(
                user,
                bot=self.runtime.bot_name(),
                max_chars=self.max_chars,
                lead=self.beaten(frozen, changed_sender),
                orientation=self.orientation,
            )}
        if not frozen.text:
            # Nothing was said and nothing is there now. Nothing to retract.
            return None
        # Either they emptied it, or the person typing is somebody the app knows
        # nothing about. The older copy cannot be taken back out of a prompt or
        # a transcript, so the only honest thing left is to say it no longer
        # holds — a bot that goes on using the name in it is addressing the
        # wrong person.
        if changed_sender:
            return {"context": RETRACTED_BY_SENDER_IN_CHAT if frozen.introduced else RETRACTED_BY_SENDER}
        return {"context": RETRACTED_IN_CHAT if frozen.introduced else RETRACTED}

    @staticmethod
    def beaten(frozen: Frozen, changed_sender: bool = False) -> str:
        """The line in front of a copy, saying what it relates to.

        Two questions decide it: whether the chat was ever told anything at all,
        and whether this copy is a correction of the same person or a different
        person entirely. Getting the second one wrong is the failure this exists
        for — "they changed this" said about somebody else invites a model to
        merge two people into one.
        """
        if not frozen.text:
            return INTRODUCED
        if changed_sender:
            return SUPERSEDES_BY_SENDER_IN_CHAT if frozen.introduced else SUPERSEDES_BY_SENDER
        return SUPERSEDES_IN_CHAT if frozen.introduced else SUPERSEDES

    # -- a chat that began before any of this --------------------------------

    def introduce(
        self, session_id: str, sender_id: str, section_of: Callable[[], ContextSection]
    ) -> Optional[Dict[str, str]]:
        """A session whose system prompt was built without this section at all.

        A chat started before the plugin was installed — or before it had a
        prompt section — carries nothing about the person, and core will not
        build that prompt again for the life of the session. `top_up` cannot
        help: it tops up a copy, and there is no copy. So the next turn of such
        a chat introduces the person instead, with the same framing as every
        other copy and a line saying it replaces nothing.

        The same thing happens where the section DID render but resolved nobody,
        because a section that resolved nobody leaves no record — which is what
        keeps this reachable on the session that most needs it: the shared chat
        whose prompt was built for a login the app has no row for.

        Once, and that is the whole difficulty. Whatever this returns rides the
        user message on the turn it fires, so a decision nothing remembers is a
        decision taken again on every turn for the rest of the chat. The record
        it leaves is the same one the frozen section leaves — same shape, same
        `FROZEN_SESSIONS` bound — and it is written even when nothing was said,
        so a gateway with nobody registered stops asking rather than resolving
        for ever. A session Hermes names no id for shares one record with every
        other such session, which is the bound the frozen record already has.
        """
        # Before the read, for the reason `render_section` gives.
        stamp = self.runtime.app_stamp()
        user = resolve(section_of(), sender_id=sender_id, configured_default=self.configured_default)
        text = render(
            user, bot=self.runtime.bot_name(), max_chars=self.max_chars, orientation=self.orientation
        )
        # The plain text is what this chat now knows, so a later top-up compares
        # like with like; the lead line belongs to this one turn only. The sender
        # goes in beside it: an introduction that said nothing because the app
        # has no row for this login must not be worked out again next turn, and
        # must not stand in the way of the NEXT person to type here.
        self.remember(
            session_id, user.user_id if user is not None else "", text, stamp, True, sender_id
        )
        if not text:
            return None
        return {"context": render(
            user,
            bot=self.runtime.bot_name(),
            max_chars=self.max_chars,
            lead=INTRODUCED,
            orientation=self.orientation,
        )}

    def on_pre_llm_call(self, **kwargs: Any) -> Optional[Dict[str, str]]:
        """Contribute context the chat's own copy does not already carry.

        Three ways it can fail to: there is no copy at all, because this chat
        began before there was one or because the section resolved nobody when it
        was built; the person sending this turn is not the one the copy
        describes; or they are that person and have since changed what it says.

        Who "the person sending this turn" is gets worked out here, per turn,
        rather than being taken from the session. A shared chat's turns come
        from different people, and the one thing that must never happen is a bot
        being handed one person's profile while another is typing.
        """
        try:
            session_id = str(kwargs.get("session_id") or "")
            # The claim, if there is one, is spent here: one claim, one turn.
            sender_id, _source = self.sender_with_source(
                str(kwargs.get("sender_id") or ""), session_id, take=True
            )
            # One read of the app's metadata per turn at most, shared by the
            # shim and the top-up, and skipped entirely when neither needs it.
            cache: List[ContextSection] = []

            def section_of() -> ContextSection:
                if not cache:
                    cache.append(self.section())
                return cache[0]

            self.fill_session_vars(sender_id, section_of)

            frozen = self.frozen_section(session_id)
            if frozen is None:
                # Nothing was ever frozen for this session and nothing ever
                # will be. See `introduce`: this is the long-running chat that
                # predates the plugin, and it fires once.
                return self.introduce(session_id, sender_id, section_of)

            # Both endings are the same work, told apart by what has changed:
            # the app's metadata under the person this chat already knows, or the
            # person themselves. See `covers` for what "already knows" includes,
            # and `top_up` for why one function answers both.
            return self.top_up(
                session_id,
                frozen,
                sender_id,
                section_of,
                changed_sender=not self.covers(frozen, sender_id),
            )
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
