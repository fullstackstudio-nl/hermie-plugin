"""Reading and writing a profile's ``ui_meta`` from inside the gateway process.

Hermie's app reaches ``ui_meta`` over the WebSocket, through ``profiles.list``
and ``profiles.configure``. A plugin is already inside the process that owns the
file, so it reads the file instead: no second connection, no credential of its
own, and no dependency on the gateway being willing to talk to itself.

The file is ``profile.yaml`` — ``$HERMES_HOME/profile.yaml`` for the default
profile and ``$HERMES_HOME/profiles/<name>/profile.yaml`` for the others. It
holds two maps that matter here:

    ui_meta:              <key> -> arbitrary JSON
    _ui_meta_revisions:   <key> -> integer, bumped on every write

The revision map is the gateway's per-key compare-and-swap. A client sends the
revision it last saw and its write is rejected when the key moved underneath it.
This module honours that from the writing side: it only ever writes keys this
plugin owns, it bumps their revisions, and it never touches a neighbouring key.

**The plugin does not write `hermie-app`.** That key belongs to the app, which
holds a revision for it and will have its write rejected if the plugin bumps it
behind the app's back. Everything the plugin publishes goes under its own
`hermie-plugin` key, which no app version writes.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# The key the app owns and the plugin only ever READS.
APP_KEY = "hermie-app"

# The key the plugin owns and is free to write.
PLUGIN_KEY = "hermie-plugin"


def hermes_home() -> Path:
    """The active profile's home, from Hermes when it is importable.

    A test (and `python -m hermie` style poking) has no Hermes on the path, so
    ``HERMES_HOME`` is honoured as a fallback rather than crashing the import.
    """
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(str(get_hermes_home()))
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def profile_path(home: Optional[Path] = None) -> Path:
    """The ``profile.yaml`` of the profile whose home this is."""
    return (home or hermes_home()) / "profile.yaml"


def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import yaml  # PyYAML ships with Hermes.

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        # An unreadable profile.yaml is the gateway's problem, not ours, and
        # guessing at {} here would be destructive on the next write. Callers
        # that write re-raise; callers that read degrade to "no registrations".
        logger.warning("hermie: could not read %s: %s", path, exc)
        raise
    return data if isinstance(data, dict) else {}


def read_key(key: str, home: Optional[Path] = None) -> Any:
    """One ``ui_meta`` key, or ``None`` when it is absent or unreadable."""
    try:
        data = _load_yaml(profile_path(home))
    except Exception:
        return None
    meta = data.get("ui_meta")
    return meta.get(key) if isinstance(meta, dict) else None


def read_revision(key: str, home: Optional[Path] = None) -> int:
    """The gateway's revision counter for one key; 0 when it has never been written."""
    try:
        data = _load_yaml(profile_path(home))
    except Exception:
        return 0
    revisions = data.get("_ui_meta_revisions")
    value = revisions.get(key) if isinstance(revisions, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def write_key(key: str, value: Any, home: Optional[Path] = None) -> bool:
    """Replace one ``ui_meta`` key and bump its revision, leaving the rest alone.

    Returns whether the file was written. This is a read-modify-write on a file
    the gateway also writes, so it is done through a temp file and an atomic
    rename: a reader sees the old bytes or the new ones, never a half file. The
    window between the read and the rename is real and is documented in
    DESIGN.md — it is narrow, this plugin writes rarely, and the only key it
    writes is one nothing else touches.
    """
    if key == APP_KEY:
        raise ValueError(f"hermie: refusing to write {APP_KEY!r}; that key belongs to the app")

    path = profile_path(home)
    try:
        data = _load_yaml(path)
    except Exception:
        return False

    meta = data.get("ui_meta")
    if not isinstance(meta, dict):
        meta = {}
    revisions = data.get("_ui_meta_revisions")
    if not isinstance(revisions, dict):
        revisions = {}

    if value is None:
        # `None` removes a key, the same way the app's own writes do, and the
        # revision counter survives the removal so a stale client still loses.
        meta.pop(key, None)
    else:
        meta[key] = json.loads(json.dumps(value))

    current = revisions.get(key)
    revisions[key] = (current if isinstance(current, int) and not isinstance(current, bool) else 0) + 1

    if meta:
        data["ui_meta"] = meta
    else:
        data.pop("ui_meta", None)
    data["_ui_meta_revisions"] = revisions

    try:
        import yaml

        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(path.parent), prefix=".hermie-", suffix=".yaml", delete=False
        ) as handle:
            yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=True)
            handle.flush()
            os.fsync(handle.fileno())
            temp = Path(handle.name)
        os.replace(temp, path)
    except Exception as exc:
        logger.warning("hermie: could not write %s: %s", path, exc)
        return False
    return True
