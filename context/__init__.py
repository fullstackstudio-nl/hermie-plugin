"""The context module: who the bot is talking to, worked out once a turn.

Until HERM-119 this module also rendered a `context` section — a person's
name, device, timezone, locale and their own free text — into a bot's system
prompt, from a bag the app wrote under its own `ui_meta` key. That is gone: the
app no longer sends it, on any build, and this module no longer reads it. Most
of what it said was duplicated anyway — since the Hermes fork this plugin runs
against, the gateway itself binds the signed-in person to every message, and
Hermes puts the time zone in the session on its own. What is genuinely lost is
the device (phone or Mac) and the app's own language, which a bot is simply no
longer told.

What is left is `pre_llm_call`, and it is narrower now: work out who the
gateway thinks is sending THIS turn, and — only where a future rung earns it
(`render.VERIFIED_RUNGS` is empty today) — say so on the message, as a fact the
gateway is putting on record rather than a profile the person wrote.

`pre_llm_call` knows exactly who is asking — it is handed `sender_id`, which is
the agent's `_user_id`, which on a WebSocket session is the `auth_user_id`
stamped from the login. On the dashboard's WebSocket route `_user_id` is often
empty while the gateway plainly does know who logged in. Two places can still
say so, and both are asked in turn: the gateway's own table of live sessions,
which stamps a login on the record a turn runs on and sits in this very process
(`live_session.py`), and failing that `HERMES_SESSION_USER_ID`, when the
gateway binds a login into the session variables.

**That order is a correction and it is worth the sentence.** The session
variables are bound when a session is created and are never rebound, so they
name whoever STARTED the chat — and a Bot Chat is shared, so on any turn after
the first that may be somebody who left hours ago. The live session record is
the one the turn runs on. Asking it first is what makes "who is sending this
turn" a question about the turn rather than about the session's history.

One limit is real and is written down in DESIGN.md rather than papered over: an
ungated gateway names nobody anywhere — no `auth_user_id`, nothing bound in the
session variables — so nothing here can name a sender at all.

`pre_llm_call` does one more thing, in `session_vars.py`: when Hermes' own
`HERMES_SESSION_USER_*` variables do not name the person sending this turn, it
puts them right for the call — filled in when they are empty, and rewritten
when they name somebody else, which on a shared chat is the login that opened
it. A tool that reads them then sees the person. It is off with
`context.session_vars: false` and it is a no-op on any gateway that fills them
in itself.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import contract
from .render import (
    BY_CLAIM,
    BY_HOOK,
    BY_LIVE_SESSION,
    BY_PLATFORM,
    BY_SESSION_VARS,
    PROVIDER_PREFIX,
    VERIFIED_RUNGS,
    same_user,
    sender_sentence,
    split_provider,
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


def _said_as(runtime_id: str) -> str:
    """A runtime session id as a log may name it: short, stable, not the id.

    A runtime id is a bearer token in one direction — anybody who learns one can
    aim a turn claim at that session — so it does not go in a log that a wider
    set of people read than can reach the session. A digest is as good for
    following one session through a log and is no use for claiming it.
    """
    if not runtime_id:
        return "no runtime id"
    return "#" + hashlib.sha256(runtime_id.encode("utf-8", "replace")).hexdigest()[:8]


def _why_refused(named: str, runtime_id: str) -> str:
    """Why `stand_in_test` would not let a claim stand in, in a few words.

    Said for the log and shaped by what a person can act on. The two real
    causes are a turn that is not a dashboard turn at all — nothing named a
    sender and no runtime id is bound, so nobody claimed it — and a sender
    spelled by a provider the dashboard does not sign people in with, which is
    a messaging platform's user or a bot and is a sender Hermes named
    correctly. Only the provider half of the login is named; see `log_claim`.
    """
    if not named:
        return "no sender was named and no runtime id is bound" if not runtime_id else "the turn declined it"
    prefix = split_provider(named)[0]
    return f"the sender's provider is {prefix}" if prefix else "the sender carries no provider"


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
        # Set at registration: a capability is advertised once the thing it
        # names is actually there, never because the code for it shipped.
        self.command_registered = False

    @property
    def fills_session_vars(self) -> bool:
        return self.runtime.config("context.session_vars", True) is not False

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

    def login_providers(self, session_id: str = "") -> set:
        """The prefixes a login THIS DASHBOARD admits people under, on this gateway.

        Hermes' registry of interactive sign-in providers, plus whatever the
        live record for this session was admitted under — a gateway can be
        serving a provider the registry has not been asked about yet, and the
        record is proof that it does.

        Two questions are settled with this set and both fail safe on an empty
        one, in opposite directions, because they are opposite questions. A
        claim may stand in only for a sender this set recognises, so an empty
        set lets no claim through. A hook sender counts as verified only when
        this set does NOT recognise it, so an empty set verifies nothing. A
        gateway that cannot say which logins are its own is a gateway that gets
        the cautious answer to both.
        """
        providers = set(self.auth_providers() or ())
        found = PROVIDER_PREFIX.match(self.live_sender(session_id) or "")
        if found is not None:
            providers.add(found.group(1))
        return providers

    def platform_sender(self, named: str, session_id: str = "") -> bool:
        """Whether the hook's sender is one the dashboard did NOT admit.

        This is the whole of what makes a hook sender worth asserting. Hermes
        hands `pre_llm_call` the agent's `_user_id`, which on a dashboard
        session is the `auth_user_id` stamped when the agent was BUILT — so on
        a shared chat it names whoever opened it, on every turn, whoever typed
        it (DESIGN.md, "A shared chat names its opener on every turn"). A
        sender spelled any other way did not come from there: a messaging
        platform names the user who sent each message, and a bot handing a turn
        to another names itself. `stand_in_test` already draws this line from
        the other side — it is the sender a claim may not override, because
        Hermes named it correctly.

        Three ways to be unsure and all three answer "no", because "no" is the
        answer that asserts nothing: a sender with no provider at all, a
        provider this dashboard signs people in with, and a gateway that cannot
        say which providers those are.
        """
        match = PROVIDER_PREFIX.match(named or "")
        if match is None:
            return False
        providers = self.login_providers(session_id)
        return bool(providers) and match.group(1) not in providers

    def stand_in_test(self, named: str, runtime_id: str, session_id: str = "") -> Callable[[str], bool]:
        """Whether a claim may replace the sender the hook was handed.

        A claim exists to correct one thing: the dashboard naming the login that
        OPENED a session as the sender of every turn. So it stands in for a
        sender spelled as a dashboard login — a provider prefix the dashboard
        signs people in with, the claimer's own or the opener's. A sender
        spelled any other way is somebody the dashboard did not admit — a
        messaging platform's user, a bot handing a turn to another — and Hermes
        named them correctly; a claim made from a browser is not evidence
        against that.

        It stands in for NO sender only on a turn with a runtime id bound, which
        is a dashboard turn. A turn with neither a sender nor a runtime id — a
        scheduled job, background work matched only through the aliases — is
        not the turn anybody claimed.

        Everything that reaches outside the store is worked out here, before
        the store's lock is taken; the test it returns is pure.
        """
        if not named:
            return lambda claimed: bool(runtime_id)
        match = PROVIDER_PREFIX.match(named)
        if match is None:
            return lambda claimed: False
        prefix = match.group(1)
        providers = self.login_providers(session_id)

        def accept(claimed: str) -> bool:
            own = PROVIDER_PREFIX.match(claimed or "")
            return prefix in providers or (own is not None and own.group(1) == prefix)

        return accept

    def claimed_sender(self, named: str = "", session_id: str = "", *, take: bool = False) -> str:
        """The person who claimed this turn from the app, or nothing.

        `take` spends the claim, and only the turn itself does that
        (`on_pre_llm_call`, and `/me`'s best-effort discard). Judging and
        spending are one step in the store (`take_if`), so the claim spent is
        always the claim that was judged.
        """
        runtime_id, durable = self.turn_ids(session_id)
        accept = self.stand_in_test(named, runtime_id, session_id)
        if not take:
            claimed = self.claims.peek(runtime_id, durable)
            return claimed if claimed and accept(claimed) else ""

        # The store calls the test only where it found a claim for this turn,
        # and under its own lock, so recording that here tells the three
        # outcomes apart — spent, refused, none — without asking the store a
        # second question it cannot answer atomically anyway. The test itself is
        # untouched; this only watches it, which is why the store's shape does
        # not change and the two copies of the plugin go on sharing one.
        found: List[str] = []

        def watched(claimed: str) -> bool:
            found.append(claimed)
            return accept(claimed)

        taken = self.claims.take_if(runtime_id, durable, watched)
        self.log_claim(taken, bool(found), runtime_id, named)
        return taken

    @staticmethod
    def log_claim(taken: str, found: bool, runtime_id: str, named: str) -> None:
        """What became of this turn's claim. Every line starts `hermie: turn claim`.

        Nothing said whether a claim was spent, refused or never found, so a
        gateway where the feature had quietly stopped working looked exactly
        like one where nobody had claimed anything — which is the worst shape a
        fault can take in a feature whose job is to be invisible.

        Three outcomes, two levels, chosen by how often each one happens on a
        gateway nobody is debugging. **Spent** and **refused** are both rare:
        one claim is made per turn by one client, and a refusal means a claim
        was made for a turn it may not stand in for, which somebody wants to
        see. Both are `info`. **No claim at all** is the ordinary turn on every
        gateway whose app does not claim — which is every turn that does not
        come from Hermie — so it is `debug` and stays out of a busy log.

        Ids the gateway minted, and nothing else. The session this turn runs on,
        and the provider half of a login (`oidc`, `basic`); never the user half,
        never a name, and nothing from the message — a log line is read by
        people who are not the person who typed.
        """
        where = _said_as(runtime_id)
        if taken:
            logger.info(
                "hermie: turn claim spent for session %s (provider %s)",
                where,
                split_provider(taken)[0] or "none",
            )
            return
        if found:
            logger.info(
                "hermie: turn claim refused for session %s; it may not stand in for this turn (%s)",
                where,
                _why_refused(named, runtime_id),
            )
            return
        logger.debug("hermie: turn claim absent for session %s", where)

    def discard_claim(self, session_id: str = "") -> None:
        """Spend this session's claim without using it, where it can be found.

        For work the plugin answers itself without a model turn, `/me` first
        among it. Best effort, and knowingly so: Hermes runs a plugin command
        on its RPC pool without binding the session variables, so on the
        dashboard today there is no runtime id to find the claim by and this
        spends nothing. The guarantee is the app's — it never claims for a
        slash command (DESIGN.md) — and this only helps on a gateway that binds
        the session for a command.
        """
        runtime_id, durable = self.turn_ids(session_id)
        if self.claims.take(runtime_id, durable):
            logger.info(
                "hermie: turn claim discarded for session %s; the plugin answered without a model turn",
                _said_as(runtime_id),
            )

    def sender_with_source(
        self,
        named: str = "",
        session_id: str = "",
        *,
        take: bool = False,
    ) -> Tuple[str, str]:
        """Who is asking, and which rung answered.

        **The order is by how recent the answer is, not by what it costs**, and
        that is a correction rather than a preference. The session variables are
        bound once, when the session is created, and go on naming the login that
        created it for as long as it lives — which on a SHARED chat is whoever
        opened it, not whoever is typing now. The gateway's own session record
        is the one this turn runs on. So the record is asked first: a stale
        answer that is cheap is still the wrong person, and reading the record
        is a dict lookup in this very process rather than anything that touches
        a disk or a socket.

        **A claim comes before all three**, including a hook sender spelled as a
        dashboard login (`stand_in_test`): on a dashboard session the hook's
        sender IS the session's creator (`_user_id` is set once from
        `auth_user_id` when the agent is built), so on a shared chat it names
        the opener on every turn, and a claim is made by an authenticated
        request closer to the turn than any of that.
        """
        claimed = self.claimed_sender(named, session_id, take=take)
        if claimed:
            return claimed, BY_CLAIM
        if named:
            # Two rungs, one lookup: the same sender, told apart by whether the
            # dashboard is where it came from. Only the platform half answers
            # "who sent THIS turn" — see `platform_sender`.
            return named, (BY_PLATFORM if self.platform_sender(named, session_id) else BY_HOOK)
        found = self.live_sender(session_id)
        if found:
            return found, BY_LIVE_SESSION
        found = self.session_sender()
        if found:
            return found, BY_SESSION_VARS
        return "", ""

    def capabilities(self) -> list:
        found = [
            # The store and the rung are part of this module, so wherever it is
            # on a claim is honoured. Whether the route that writes one is
            # reachable is the dashboard's business: an app that sees this and
            # gets a 404 is talking to a gateway whose dashboard did not mount
            # the plugin's routes, and goes on without a claim.
            contract.CAP_CONTEXT_TURN_CLAIM,
        ]
        if self.command_registered:
            found.append(contract.CAP_COMMAND_ME)
        return found

    # -- the command ---------------------------------------------------------

    def on_me_command(self, raw_args: str = "") -> Optional[str]:
        """`/me`, answered here rather than by the model. See `me.py`.

        It tries to spend a claim afterwards, because no model turn follows a
        command. That is best effort and on today's gateway it finds nothing:
        Hermes calls a plugin command with no session variables bound, so there
        is no runtime id to find a claim by. The app never claiming for a slash
        command is what actually holds.
        """
        try:
            return me_command.answer(self, raw_args)
        finally:
            try:
                self.discard_claim()
            except Exception as exc:
                logger.warning("hermie: could not spend the turn claim after /me: %s", exc)

    # -- telling Hermes who is asking ----------------------------------------

    def fill_session_vars(self, sender_id: str) -> Dict[str, str]:
        """Say who is asking in `HERMES_SESSION_USER_*`, for this call.

        Whose turn it is comes from the same order the rest of the module
        uses: the sender the caller worked out — handed to the hook, read off
        the live session record, or bound into the variables. See
        `session_vars.py` for what the write can and cannot reach.

        Two cases. When nothing is bound, `USER_ID` is filled in. When
        something IS bound and it names a DIFFERENT person from the one
        sending this turn, the whole trio is rewritten: a value bound at
        session start names whoever opened the session, and on a chat several
        people share that is exactly the line Hermes puts in front of the
        model as "User:" — contradicting, out loud, the person actually
        typing. A gateway that already names the sender is left alone, because
        then there is nothing to correct.

        `USER_ID_ALT` and `USER_NAME` are never filled with anything any more
        — there is no profile left to read either from — so a rewrite blanks
        them rather than leaving a name that belonged to whoever opened the
        session standing once somebody else is confirmed to be typing.
        """
        if not sender_id or not self.fills_session_vars or not self.session_vars.available():
            return {}
        bound = self.session_vars.read(USER_ID)
        if bound and same_user(bound, sender_id):
            return {}
        return self.session_vars.fill(
            {USER_ID: sender_id, USER_ID_ALT: "", USER_NAME: ""},
            replace=bool(bound),
        )

    @staticmethod
    def asserted_sender(rung: str, sender_id: str) -> str:
        """The one sentence that says who sent THIS turn, or "" — today, always "".

        `VERIFIED_RUNGS` (`render.py`) is empty: the two rungs once treated as
        answering "who sent this turn" — a claim bound to a session rather than
        to the submit it was made for, and a hook sender trusted on a
        registry-name convention nothing in Hermes actually pins — did not prove
        it, and both assertions are withdrawn until a claim is bound to
        `sha256` of the exact prompt text (DESIGN.md, "Decision (2026-09-22)").
        Never on a rung that names the opener of the session, which is every
        rung there is right now.
        """
        return sender_sentence(sender_id) if rung in VERIFIED_RUNGS else ""

    def on_pre_llm_call(self, **kwargs: Any) -> Optional[Dict[str, str]]:
        """Say who sent this turn, only where a rung has earned the right to.

        Three ways it can fail to say anything: no rung answered at all, the
        rung that did is not one this build treats as confirmed, or Hermes'
        own `HERMES_SESSION_USER_*` variables already carry it and there is
        nothing left over for `fill_session_vars` to correct.

        Who "the person sending this turn" is gets worked out here, per turn,
        rather than being taken from the session. A shared chat's turns come
        from different people, and the one thing that must never happen is a
        bot being told a stale sender is confirmed.
        """
        try:
            session_id = str(kwargs.get("session_id") or "")
            # The claim, if there is one, is spent here: one claim, one turn.
            sender_id, source = self.sender_with_source(
                str(kwargs.get("sender_id") or ""), session_id, take=True
            )
            self.fill_session_vars(sender_id)
            # Settled before anything else: whether the gateway checked who is
            # sending is true or false whatever else this turn has to say.
            asserted = self.asserted_sender(source, sender_id)
            return {"context": asserted} if asserted else None
        except Exception as exc:
            logger.warning("hermie: could not add per-sender context: %s", exc)
            return None


def register(ctx, runtime) -> ContextModule:
    module = ContextModule(runtime)
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
