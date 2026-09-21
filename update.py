"""Which build of this plugin is installed, and whether a newer one exists.

`hermes plugins update hermie` already git-pulls the installed tree, so the
update *path* is one command. What was missing is the app being able to say
that the command is worth running.

Two halves, and only the first is on by default:

- **What is installed.** Read off the tree Hermes cloned, with no subprocess and
  no network: `.git/HEAD`, and the ref file it points at. This is a fact about
  the local machine and it costs two small file reads at load.
- **What is newest.** One request to the plugin's own repository, cached for an
  hour in the plugin's state. It is **off unless asked for**, because a gateway
  that makes an unprompted outbound request is a surprise on a product whose
  whole pitch is that it has no relay, no account and no credential. Nothing
  identifying is sent: no gateway id, no profile, no version, no install id — it
  is a plain GET of a public URL, and the answer is a tag name.

An app that would rather not switch it on has everything it needs anyway: the
advert carries this build's `version` and installed ref, and comparing that
against the newest release is something a phone can do on its own network
without a gateway reaching anywhere.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

# The plugin's own repository, from the one place that names it. Not
# configurable: a plugin that can be pointed at an arbitrary URL to ask "is
# there a newer me" is a plugin that can be pointed at an arbitrary URL.
from .contract import REPO

logger = logging.getLogger(__name__)

LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"

# An hour. Long enough that a restart loop cannot hammer anything, short enough
# that somebody who just published a release sees it the same afternoon.
CACHE_SECONDS = 3600

TIMEOUT_SECONDS = 5

# Where the cached answer lives inside the plugin's state.
STATE_KEY = "update"


def installed_ref(root: Path) -> str:
    """The commit this tree is checked out at, or ``""``.

    `hermes plugins install` clones the repo, so the installed tree is a git
    checkout and `.git/HEAD` names the build. Read directly rather than through
    `git`: a subprocess at plugin load is a cost and a dependency, and this is
    two reads of small files that either exist or do not.
    """
    try:
        head = (root / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    except Exception:
        return ""
    if not head.startswith("ref:"):
        # A detached HEAD, which is what `--ref <sha>` leaves behind.
        return head if _looks_like_sha(head) else ""
    name = head[4:].strip()
    try:
        return (root / ".git" / name).read_text(encoding="utf-8").strip()
    except Exception:
        pass
    # A packed ref, which is what a fresh clone usually has.
    try:
        for line in (root / ".git" / "packed-refs").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == name:
                return parts[0]
    except Exception:
        pass
    return ""


def _looks_like_sha(value: str) -> bool:
    return len(value) == 40 and all(character in "0123456789abcdef" for character in value.lower())


def cached(state: Any, *, now: float) -> Optional[Dict[str, Any]]:
    """The last answer, while it is still fresh enough to use."""
    entry = state.data.get(STATE_KEY)
    if not isinstance(entry, dict):
        return None
    at = entry.get("at")
    if not isinstance(at, int) or isinstance(at, bool):
        return None
    return entry if 0 <= now - at < CACHE_SECONDS else None


def fetch_latest(url: str = LATEST_URL) -> str:
    """The newest release tag, or ``""``.

    Deliberately boring: one GET, a short timeout, no headers that say anything
    about this machine, and every failure is the same answer as "there is no
    newer one". A gateway must not be slower, noisier or less private because a
    version check did not work out.
    """
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
        tag = body.get("tag_name") if isinstance(body, dict) else None
        return str(tag)[:64] if isinstance(tag, str) and tag.strip() else ""
    except Exception as exc:
        logger.info("hermie: could not ask for the newest release (%s)", exc)
        return ""


def check(state: Any, *, now: Optional[float] = None, fetch=None) -> Dict[str, Any]:
    """The newest release, from the cache when it is fresh and the net when not.

    Returns the whole cache entry so the caller can put `latest` in the advert
    and `at` beside it. A failed fetch is cached too — otherwise a gateway with
    no outbound route would try again on every single load.
    """
    stamp = int(now if now is not None else time.time())
    entry = cached(state, now=stamp)
    if entry is not None:
        return entry
    # Resolved here rather than as a default argument: a default binds the
    # function object once, at import, and then nothing can stand in for it.
    entry = {"latest": (fetch or fetch_latest)(), "at": stamp}
    state.data[STATE_KEY] = entry
    state.save()
    return entry
