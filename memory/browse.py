"""Turning memory entries into something an app can list, search and draw.

Everything here is a pure function of entries that were already read, so the
whole shape of a listing, a search and a graph is testable without a profile, a
gateway or a store. The half that touches Hermes is in ``__init__.py``.

A memory file is plain UTF-8 text: entries joined by ``"\\n§\\n"``, no ids, no
timestamps, no structure. So an id has to be minted here, and the only honest
one is positional — ``memory:3`` is "the fourth entry of MEMORY.md as it reads
right now". That is deliberately not a handle: entries move when one above them
is removed. Every write goes back through ``MemoryStore`` keyed on the entry's
**text**, which is what the store itself matches on, so an index that went stale
between a read and a write cannot delete the wrong line.

The `raw` half of this module does the opposite of all that: it hands a document
back exactly as it is stored, delimiters and headings and blank lines included,
because the parse above is what hides them. A listing answers "which entries are
there"; that one answers "what is in the file", and the two are different
questions a person asks for different reasons.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Both targets, in the order a person reads them. There are exactly two: the
# store dispatches on a bare `target == "user"` and the tool layer rejects
# anything else, so a third would be an invention of ours.
TARGETS = ("memory", "user")

# How much of an entry a node carries. A graph is drawn, not read; the full text
# is one `list` call away and putting it in every node is how a payload gets to
# be megabytes.
EXCERPT_CHARS = 120

# Ceilings for one `graph` response, so a large store cannot answer with
# something an app has to stream.
MAX_NODES = 400
MAX_EDGES = 1200
DEFAULT_PAGE = 100

# What one `raw` document may carry. A memory file is a few kilobytes and the
# store's own limits are smaller still, so this is not a page size — there is no
# way for a reader to ask for the rest, and no intention of adding one. It is a
# ceiling on a file that has gone wrong, and `truncated` beside the real `chars`
# is what an answer says when it bit.
RAW_DOCUMENT_CHARS = 256 * 1024

# The built-in backend: the two files themselves, rather than a provider.
BUILTIN = "builtin"
BUILTIN_LABEL = "MEMORY.md and USER.md"

# What each target's document is called. It is the file's own name on disk,
# because a view whose whole point is showing a file as stored should call that
# file what the filesystem calls it. `memory/__init__.py` reads the files by
# these same names, and a test keeps them equal to the store's.
RAW_LABELS = {"memory": "MEMORY.md", "user": "USER.md"}

# Cheap topics. None of this is NLP and it does not pretend to be: a capitalised
# word that is not sentence-initial, an @handle, a #hashtag, a date. It is there
# so a graph has something to cluster on, and a topic that is nonsense is a node
# nobody clicks rather than a wrong answer.
HANDLE = re.compile(r"(?<![\w@])@([A-Za-z0-9_][A-Za-z0-9_.-]{1,30})")
HASHTAG = re.compile(r"(?<![\w#])#([A-Za-z][A-Za-z0-9_-]{1,30})")
DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
CAPITALISED = re.compile(r"\b([A-Z][a-zA-Z0-9]{2,}(?:\s+[A-Z][a-zA-Z0-9]{2,}){0,2})\b")

# Words that start a sentence often enough that treating them as a topic would
# put "The" at the centre of every graph.
STOPWORDS = frozenset(
    """the this that these those they them their there here when where what which who whom whose
    and but for nor yet with from into onto upon about after before during until while because
    should would could must might will shall have has had been being does did doing not never
    always often sometimes usually prefer prefers wants needs uses using user memory note notes""".split()
)


def entry_id(target: str, index: int) -> str:
    """``memory:3``. Positional, and the docstring above says why that is safe."""
    return f"{target}:{index}"


def excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    """One line, bounded, with the ellipsis inside the bound."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def topics_in(text: str) -> List[str]:
    """Every cheap topic in one entry, deduplicated, in a stable order."""
    found: List[str] = []
    for pattern in (HANDLE, HASHTAG, DATE):
        found.extend(match.group(1) for match in pattern.finditer(text or ""))
    for match in CAPITALISED.finditer(text or ""):
        phrase = match.group(1).strip()
        if phrase.lower() not in STOPWORDS and len(phrase) > 2:
            found.append(phrase)
    out: List[str] = []
    for topic in found:
        if topic not in out:
            out.append(topic)
    return out


def entry_rows(entries: Iterable[str], target: str) -> List[Dict[str, Any]]:
    """One row per entry: an id, the text, its length, and its topics."""
    return [
        {
            "id": entry_id(target, index),
            "target": target,
            "index": index,
            "text": text,
            "chars": len(text),
            "topics": topics_in(text),
        }
        for index, text in enumerate(entries)
    ]


def listing(
    by_target: Dict[str, List[str]], usage: Dict[str, Tuple[int, int]]
) -> Dict[str, Any]:
    """The `list` answer: entries per target, with what they cost.

    `usage` is `{target: (chars, limit)}`, read off the store rather than
    counted here — the store counts the delimiter as part of the length and a
    second opinion about that is a second answer.
    """
    targets = []
    for target in TARGETS:
        chars, limit = usage.get(target, (0, 0))
        targets.append(
            {
                "target": target,
                "entries": entry_rows(by_target.get(target) or [], target),
                "chars": chars,
                "limit": limit,
                "percent": round(100 * chars / limit) if limit else 0,
            }
        )
    return {"targets": targets}


def raw_document(target: str, content: str, *, limit: int = RAW_DOCUMENT_CHARS) -> Dict[str, Any]:
    """One document as stored, cut only if it is absurd, and saying when it was.

    `chars` is the length of the WHOLE document rather than of what is carried.
    A reader that measured the string it received would report a runaway file as
    small on the one occasion the number mattered, and `truncated` would be the
    only hint left that it had ever been bigger.
    """
    text = "" if content is None else str(content)
    return {
        "id": target,
        "label": RAW_LABELS.get(target, target),
        "content": text[:limit],
        "chars": len(text),
        "truncated": len(text) > limit,
    }


def builtin_backend(
    documents: Dict[str, str],
    *,
    editable: bool,
    unreadable: Iterable[str] = (),
    limit: int = RAW_DOCUMENT_CHARS,
) -> Dict[str, Any]:
    """The two files, in the order a person reads them, exactly as they are held.

    A target that is not in *documents* has no file on disk, and it is absent
    from the answer rather than present and empty. That distinction is the
    reason somebody opens this: an empty document says the file is there and
    bare, and a missing one says the store has never written it, and being told
    the first when the second is true sends a person looking in the wrong place.

    A file that exists and cannot be read is in *unreadable* and is named in the
    note instead of shown as empty, because empty is a claim about its contents.
    """
    refused = set(unreadable)
    unread = [RAW_LABELS.get(target, target) for target in TARGETS if target in refused]
    return {
        "name": BUILTIN,
        "label": BUILTIN_LABEL,
        "available": True,
        "editable": bool(editable),
        "note": f"This gateway could not read {' or '.join(unread)}." if unread else None,
        "documents": [
            raw_document(target, documents[target], limit=limit)
            for target in TARGETS
            if target in documents
        ],
    }


def external_backend(
    name: str, *, label: str = "", installed: bool = True, configured: bool = True
) -> Dict[str, Any]:
    """A provider-backed memory, which can be named and cannot be enumerated.

    `documents` is empty on every one of these and the note says which kind of
    empty it is. A provider offers `prefetch(query)` — formatted text for one
    turn — and mem0's own surface is a query with a top-k and no call that
    returns everything, so there is nothing to ask that would answer "what is in
    there". Saying that is the honest answer and inventing an API is not.

    Not installed, installed but not configured, and set up but unlistable are
    three different things to be told: the first two are something to go and do,
    the third is somewhere else to go and look. `available` is false for the two
    that are not really here, which is what an app reads to tell them apart.
    """
    if not installed:
        note = f"{name} is not installed on this gateway."
    elif not configured:
        note = f"{name} is installed on this gateway and is not configured for this profile."
    else:
        note = f"{name} answers a query and offers no call that lists what it holds."
    return {
        "name": name,
        "label": label or name,
        "available": bool(installed) and bool(configured),
        # No route writes a whole document, for any backend. The field is on the
        # wire for the day one does; until then a reader that trusted it would
        # be drawing a control that cannot save.
        "editable": False,
        "note": note,
        "documents": [],
    }


def raw(profile: str, backends: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The `raw` answer: which profile was read, and every backend it has."""
    return {"profile": str(profile), "backends": list(backends)}


def matches(text: str, query: str) -> bool:
    """A plain substring match, case-insensitively, on whole words where it can.

    Deliberately not a ranking, a stemmer or a regex: a memory store is a few
    dozen lines, a person searching it knows roughly what they wrote, and a
    query that is a regex the user did not mean is worse than one that matches
    nothing. A multi-word query has to match every word, in any order.
    """
    haystack = (text or "").lower()
    words = [word for word in (query or "").lower().split() if word]
    return bool(words) and all(word in haystack for word in words)


def search(by_target: Dict[str, List[str]], query: str) -> Dict[str, Any]:
    """Every entry matching *query*, across both targets, keeping its id."""
    hits = [
        row
        for target in TARGETS
        for row in entry_rows(by_target.get(target) or [], target)
        if matches(row["text"], query)
    ]
    return {"query": query, "count": len(hits), "results": hits}


def graph(
    by_target: Dict[str, List[str]],
    *,
    profile: str,
    offset: int = 0,
    limit: int = DEFAULT_PAGE,
    max_nodes: int = MAX_NODES,
    max_edges: int = MAX_EDGES,
) -> Dict[str, Any]:
    """Nodes and edges an app can draw, over one page of entries.

    The page is over ENTRIES, not over nodes: a page whose topic nodes happened
    to fill the cap would silently drop entries, and an app paging through would
    never learn it had missed one. Topic and profile nodes ride along with the
    entries that need them, and the caps are a last defence rather than the
    paging mechanism — `truncated` says when one bit.
    """
    rows = [row for target in TARGETS for row in entry_rows(by_target.get(target) or [], target)]
    total = len(rows)
    offset = max(0, int(offset))
    limit = max(1, int(limit))
    page = rows[offset : offset + limit]

    profile_node = {"id": f"profile:{profile}", "type": "profile", "label": profile}
    nodes: List[Dict[str, Any]] = [profile_node]
    edges: List[Dict[str, Any]] = []
    seen_topics: Dict[str, str] = {}
    truncated = False

    # entry -> profile, and entry -> topic. A topic node is created the first
    # time an entry on this page needs it.
    by_topic: Dict[str, List[str]] = {}
    for row in page:
        if len(nodes) >= max_nodes:
            truncated = True
            break
        nodes.append(
            {
                "id": row["id"],
                "type": "entry",
                "target": row["target"],
                "label": excerpt(row["text"]),
                "chars": row["chars"],
            }
        )
        edges.append({"from": row["id"], "to": profile_node["id"], "type": "in_profile"})
        for topic in row["topics"]:
            node_id = seen_topics.get(topic)
            if node_id is None:
                if len(nodes) >= max_nodes:
                    truncated = True
                    break
                node_id = f"topic:{topic}"
                seen_topics[topic] = node_id
                nodes.append({"id": node_id, "type": "topic", "label": topic})
            edges.append({"from": row["id"], "to": node_id, "type": "mentions"})
            by_topic.setdefault(topic, []).append(row["id"])

    # entry <-> entry, when two share a topic. Each pair once, and only inside
    # this page: an edge to an entry the caller was never sent is an edge it
    # cannot draw.
    for topic, members in by_topic.items():
        for first in range(len(members)):
            for second in range(first + 1, len(members)):
                if len(edges) >= max_edges:
                    truncated = True
                    break
                edges.append(
                    {"from": members[first], "to": members[second], "type": "shares_topic", "topic": topic}
                )
            if len(edges) >= max_edges:
                break

    return {
        "nodes": nodes,
        "edges": edges[:max_edges],
        "page": {
            "offset": offset,
            "limit": limit,
            "returned": len(page),
            "total": total,
            "hasMore": offset + len(page) < total,
        },
        "truncated": truncated,
    }


def find_text(entries: List[str], entry: Optional[str], index: Optional[int]) -> str:
    """The text an edit is aimed at, from a text or from an index.

    An app may hold either. The text is preferred and is what goes to the store;
    an index is only a way of naming one, and naming one that is no longer there
    is an error rather than a guess at the neighbour.
    """
    if entry:
        return str(entry)
    if index is None:
        return ""
    position = int(index)
    return entries[position] if 0 <= position < len(entries) else ""
