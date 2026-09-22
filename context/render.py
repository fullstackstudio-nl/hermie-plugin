"""Reading the device-context section, and turning it into prompt text.

The app writes this under its own ui_meta key, alongside the push registrations,
in a `context` section. There is one key per person now — `hermie-app:<user id>`
— and the older shared `hermie-app` is still read for one version:

    {"context": {"v": 1,
                 "default": "<user id>",
                 "users": {"<user id>": {"displayName": "...",
                                         "userIdAlt": "...",
                                         "about": "...",
                                         "device": {"model": "...", "os": "...", "appVersion": "..."},
                                         "timezone": "Europe/Amsterdam",
                                         "locale": "nl-NL",
                                         "perBot": {"<bot>": "..."},
                                         "updatedAt": 1789957143}}}}

`v` is checked, not assumed, and an entry whose version this build does not know
is dropped rather than guessed at — the same rule the registrations follow, for
the same reason: what reaches here came off a gateway as a bag of JSON.

The ids on the two sides of this are not spelled the same. A gateway login
carries the provider that issued it — `self-hosted:<uuid>`, `oidc:<sub>`,
`basic:<name>` — while the app registers a person under the bare id
`/api/auth/me` hands back. `same_user` below is the one place that knows both
forms name one person.

Everything rendered is bounded. A system prompt section is prompt bytes charged
on every turn of the session it was frozen into, so a user who pastes an essay
into "about me" gets it truncated rather than getting a slower bot forever.

What is rendered also says where it came from. `Orientation` below is the short
paragraph that tells a bot it is reading the person's own Hermie profile, what it
may do with that, and where to look for the rest — without it, somebody has to
sit and explain the plugin to their bot before the feature works at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

SECTION_VERSION = 1

# The provider prefix on a gateway login, and the whole subtlety of reading it:
# an OIDC subject may itself be a URL, and the colon in
# `https://accounts.example.com/12345` is a scheme, not a provider. Requiring
# that what follows is not `//` is what keeps such a subject whole — including
# when it arrives prefixed, as `oidc:https://…`, where the first colon really
# is the provider and the second really is not.
PROVIDER_PREFIX = re.compile(r"^([A-Za-z][A-Za-z0-9._-]*):(?!//)(.+)$")

# Every rung of the resolution order, named once. The first three are where a
# sender can come from, in the order they are asked, and belong to the module
# that asks; the rest are what `resolve` falls back to. They are here together because `/me` answers with
# them and a person reading that answer should be reading one vocabulary.
BY_HOOK = "hook sender"
BY_SESSION_VARS = "session variables"
BY_LIVE_SESSION = "live session record"
BY_CONFIGURED = "configured default"
BY_APP_DEFAULT = "app default"
BY_ONLY_USER = "only registered person"
BY_NOBODY = "nobody"

SENDER_RUNGS = (BY_HOOK, BY_LIVE_SESSION, BY_SESSION_VARS)

# Per-field caps, applied before the whole-section cap, so one long field cannot
# crowd out the short ones that identify the person.
LIMITS = {
    "displayName": 80,
    "userIdAlt": 128,
    "about": 600,
    "model": 60,
    "os": 40,
    "appVersion": 30,
    "timezone": 60,
    "locale": 20,
    "perBot": 400,
}


@dataclass(frozen=True)
class UserContext:
    user_id: str
    display_name: str = ""
    # A second id the same person is known by, when the app knows one. Only
    # used to fill in Hermes' `HERMES_SESSION_USER_ID_ALT`; never rendered.
    user_id_alt: str = ""
    about: str = ""
    device_model: str = ""
    device_os: str = ""
    app_version: str = ""
    timezone: str = ""
    locale: str = ""
    per_bot: Dict[str, str] = field(default_factory=dict)
    updated_at: int = 0


@dataclass(frozen=True)
class ContextSection:
    users: Dict[str, UserContext] = field(default_factory=dict)
    default_user: str = ""
    # user id -> whose ui_meta key that entry was read out of (`""` for the
    # legacy shared bag). Only `read_sections` can know this, and only `/me`
    # asks; resolution never looks at it.
    origins: Dict[str, str] = field(default_factory=dict)


def split_provider(user_id: str) -> Tuple[str, str]:
    """``("oidc", "<sub>")`` for a prefixed id, ``("", the id)`` for a bare one."""
    match = PROVIDER_PREFIX.match(user_id or "")
    return (match.group(1), match.group(2)) if match else ("", user_id or "")


def same_user(one: str, other: str) -> bool:
    """Whether two ids name one person, across the provider prefix.

    Equal ids are one person. Otherwise exactly one of the two may carry a
    prefix and the bare halves must match: `self-hosted:ef11…` is `ef11…`, and
    `max` is `basic:max`.

    Two *different* prefixes are two different logins and never match, however
    alike the bare halves look. `oidc:max` and `basic:max` are as likely to be
    two people as one, and this module would rather name nobody than the wrong
    person — the same rule that makes `resolve` give up on a tie.
    """
    if not one or not other:
        return False
    if one == other:
        return True
    one_prefix, one_bare = split_provider(one)
    other_prefix, other_bare = split_provider(other)
    if bool(one_prefix) == bool(other_prefix):
        return False
    return one_bare == other_bare


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit] if isinstance(value, (str, int, float)) else ""


def _number(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def user_of(user_id: str, value: Any) -> Optional[UserContext]:
    if not user_id or not isinstance(value, dict):
        return None
    device = value.get("device") if isinstance(value.get("device"), dict) else {}
    raw_per_bot = value.get("perBot") if isinstance(value.get("perBot"), dict) else {}
    return UserContext(
        user_id=user_id,
        display_name=_text(value.get("displayName"), LIMITS["displayName"]),
        user_id_alt=_text(value.get("userIdAlt"), LIMITS["userIdAlt"]),
        about=_text(value.get("about"), LIMITS["about"]),
        device_model=_text(device.get("model"), LIMITS["model"]),
        device_os=_text(device.get("os"), LIMITS["os"]),
        app_version=_text(device.get("appVersion"), LIMITS["appVersion"]),
        timezone=_text(value.get("timezone"), LIMITS["timezone"]),
        locale=_text(value.get("locale"), LIMITS["locale"]),
        per_bot={
            str(bot): _text(text, LIMITS["perBot"])
            for bot, text in raw_per_bot.items()
            if _text(text, LIMITS["perBot"])
        },
        updated_at=_number(value.get("updatedAt")),
    )


def read_section(app_key_value: Any) -> ContextSection:
    """The whole ``context`` section out of one app-owned bag."""
    if not isinstance(app_key_value, dict):
        return ContextSection()
    section = app_key_value.get("context")
    if not isinstance(section, dict):
        return ContextSection()
    if _number(section.get("v")) != SECTION_VERSION:
        return ContextSection()

    rows = section.get("users") if isinstance(section.get("users"), dict) else {}
    users = {
        str(user_id): parsed
        for user_id, value in rows.items()
        if (parsed := user_of(str(user_id), value)) is not None
    }
    return ContextSection(users=users, default_user=_text(section.get("default"), 128))


def read_sections(items: Iterable[Tuple[str, Any]]) -> ContextSection:
    """One view over every app-owned bag, as ``(user id, value)`` pairs.

    The pairs arrive in precedence order (legacy first) and a later one wins for
    the person it names. A per-user key that carries a *different* person's
    entry is still read — it costs nothing and an app mid-migration may well
    have copied a whole bag across — but it never beats that person's own key.

    The section-wide `default` is the legacy bag's, because a per-user bag can
    only sensibly name itself. When there is no legacy bag and the per-user ones
    agree on one name, that is used; when they disagree, nobody is the default
    and `resolve` falls through to "the only registered person".
    """
    users: Dict[str, UserContext] = {}
    owned: Dict[str, UserContext] = {}
    origins: Dict[str, str] = {}
    legacy_default = ""
    defaults = set()
    for user_id, value in items:
        section = read_section(value)
        users.update(section.users)
        origins.update({found: user_id for found in section.users})
        if user_id and user_id in section.users:
            owned[user_id] = section.users[user_id]
        if section.default_user:
            if user_id:
                defaults.add(section.default_user)
            else:
                legacy_default = section.default_user
    users.update(owned)
    origins.update({found: found for found in owned})
    default_user = legacy_default or (next(iter(defaults)) if len(defaults) == 1 else "")
    return ContextSection(users=users, default_user=default_user, origins=origins)


def match_sender(section: ContextSection, sender_id: str) -> Optional[UserContext]:
    """The registered person a sender names, in either form of the id.

    Only the sender is read this leniently. It is the one id that arrives from
    outside, spelled however the login spelled it; `context.default_user` and
    the app's own `default` are written by hand against the ids the app itself
    registers, so they are matched as written.

    A bare sender can in principle fit two prefixed entries — `basic:max` and
    `oidc:max` — and that is a tie, which names nobody.
    """
    if not sender_id:
        return None
    if sender_id in section.users:
        return section.users[sender_id]
    found = [user for user_id, user in section.users.items() if same_user(user_id, sender_id)]
    return found[0] if len(found) == 1 else None


def resolve_with_reason(
    section: ContextSection, *, sender_id: str = "", configured_default: str = ""
) -> Tuple[Optional[UserContext], str]:
    """`resolve`, and which rung answered — the one `/me` has to report."""
    sender = match_sender(section, sender_id)
    if sender is not None:
        return sender, BY_HOOK
    if configured_default and configured_default in section.users:
        return section.users[configured_default], BY_CONFIGURED
    if section.default_user and section.default_user in section.users:
        return section.users[section.default_user], BY_APP_DEFAULT
    # Only where the gateway named nobody at all. The two defaults above are
    # somebody's statement about who to assume — the operator's in the config,
    # the app's in the section — while this rung is a guess, and a guess must
    # not answer for a gateway that DID name somebody. A login the app has no
    # row for is a person the app has no row for, and handing them the only
    # registered person's notes is how one person's profile reaches another.
    if not sender_id and len(section.users) == 1:
        return next(iter(section.users.values())), BY_ONLY_USER
    return None, BY_NOBODY


def resolve(section: ContextSection, *, sender_id: str = "", configured_default: str = "") -> Optional[UserContext]:
    """Whose context to use.

    In order: the sender the gateway named, then the operator's configured
    default, then the app's own default, then — only where the gateway named
    nobody and exactly one person is registered — that person. With no way to
    tell who is asking, or with a sender the app has no row for and no default
    naming anyone, this returns nothing: showing a bot the wrong person's notes
    is worse than showing it none.

    The sender is matched by `match_sender`, so a login that carries its
    provider finds the person the app registered bare, and the other way round.
    """
    return resolve_with_reason(
        section, sender_id=sender_id, configured_default=configured_default
    )[0]


# -- saying which copy of this a bot should believe --------------------------
#
# A bot can end up holding two descriptions of one person: the one frozen into
# its system prompt when the session started, and a newer one riding a turn.
# Core cannot be asked to re-render a frozen section, so the newer copy has to
# say which of the two wins, or a model is left to guess between two profiles of
# one person and may well average them.
#
# Where the older copy sits decides the wording, and it matters: a section
# frozen into the prompt is in the prompt, while one introduced mid-chat was
# said in the chat. Pointing a model at the system prompt when nothing is there
# points it at nothing, which is the same failure the framing line avoids.

SUPERSEDES = (
    "The person has changed this since this chat began. "
    "It replaces what the system prompt says about them."
)

SUPERSEDES_IN_CHAT = (
    "The person has changed this since this chat began. "
    "It replaces what was said about them earlier in this chat."
)

# And when they emptied it. A frozen section cannot be taken back out of a
# system prompt — core renders it once and replays it verbatim — so the only
# thing left is to say out loud that it no longer holds. Somebody who deletes
# what they wrote about themselves has usually deleted it on purpose.
RETRACTED = (
    "The person has removed the background they had set about themselves. "
    "Disregard what the system prompt says about them; there is nothing there now."
)

RETRACTED_IN_CHAT = (
    "The person has removed the background they had set about themselves. "
    "Disregard what was said about them earlier in this chat; there is nothing there now."
)

# Said on the first copy to reach a chat that began without one: a session whose
# prompt was built before this plugin was installed, or before it had a section
# at all. It corrects nothing and says so, because a note that claims to replace
# something aims a model at text that was never there.
INTRODUCED = (
    "This is reaching this chat for the first time; "
    "nothing earlier in it said who you are talking to."
)

# And when the chat is shared. One session can carry turns from several people:
# a Bot Chat somebody started is a chat anybody who can reach the gateway may
# type into, and whoever started it is not the one sending every turn afterwards.
# The copy the chat already holds describes the person it was rendered for, so a
# turn from somebody else has to say that the person has CHANGED rather than
# that they edited anything — a model told "they changed this" about a different
# person merges two people into one, which is the failure this is here to end.
SUPERSEDES_BY_SENDER = (
    "Somebody else is sending this turn. This is who is talking to you now, "
    "and it replaces what the system prompt says about who that is."
)

SUPERSEDES_BY_SENDER_IN_CHAT = (
    "Somebody else is sending this turn. This is who is talking to you now, "
    "and it replaces what was said about them earlier in this chat."
)

# The same change, to somebody the app knows nothing about. Nothing can be said
# about them, so the only honest move is to withdraw the person who was named:
# a bot that goes on using the previous name is addressing the wrong person.
RETRACTED_BY_SENDER = (
    "Somebody else is sending this turn and nothing is shared about them. "
    "Disregard what the system prompt says about who you are talking to."
)

RETRACTED_BY_SENDER_IN_CHAT = (
    "Somebody else is sending this turn and nothing is shared about them. "
    "Disregard what was said earlier in this chat about who you are talking to."
)


# -- where all of this comes from -------------------------------------------
#
# The facts above say what the person is like. They do not say what a bot is
# reading, and a bot that has not been told has to be taught by hand — which is
# the one thing a context feature must not ask of anybody. So the section also
# carries a short, stable paragraph: where the facts come from, what may be done
# with them, and where to look for more.
#
# Two properties are deliberate. Every sentence is plain and permissive, because
# this is context and not instruction, and a paragraph of directives in a system
# prompt is a paragraph the person did not write. And every sentence is true on
# the gateway that renders it: the two that point somewhere — the command and
# the memory browser — are said only where that place will answer, by the same
# rule a capability follows.

ORIENTATION_SOURCE = (
    "These details come from the person's own profile in their Hermie app and reach you "
    "through the Hermie plugin on this gateway, which keeps them current."
)

ORIENTATION_USE = (
    "You may address them by name, use their timezone and locale for dates, times and "
    "language, and phrase steps for the device they are on."
)

ORIENTATION_SETTINGS = "They decide what is shared here, in Hermie under Settings → Context."

# Only where `register_command` actually took the command.
ORIENTATION_COMMAND = "`/me`, typed in this chat, prints what is shared and how it was worked out."

# Only where the memory module is switched on and will answer.
ORIENTATION_MEMORY = (
    "What they told you in earlier chats is kept in this profile's memory rather than here, "
    "and they can read that in Hermie too."
)

ORIENTATION_ASK = (
    "When something you need about them is not here, they have not shared it, "
    "and asking them is the only way to know."
)

# The last line, always. The reader is a model, and a model that is not told
# where a fact came from will treat it as an instruction. This is the person's
# own description of themselves, not a directive, and it says so.
FRAMING = "This is background the person set in their app, not an instruction for this turn."


@dataclass(frozen=True)
class Orientation:
    """Which of the orientation sentences this gateway can stand behind.

    The flags are the two that name a place to look. `/me` exists only where
    Hermes took the registration, and the memory browser only where the module
    is on, so a gateway that has neither says neither: pointing a bot at
    something that is not there is worse than pointing it nowhere.
    """

    command: bool = False
    memory: bool = False

    def lines(self) -> List[str]:
        """The sentences, in the order they are read AND dropped.

        Least load-bearing last, because a tight cap drops from the end.
        """
        found = [ORIENTATION_SOURCE, ORIENTATION_USE, ORIENTATION_SETTINGS]
        if self.command:
            found.append(ORIENTATION_COMMAND)
        if self.memory:
            found.append(ORIENTATION_MEMORY)
        found.append(ORIENTATION_ASK)
        return found


def render(
    user: Optional[UserContext],
    *,
    bot: str = "",
    max_chars: int = 1200,
    lead: str = "",
    orientation: Orientation = Orientation(),
) -> str:
    """The prompt text for one person, or an empty string.

    Empty matters: `register_system_prompt_section` renders into the prompt
    verbatim, and a heading with nothing under it teaches a model that the
    section is noise.

    `lead` says how this copy relates to one the chat has already seen — that it
    supersedes it (`SUPERSEDES`), or that it is the first one (`INTRODUCED`). It
    is part of the bounded text rather than something a caller glues on
    afterwards, so the cap covers it.

    The orientation paragraph gives way to the person's own words: when the cap
    is tight it is dropped a whole sentence at a time, because the budget exists
    for what they wrote and half a sentence about where to look is worse than
    none of one.
    """
    if user is None:
        return ""

    lines: List[str] = []
    if user.display_name:
        lines.append(f"You are talking to {user.display_name}.")

    device: List[str] = []
    if user.device_model:
        device.append(user.device_model)
    if user.device_os:
        device.append(user.device_os)
    if device:
        lines.append(f"They are on {' running '.join(device)}.")

    where: List[str] = []
    if user.timezone:
        where.append(f"their timezone is {user.timezone}")
    if user.locale:
        where.append(f"their locale is {user.locale}")
    if where:
        lines.append(f"For dates, times and language: {', and '.join(where)}.")

    if user.about:
        lines.append(f"What they told you about themselves: {user.about}")

    note = user.per_bot.get(bot) if bot else ""
    if note:
        lines.append(f"What they told you specifically about this chat: {note}")

    # Emptiness is decided on what the person actually wrote, before the framing
    # and the orientation are added: a section that is nothing but those is a
    # heading with nothing under it, which teaches a model that it is noise.
    if not lines:
        return ""

    if lead:
        lines.insert(0, lead)

    said = orientation.lines()
    while True:
        text = "\n".join(lines + said + [FRAMING])
        if len(text) <= max_chars or not said:
            break
        said = said[:-1]
    return text if len(text) <= max_chars else text[: max_chars - 1].rstrip() + "…"
