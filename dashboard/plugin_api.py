"""Hermie's memory routes, mounted by the dashboard at /api/plugins/hermie/.

Hermes discovers this file through `dashboard/manifest.json` and imports it with
`spec_from_file_location` under a name of its own — so it is NOT loaded as part
of the plugin package and cannot use relative imports. `_package()` below loads
the package beside it, the same way `tests/conftest.py` does, which keeps the
logic in the package where it is testable instead of duplicated here.

**Auth.** None of these handlers check a caller, and that is not an oversight.
Hermes gates `/api/plugins/...` with process-wide middleware that answers 401
before a handler runs; there is no per-route dependency to add, and no route on
this gateway — core's included — ever sees a user, a role or an owner. So the
trust model is the dashboard's: whoever is signed in is an operator of this
machine. README says so in the shared-gateway warning.

**Profile.** Required on every route, because a plugin handler is handed none
and would otherwise act on whichever profile the dashboard process happens to
have started with. It is validated against the gateway's real profile list and
the store is opened under that profile's home. See `memory/__init__.py`.

Every handler does its filesystem work on a worker thread: the store takes a
file lock, and a lock taken on the event loop would stall every other request.

`raw` is the one handler that reads a file itself rather than asking the store,
and it takes no lock. That is safe in the only direction it could go wrong: the
store writes a memory file by writing a temporary one and renaming it over the
old, so a reader sees the whole of one version or the whole of the other, never a
half-written file. Taking the store's lock to read would mean a browser tab could
hold up a bot's own write.
"""

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

router = APIRouter()

_PACKAGE = "hermie_plugin"
_ROOT = Path(__file__).resolve().parent.parent


def _package():
    """The plugin package, loaded once, from the directory above this file."""
    module = sys.modules.get(_PACKAGE)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(
        _PACKAGE, _ROOT / "__init__.py", submodule_search_locations=[str(_ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    # In sys.modules before execution so the package's own relative imports
    # resolve while it is still being built.
    sys.modules[_PACKAGE] = module
    spec.loader.exec_module(module)
    return module


def _memory():
    _package()
    from hermie_plugin.memory import browse as browse_mod  # noqa: F401
    import hermie_plugin.memory as memory_mod

    return memory_mod, browse_mod


def _profile_name():
    _package()
    import hermie_plugin.profile_name as profile_name_mod

    return profile_name_mod


class EditBody(BaseModel):
    profile: str
    target: str
    op: str
    content: Optional[str] = None
    old_text: Optional[str] = None
    index: Optional[int] = None


def _resolve(profile: Optional[str], *, need_edit: bool = False):
    """The profile's home and settings, or the refusal the caller has earned."""
    memory_mod, _ = _memory()
    try:
        home = memory_mod.profile_home(profile)
    except memory_mod.ProfileRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except memory_mod.MemoryUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    settings = memory_mod.settings_for(home)
    if not settings["browse"]:
        raise HTTPException(
            status_code=403,
            detail=(
                "Memory browsing is switched off for this profile. Set "
                "plugins.entries.hermie.settings.memory.browse to true in that "
                "profile's config.yaml and restart the gateway."
            ),
        )
    if need_edit and not settings["edit"]:
        raise HTTPException(
            status_code=403,
            detail=(
                "Memory editing is switched off for this profile. Set "
                "plugins.entries.hermie.settings.memory.edit to true in that "
                "profile's config.yaml and restart the gateway."
            ),
        )
    return home, settings


def _read(home) -> Dict[str, Any]:
    """Both targets' entries and what they cost, under that profile's home."""
    memory_mod, _ = _memory()
    with memory_mod.scoped(home):
        store = memory_mod.open_store()
        by_target = memory_mod.entries_of(store)
        usage = memory_mod.usage_of(store, by_target)
    return {"by_target": by_target, "usage": usage}


@router.get("/memory/list")
async def memory_list(profile: Optional[str] = None):
    home, _settings = _resolve(profile)
    memory_mod, browse_mod = _memory()

    def run():
        read = _read(home)
        answer = browse_mod.listing(read["by_target"], read["usage"])
        answer["profile"] = str(profile)
        answer["providers"] = memory_mod.providers()
        return answer

    return await run_in_threadpool(_guard, run, "list")


@router.get("/memory/search")
async def memory_search(profile: Optional[str] = None, q: str = ""):
    if not str(q or "").strip():
        raise HTTPException(status_code=400, detail="q is required")
    home, _settings = _resolve(profile)
    _memory_mod, browse_mod = _memory()

    def run():
        answer = browse_mod.search(_read(home)["by_target"], q)
        answer["profile"] = str(profile)
        return answer

    return await run_in_threadpool(_guard, run, "search")


@router.get("/memory/graph")
async def memory_graph(profile: Optional[str] = None, offset: int = 0, limit: int = 0):
    home, _settings = _resolve(profile)
    _memory_mod, browse_mod = _memory()

    def run():
        return browse_mod.graph(
            _read(home)["by_target"],
            profile=str(profile),
            offset=offset,
            limit=limit or browse_mod.DEFAULT_PAGE,
        )

    return await run_in_threadpool(_guard, run, "graph")


@router.get("/memory/raw")
async def memory_raw(profile: Optional[str] = None, backend: Optional[str] = None):
    """Every backend of this profile, with what it holds or why it cannot say.

    Gated by `memory.browse` and nothing else: this is reading, of the same files
    `list` reads, and a second switch for the same permission would be a switch
    somebody has to discover before the tab works.

    `backend` narrows the answer to one and is optional. A name this gateway does
    not have is a 400 rather than an empty list, because an app that mistyped a
    backend and got `[]` would report that the gateway has nothing.
    """
    home, settings = _resolve(profile)
    memory_mod, browse_mod = _memory()

    def run():
        with memory_mod.scoped(home):
            documents, unreadable = memory_mod.raw_files()
            backends = [
                browse_mod.builtin_backend(
                    documents, editable=settings["edit"], unreadable=unreadable
                )
            ] + memory_mod.external_backends()
        wanted = str(backend or "")
        if wanted:
            backends = [row for row in backends if row["name"] == wanted]
            if not backends:
                raise HTTPException(
                    status_code=400, detail=f"no memory backend named '{wanted}' on this gateway"
                )
        return browse_mod.raw(str(profile), backends)

    return await run_in_threadpool(_guard, run, "raw")


@router.post("/memory/edit")
async def memory_edit(body: EditBody):
    memory_mod, browse_mod = _memory()
    if body.target not in browse_mod.TARGETS:
        raise HTTPException(
            status_code=400, detail=f"target must be one of {', '.join(browse_mod.TARGETS)}"
        )
    if body.op not in ("add", "replace", "remove"):
        raise HTTPException(status_code=400, detail="op must be add, replace or remove")
    home, _settings = _resolve(body.profile, need_edit=True)

    def run():
        # Everything happens inside one scope AND one store, so the entry an
        # index names is the entry the store is about to match on.
        with memory_mod.scoped(home):
            store = memory_mod.open_store()
            entries = memory_mod.entries_of(store).get(body.target) or []
            if body.op == "add":
                if not str(body.content or "").strip():
                    return {"success": False, "error": "content is required to add an entry"}
                return store.add(body.target, str(body.content))

            old_text = browse_mod.find_text(entries, body.old_text, body.index)
            if not old_text:
                return {
                    "success": False,
                    "error": "no such entry; list the target again and retry",
                    "current_entries": entries,
                }
            if body.op == "remove":
                return store.remove(body.target, old_text)
            if not str(body.content or "").strip():
                return {"success": False, "error": "content is required to replace an entry"}
            return store.replace(body.target, old_text, str(body.content))

    return await run_in_threadpool(_guard, run, "edit")


class ProfileDisplayNameBody(BaseModel):
    display_name: str


def _resolve_profile_for_edit(name: Optional[str]):
    """That profile's home, or the refusal the caller has earned, for setting
    its display name.

    Deliberately not `_resolve` above: that one answers 400 for both a bad name
    and an unknown one, because that is the pair of answers `memory`'s routes
    have always given. This route owes the app a 404 for a profile that simply
    is not there, so `hermie_plugin.profile_name.profile_home` is asked
    instead — it raises the two cases apart. `need_edit` has no browse
    counterpart here: there is nothing to read before there is something to
    write, so one switch (`profiles.edit`) covers the whole route.
    """
    profile_name_mod = _profile_name()
    try:
        home = profile_name_mod.profile_home(name)
    except profile_name_mod.ProfileRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except profile_name_mod.ProfileNotFound as exc:
        raise HTTPException(status_code=404, detail=f"no profile named {str(exc)!r} on this gateway")
    except profile_name_mod.ProfileNameUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    if not profile_name_mod.target_profile_edit_enabled(home):
        raise HTTPException(
            status_code=403,
            detail=(
                "Profile editing is switched off for this profile. Set "
                "plugins.entries.hermie.settings.profiles.edit to true in that "
                "profile's config.yaml and restart the gateway."
            ),
        )
    return home


@router.patch("/profiles/{name}")
async def profile_display_name(name: str, body: ProfileDisplayNameBody):
    """Set *name*'s presentation label. See `hermie_plugin.profile_name` for
    why this is not a second door to Hermes' own `PATCH /api/profiles/{name}`.
    """
    profile_name_mod = _profile_name()
    home = _resolve_profile_for_edit(name)

    def run():
        cleaned = profile_name_mod.set_display_name(home, body.display_name)
        return {"name": name, "display_name": cleaned}

    return await run_in_threadpool(_guard_profile_name, run, "profile display name")


def _guard_profile_name(run, label: str):
    """Run *run*, turning a bad display name into a 400, a missing Hermes into
    a 503 and a crash into a 500. Separate from `_guard` because that one maps
    `MemoryUnavailable`, not `ProfileNameUnavailable` or `DisplayNameRefused`.
    """
    profile_name_mod = _profile_name()
    try:
        return run()
    except HTTPException:
        raise
    except profile_name_mod.DisplayNameRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except profile_name_mod.ProfileNameUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{label} failed: {exc}")


def _guard(run, label: str):
    """Run *run*, turning a missing Hermes into a 503 and a crash into a 500."""
    memory_mod, _ = _memory()
    try:
        return run()
    except HTTPException:
        raise
    except memory_mod.MemoryUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"memory {label} failed: {exc}")
