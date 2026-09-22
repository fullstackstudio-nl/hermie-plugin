"""Reading and editing a profile's memory, from inside the gateway process.

This is the one module in the plugin that answers HTTP. Everything else reaches
the app through `ui_meta`; a memory browser cannot, because it is a request/
response surface over data far too large to publish into a profile file. So it
mounts on the dashboard's own plugin router (`dashboard/plugin_api.py`), which
is what `docs/DESIGN.md` §1 describes.

**The trust model is the dashboard's, and it is not per user.** Hermes
authenticates a dashboard request with process-wide middleware and no route ever
sees a role, an owner or a permission — every authenticated caller can reach
every route, core's included. So these routes treat whoever is signed in to the
dashboard as an operator of this machine, which is the same thing core's own
`/api/memory` reset route already assumes. There is no per-user authorization
here because there is nothing to build one out of, and pretending otherwise
would be worse than saying so.

**Profile scoping is this module's own job.** A plugin route is handed no
profile and runs under whichever home the dashboard process started with, so
`profile` is required, checked against the gateway's real profile list, and
resolved to that profile's directory. The store is then opened under
`hermes_constants.set_hermes_home_override`, the public context-local override —
so `MemoryStore._path_for` resolves inside that profile, its file lock is the
lock on that profile's file, and the dashboard's own `HERMES_HOME` is never
touched on another profile's behalf. The override is a `ContextVar` and is set
and reset on the same thread that does the work.

There are exactly two targets, `memory` and `USER`, because the store dispatches
on a bare `target == "user"` and the tool layer refuses anything else. An
external provider (mem0 and friends) is **listed and never enumerated**: the
provider interface offers `prefetch(query)` returning opaque formatted text and
no listing call at all, so its entries cannot be shown read-only without
inventing an API Hermes does not have.

**Raw reading goes to the files, not to the store.** Everything above hands back
a memory the store has already parsed into entries, which is the shape to edit
and the wrong shape for "what is actually in there" — a heading, a blank line the
store kept and a delimiter that ended up inside an entry all vanish in the
parse. So `raw_files` reads the two files itself, as bytes-to-text and nothing
more, from the directory Hermes resolves per call so the home override still
moves it. It writes nothing and it parses nothing.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .. import contract
from . import browse
from .browse import TARGETS

logger = logging.getLogger(__name__)

NAME = "memory"

# What a memory file joins its entries with. Defined by the store; repeated here
# only as the fallback for a runtime where the store cannot be imported, and a
# test keeps the two equal when it can.
ENTRY_DELIMITER = "\n§\n"

# The file each target is stored in. One source with the labels a raw document
# carries, because they are the same two strings for the same reason: the label
# of a file shown as stored is the file's own name. A test keeps these equal to
# the store's own names wherever Hermes can be imported.
RAW_FILENAMES = dict(browse.RAW_LABELS)


class MemoryUnavailable(RuntimeError):
    """This process has no Hermes to ask — a test, a CLI, anything but a gateway."""


class ProfileRefused(ValueError):
    """The caller named a profile that is not a profile on this gateway."""


# -- the profile ------------------------------------------------------------


def _clean_profile(name: Any) -> str:
    """The name, or a refusal. Nothing here is turned into a path.

    A profile is an identifier the gateway already knows, so this rejects rather
    than sanitises: anything carrying a separator, a parent reference, a null or
    surrounding space is not a typo to be fixed, it is a caller trying a path.
    The real gate is the membership check below — this one exists so a traversal
    attempt is refused before it reaches any Hermes function at all.
    """
    text = str(name or "")
    if not text or text != text.strip():
        raise ProfileRefused("a profile name is required")
    if len(text) > 64:
        raise ProfileRefused("that is not a profile name")
    if any(bad in text for bad in ("/", "\\", "..", "\0", ":")) or text in (".", "~"):
        raise ProfileRefused("that is not a profile name")
    return text


def profile_names() -> List[str]:
    """Every profile this gateway has, including the default one."""
    try:
        from hermes_cli.profiles import list_profile_names  # type: ignore
    except Exception as exc:  # pragma: no cover - exercised only off a gateway
        raise MemoryUnavailable(f"hermes profiles are not importable here: {exc}")
    names = [str(item) for item in (list_profile_names() or [])]
    return names if "default" in names else ["default"] + names


def profile_home(name: Any) -> Path:
    """The home directory of a profile the gateway really has.

    Membership in the gateway's own list is the check. A name that is not on it
    is refused without saying whether it merely does not exist, because the two
    answers are the same answer to somebody guessing.
    """
    text = _clean_profile(name)
    if text not in profile_names():
        raise ProfileRefused(f"no profile named {text!r} on this gateway")
    from hermes_cli.profiles import get_profile_dir  # type: ignore

    return Path(str(get_profile_dir(text)))


@contextlib.contextmanager
def scoped(home: Path) -> Iterator[None]:
    """Run the body as though the gateway's home were *home*.

    `set_hermes_home_override` is context-local and public, and deliberately
    does not touch `os.environ` — which a process-wide write would, reaching
    every other thread in the gateway. Set and reset happen here, on one thread,
    so an exception cannot leave another profile's home bound.
    """
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override  # type: ignore

    token = set_hermes_home_override(str(home))
    try:
        yield
    finally:
        reset_hermes_home_override(token)


# -- the store --------------------------------------------------------------


def open_store() -> Any:
    """Hermes' own memory store, loaded from whatever home is bound right now.

    `load_on_disk_store` is the sanctioned read path: it applies the configured
    char limits and the enable flags before reading, which a bare `MemoryStore()`
    would not. It must be called inside `scoped`.
    """
    try:
        from tools.memory_tool import load_on_disk_store  # type: ignore
    except Exception as exc:
        raise MemoryUnavailable(f"the hermes memory store is not importable here: {exc}")
    return load_on_disk_store()


def entries_of(store: Any) -> Dict[str, List[str]]:
    """Both targets' entries, as the store parsed them."""
    return {
        "memory": [str(item) for item in getattr(store, "memory_entries", []) or []],
        "user": [str(item) for item in getattr(store, "user_entries", []) or []],
    }


def usage_of(store: Any, by_target: Dict[str, List[str]]) -> Dict[str, Tuple[int, int]]:
    """`{target: (chars, limit)}`, counted the way the store counts.

    The delimiter is part of what a store spends, so the count joins on it
    rather than summing the entries — a listing that disagreed with the store
    about how full a file is would have somebody deleting entries to fix a
    number that was never true.
    """
    limits = {
        "memory": int(getattr(store, "memory_char_limit", 0) or 0),
        "user": int(getattr(store, "user_char_limit", 0) or 0),
    }
    return {
        target: (len(ENTRY_DELIMITER.join(by_target.get(target) or [])), limits.get(target, 0))
        for target in TARGETS
    }


# -- the files, as they are stored ------------------------------------------


def memory_dir() -> Path:
    """The directory this home keeps its memory files in. Call inside `scoped`.

    Asked of Hermes rather than joined here. `get_memory_dir` is resolved per
    call precisely so the home override moves it, and a second opinion about
    where a memory file lives is how a route ends up reading a file nothing
    writes.
    """
    try:
        from tools.memory_tool import get_memory_dir  # type: ignore
    except Exception as exc:
        raise MemoryUnavailable(f"the hermes memory store is not importable here: {exc}")
    return Path(str(get_memory_dir()))


def raw_files() -> Tuple[Dict[str, str], List[str]]:
    """`({target: the file as stored}, [targets whose file could not be read])`.

    Call inside `scoped`. Nothing is written and nothing is parsed: the
    delimiters, a heading somebody put at the top and a blank line the store
    kept are the whole reason to read a file this way.

    A target with no file at all is in neither half, so the answer can tell "the
    file is there and empty" from "there is no file yet" — which a parsed listing
    cannot, and which is the difference between a memory that was cleared and one
    that was never written.

    `utf-8-sig` because the store reads them that way: a BOM a Windows editor
    left behind belongs to neither the file's content nor this answer. Decoding
    stays strict, so a file that is not text is reported as unreadable instead of
    being handed over with the undecodable bytes quietly replaced.
    """
    directory = memory_dir()
    found: Dict[str, str] = {}
    unreadable: List[str] = []
    for target in TARGETS:
        path = directory / RAW_FILENAMES[target]
        if not path.exists():
            continue
        try:
            found[target] = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            logger.info("hermie: could not read %s (%s)", path, exc)
            unreadable.append(target)
    return found, unreadable


# -- the providers -----------------------------------------------------------


def _discovered() -> List[Dict[str, Any]]:
    """Hermes' own memory-provider rows, or nothing where it cannot say.

    One place asks, so `list` and `raw` cannot end up disagreeing about which
    providers this gateway has. A gateway that cannot answer at all is not an
    error: the built-in memory is still readable and saying so is still useful.
    """
    try:
        from hermes_cli.web_server_memory import _discover_memory_provider_statuses  # type: ignore

        rows = _discover_memory_provider_statuses() or []
    except Exception as exc:
        logger.info("hermie: could not list memory providers (%s)", exc)
        return []
    return [
        row
        for row in rows
        if isinstance(row, dict) and str(row.get("name") or "") not in ("", browse.BUILTIN)
    ]


def providers() -> List[Dict[str, Any]]:
    """Which memory providers exist, and the fact that none can be listed.

    `enumerable` is `False` on every row and that is not a placeholder. The
    provider interface (`agent/memory_provider.py`) has `prefetch(query)`,
    which returns formatted text for a turn, and no call that returns entries.
    mem0's own surface is `search(query, top_k)` with no `get_all`. So a
    provider's memories cannot be shown read-only without inventing an API, and
    the honest answer is to name the provider and say it cannot be opened.
    """
    rows: List[Dict[str, Any]] = [
        {"name": browse.BUILTIN, "description": browse.BUILTIN_LABEL, "available": True, "enumerable": True}
    ]
    for row in _discovered():
        rows.append(
            {
                "name": str(row.get("name") or ""),
                "description": str(row.get("description") or ""),
                "available": bool(row.get("available")),
                # See the docstring. Not "not implemented" — not offered.
                "enumerable": False,
            }
        )
    return rows


def external_backends() -> List[Dict[str, Any]]:
    """Every backend besides the files, in the terms the `raw` answer uses.

    Call inside `scoped`: which provider a profile names is that profile's own
    config, so this is a different answer per profile.

    `available` is narrower here than on a `list` row, deliberately. There it is
    the discovery's own meaning — the provider's package imports — because that
    route reports what exists. Here it has to mean "this gateway could really
    use it", since the question being asked is what is stored in it: a provider
    named in the config with no credentials is installed and holds nothing this
    gateway could ever reach, and reporting it as available would point somebody
    at an empty card as though it were the answer. A gateway too old to say
    whether a provider is configured is taken at its word rather than accused.
    """
    return [
        browse.external_backend(
            str(row.get("name") or ""),
            label=str(row.get("description") or ""),
            installed=bool(row.get("available")),
            configured=row.get("configured") is not False,
        )
        for row in _discovered()
    ]


# -- the module -------------------------------------------------------------


class MemoryModule:
    """What the plugin advertises about all of this.

    The routes themselves are mounted by the dashboard, not by `register()`, so
    this class holds no handler. It holds the two switches, because a capability
    has to say whether this gateway will actually answer.
    """

    def __init__(self, runtime):
        self.runtime = runtime

    @property
    def browse_enabled(self) -> bool:
        return self.runtime.config("memory.browse", True) is not False

    @property
    def edit_enabled(self) -> bool:
        return self.runtime.config("memory.edit", True) is not False

    def capabilities(self) -> List[str]:
        found: List[str] = []
        if self.browse_enabled:
            found.append(contract.CAP_MEMORY_BROWSE)
            # Reading a backend as stored is reading: the same files, behind the
            # same switch, and a string of its own only so an app can tell a
            # plugin that serves the route from one old enough to answer 404.
            found.append(contract.CAP_MEMORY_RAW)
            # Editing without browsing is a switch nobody wants: an app that
            # cannot list entries cannot name one to replace or remove.
            if self.edit_enabled:
                found.append(contract.CAP_MEMORY_EDIT)
        return found


def settings_for(home: Path) -> Dict[str, bool]:
    """The two switches as *this profile's* config.yaml sets them.

    A route has no plugin context, so it cannot use `ctx.get_config`. It reads
    the same place that reads from — `plugins.entries.hermie.settings` in the
    config of the profile being asked about, which is also the right scope: the
    operator of a profile decides whether that profile's memory can be opened.
    Unreadable config means the defaults, which are both on.
    """
    values = {"browse": True, "edit": True}
    try:
        with scoped(home):
            from hermes_cli.config import load_config  # type: ignore

            config = load_config() or {}
        entry = (((config.get("plugins") or {}).get("entries") or {}).get("hermie") or {}).get("settings") or {}
        section = entry.get("memory") if isinstance(entry.get("memory"), dict) else {}
        for key in values:
            if isinstance(section.get(key), bool):
                values[key] = section[key]
    except Exception as exc:
        logger.info("hermie: could not read the memory settings (%s); using the defaults", exc)
    return values


def register(ctx, runtime) -> MemoryModule:
    """No hook and no route: the dashboard mounts those. Only the advert."""
    return MemoryModule(runtime)
