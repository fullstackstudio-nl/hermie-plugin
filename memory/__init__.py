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
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .. import contract
from .browse import TARGETS

logger = logging.getLogger(__name__)

NAME = "memory"

# What a memory file joins its entries with. Defined by the store; repeated here
# only as the fallback for a runtime where the store cannot be imported, and a
# test keeps the two equal when it can.
ENTRY_DELIMITER = "\n§\n"


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
        {"name": "builtin", "description": "MEMORY.md and USER.md", "available": True, "enumerable": True}
    ]
    try:
        from hermes_cli.web_server_memory import _discover_memory_provider_statuses  # type: ignore

        for row in _discover_memory_provider_statuses() or []:
            if not isinstance(row, dict) or str(row.get("name") or "") == "builtin":
                continue
            rows.append(
                {
                    "name": str(row.get("name") or ""),
                    "description": str(row.get("description") or ""),
                    "available": bool(row.get("available")),
                    # See the docstring. Not "not implemented" — not offered.
                    "enumerable": False,
                }
            )
    except Exception as exc:
        logger.info("hermie: could not list memory providers (%s)", exc)
    return rows


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
