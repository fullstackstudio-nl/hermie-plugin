"""The memory browser: what it shows, what it refuses, and whose files it reads.

Two halves, tested apart because they fail for different reasons.

`browse.py` is pure — entries in, a listing or a graph out — so it is tested
directly on lists of strings. The half in `memory/__init__.py` reaches Hermes,
and the thing most worth pinning there is **profile scoping**: a plugin route is
handed no profile and runs under whichever home the dashboard process started
with, so every one of these routes has to resolve a profile itself and open the
store under it. A test that let that slip would let a route read the wrong
person's memory on a multiplexed gateway.

The Hermes-facing tests run against a fake `hermes_constants`, a fake
`hermes_cli.profiles` and a fake `tools.memory_tool` installed in `sys.modules`,
because Hermes is not importable here. They are about the plugin's own contract
with those modules — that it validates before resolving, that it sets the home
override and resets it, that it never builds a path itself — which is exactly
the part a real gateway would not tell us about until it was too late.

The raw side of `browse.py` is pure too, and the tests for it are about telling
three kinds of nothing apart: a file that is absent, a file that is there and
bare, and a backend that cannot be listed at all. Reading real files through the
route is in `test_memory_routes.py`, where there is a filesystem to read.
"""

import sys
import types

import pytest

from hermie_plugin.memory import browse


# -- the listing -------------------------------------------------------------


def test_entries_get_ids_that_say_where_they_came_from():
    rows = browse.entry_rows(["first", "second"], "memory")

    assert [row["id"] for row in rows] == ["memory:0", "memory:1"]
    assert rows[0]["chars"] == len("first")


def test_a_listing_reports_both_targets_with_what_they_cost():
    answer = browse.listing(
        {"memory": ["a" * 10], "user": []}, {"memory": (10, 100), "user": (0, 50)}
    )

    memory, user = answer["targets"]
    assert (memory["target"], memory["chars"], memory["limit"], memory["percent"]) == ("memory", 10, 100, 10)
    assert (user["target"], user["entries"], user["percent"]) == ("user", [], 0)


def test_a_listing_names_both_targets_even_when_one_is_empty():
    """An app that only ever saw the targets with content could not add to the other."""
    answer = browse.listing({}, {})

    assert [row["target"] for row in answer["targets"]] == list(browse.TARGETS)


def test_a_limit_of_zero_does_not_divide_by_it():
    assert browse.listing({"memory": ["x"]}, {"memory": (1, 0)})["targets"][0]["percent"] == 0


# -- search ------------------------------------------------------------------


def entries():
    return {
        "memory": [
            "Sebas runs FullStack Studio and prefers short answers.",
            "The gateway restart on 2026-09-21 lost the cron schedule.",
            "Ask @max before touching the invoice templates. #billing",
        ],
        "user": ["Timezone is Europe/Amsterdam."],
    }


def test_search_finds_across_both_targets():
    found = browse.search(entries(), "europe")

    assert found["count"] == 1
    assert found["results"][0]["target"] == "user"


def test_every_word_has_to_match_and_order_does_not():
    assert browse.search(entries(), "studio sebas")["count"] == 1
    assert browse.search(entries(), "sebas invoice")["count"] == 0


def test_an_empty_query_matches_nothing_rather_than_everything():
    for query in ("", "   ", None):
        assert browse.search(entries(), query)["count"] == 0


def test_a_query_that_looks_like_a_regex_is_read_as_text():
    """A person searching their own notes did not mean to write a pattern."""
    store = {"memory": ["a literal .* lives here", "nothing to see"], "user": []}

    assert browse.search(store, ".*")["count"] == 1


# -- topics ------------------------------------------------------------------


def test_the_cheap_topics_are_the_four_that_were_asked_for():
    found = browse.topics_in("Ask @max about FullStack Studio on 2026-09-21 #billing")

    assert "max" in found and "billing" in found
    assert "2026-09-21" in found and "FullStack Studio" in found


def test_a_sentence_opener_is_not_a_topic():
    """Otherwise "The" sits at the centre of every graph."""
    assert browse.topics_in("The gateway restarted. Always check the log.") == []


def test_a_topic_is_named_once_however_often_it_appears():
    assert browse.topics_in("@max and @max again").count("max") == 1


# -- the graph ---------------------------------------------------------------


def test_the_graph_links_entries_to_the_profile_and_to_their_topics():
    drawn = browse.graph(entries(), profile="jurist")

    kinds = {node["type"] for node in drawn["nodes"]}
    assert kinds == {"profile", "entry", "topic"}
    assert any(edge["type"] == "in_profile" for edge in drawn["edges"])
    assert any(edge["type"] == "mentions" for edge in drawn["edges"])


def test_two_entries_sharing_a_topic_are_joined():
    store = {"memory": ["Ask @max about this", "@max said no"], "user": []}

    shared = [edge for edge in browse.graph(store, profile="p")["edges"] if edge["type"] == "shares_topic"]

    assert len(shared) == 1 and shared[0]["topic"] == "max"


def test_an_entry_is_excerpted_rather_than_carried_whole():
    store = {"memory": ["x" * 500], "user": []}

    label = [n for n in browse.graph(store, profile="p")["nodes"] if n["type"] == "entry"][0]["label"]

    assert len(label) <= browse.EXCERPT_CHARS


def test_the_graph_pages_over_entries():
    store = {"memory": [f"entry number {i}" for i in range(10)], "user": []}

    first = browse.graph(store, profile="p", offset=0, limit=4)
    last = browse.graph(store, profile="p", offset=8, limit=4)

    assert (first["page"]["returned"], first["page"]["total"], first["page"]["hasMore"]) == (4, 10, True)
    assert (last["page"]["returned"], last["page"]["hasMore"]) == (2, False)


def test_the_graph_is_capped_and_says_when_it_bit():
    store = {"memory": [f"Topic{i} and Topic{i}b" for i in range(200)], "user": []}

    drawn = browse.graph(store, profile="p", limit=200, max_nodes=20)

    assert len(drawn["nodes"]) <= 20 and drawn["truncated"] is True


def test_an_edge_never_names_an_entry_the_caller_was_not_sent():
    store = {"memory": ["Ask @max one", "Ask @max two", "Ask @max three"], "user": []}

    drawn = browse.graph(store, profile="p", offset=0, limit=2)
    ids = {node["id"] for node in drawn["nodes"]}

    for edge in drawn["edges"]:
        assert edge["from"] in ids and edge["to"] in ids


# -- naming an entry to edit -------------------------------------------------


def test_an_edit_prefers_the_text_it_was_given():
    assert browse.find_text(["a", "b"], "b", 0) == "b"


def test_an_index_names_the_entry_at_that_position():
    assert browse.find_text(["a", "b"], None, 1) == "b"


def test_an_index_that_is_gone_names_nothing_rather_than_a_neighbour():
    """A stale index must not delete whatever moved into its place."""
    assert browse.find_text(["a", "b"], None, 5) == ""
    assert browse.find_text(["a", "b"], None, -1) == ""
    assert browse.find_text([], None, 0) == ""


# -- profile scoping ---------------------------------------------------------


class FakeStore:
    memory_char_limit = 2200
    user_char_limit = 1375

    def __init__(self, home):
        self.home = home
        self.memory_entries = [f"a note from {home}"]
        self.user_entries = []
        self.calls = []

    def add(self, target, content):
        self.calls.append(("add", target, content))
        return {"success": True, "target": target}

    def replace(self, target, old_text, new_content):
        self.calls.append(("replace", target, old_text, new_content))
        return {"success": True, "replaced_entry": old_text}

    def remove(self, target, old_text):
        self.calls.append(("remove", target, old_text))
        return {"success": True}


@pytest.fixture
def hermes(monkeypatch):
    """A stand-in for the three Hermes modules this half of the plugin uses."""
    bound = {"home": None, "resets": 0}

    constants = types.ModuleType("hermes_constants")

    def set_override(path):
        bound["home"] = str(path)
        return object()

    def reset_override(_token):
        bound["home"] = None
        bound["resets"] += 1

    constants.set_hermes_home_override = set_override
    constants.reset_hermes_home_override = reset_override

    profiles = types.ModuleType("hermes_cli.profiles")
    profiles.list_profile_names = lambda: ["default", "jurist", "marketing"]
    profiles.get_profile_dir = lambda name: f"/homes/{name}"

    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.profiles = profiles

    memory_tool = types.ModuleType("tools.memory_tool")
    memory_tool.load_on_disk_store = lambda: FakeStore(bound["home"])
    tools = types.ModuleType("tools")
    tools.memory_tool = memory_tool

    for name, module in (
        ("hermes_constants", constants),
        ("hermes_cli", hermes_cli),
        ("hermes_cli.profiles", profiles),
        ("tools", tools),
        ("tools.memory_tool", memory_tool),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    return bound


def test_a_known_profile_resolves_to_its_own_home(hermes):
    import hermie_plugin.memory as memory

    assert str(memory.profile_home("jurist")) == "/homes/jurist"


@pytest.mark.parametrize(
    "name",
    ["../../etc", "jurist/../marketing", "/etc/passwd", "..", ".", "~", "a\\b", "with\0null", "c:name"],
)
def test_a_name_that_is_really_a_path_is_refused(hermes, name):
    import hermie_plugin.memory as memory

    with pytest.raises(memory.ProfileRefused):
        memory.profile_home(name)


def test_a_profile_this_gateway_does_not_have_is_refused(hermes):
    import hermie_plugin.memory as memory

    with pytest.raises(memory.ProfileRefused):
        memory.profile_home("somebody-elses")


@pytest.mark.parametrize("name", [None, "", "   ", " jurist", "jurist "])
def test_an_absent_or_padded_name_is_refused(hermes, name):
    import hermie_plugin.memory as memory

    with pytest.raises(memory.ProfileRefused):
        memory.profile_home(name)


def test_the_store_is_opened_under_that_profile_and_the_override_is_put_back(hermes):
    """The whole of profile scoping, in one assertion each way."""
    import hermie_plugin.memory as memory

    with memory.scoped("/homes/marketing"):
        store = memory.open_store()
        assert memory.entries_of(store)["memory"] == ["a note from /homes/marketing"]

    assert hermes["home"] is None and hermes["resets"] == 1


def test_the_override_is_put_back_even_when_the_body_raises(hermes):
    """Otherwise one failed request leaves another profile's home bound."""
    import hermie_plugin.memory as memory

    with pytest.raises(ValueError):
        with memory.scoped("/homes/jurist"):
            raise ValueError("boom")

    assert hermes["home"] is None and hermes["resets"] == 1


def test_usage_counts_the_delimiter_the_way_the_store_spends_it(hermes):
    import hermie_plugin.memory as memory

    usage = memory.usage_of(FakeStore("/x"), {"memory": ["aaa", "bbb"], "user": []})

    assert usage["memory"] == (len("aaa" + memory.ENTRY_DELIMITER + "bbb"), 2200)
    assert usage["user"] == (0, 1375)


def test_the_delimiter_matches_the_one_hermes_uses():
    """A copy that drifted would split somebody's file in the wrong places."""
    try:
        from tools.memory_tool_store import ENTRY_DELIMITER
    except Exception:
        pytest.skip("hermes is not importable here")
    import hermie_plugin.memory as memory

    assert memory.ENTRY_DELIMITER == ENTRY_DELIMITER


# -- a document as stored -----------------------------------------------------


def test_a_document_carries_the_file_and_the_name_it_has_on_disk():
    answer = browse.raw_document("user", "a line\n§\nanother\n")

    assert (answer["id"], answer["label"]) == ("user", "USER.md")
    assert answer["content"] == "a line\n§\nanother\n", "the delimiters are the point"
    assert (answer["chars"], answer["truncated"]) == (len("a line\n§\nanother\n"), False)


def test_a_document_that_is_cut_still_reports_its_real_length():
    """Otherwise a runaway file reads as small on the one occasion it matters."""
    answer = browse.raw_document("memory", "x" * 50, limit=10)

    assert answer["content"] == "x" * 10
    assert (answer["chars"], answer["truncated"]) == (50, True)


def test_a_document_exactly_at_the_limit_is_not_called_truncated():
    assert browse.raw_document("memory", "x" * 10, limit=10)["truncated"] is False


def test_the_builtin_backend_reads_in_the_order_a_person_reads():
    backend = browse.builtin_backend({"user": "u", "memory": "m"}, editable=True)

    assert [document["id"] for document in backend["documents"]] == ["memory", "user"]
    assert (backend["name"], backend["available"], backend["editable"]) == ("builtin", True, True)
    assert backend["note"] is None


def test_a_file_that_is_not_there_is_absent_rather_than_empty():
    """"There is no file" and "the file is bare" send a person to different places."""
    missing = browse.builtin_backend({"memory": "m"}, editable=False)
    bare = browse.builtin_backend({"memory": "m", "user": ""}, editable=False)

    assert [document["id"] for document in missing["documents"]] == ["memory"]
    assert [document["id"] for document in bare["documents"]] == ["memory", "user"]
    assert bare["documents"][1]["content"] == "" and bare["documents"][1]["chars"] == 0


def test_a_file_that_cannot_be_read_is_named_rather_than_shown_as_empty():
    backend = browse.builtin_backend({"memory": "m"}, editable=False, unreadable=["user"])

    assert "USER.md" in backend["note"]
    assert [document["id"] for document in backend["documents"]] == ["memory"]


def test_the_builtin_backend_is_read_only_when_editing_is_off():
    assert browse.builtin_backend({}, editable=False)["editable"] is False


def test_a_provider_that_is_set_up_is_available_and_says_it_cannot_be_listed():
    backend = browse.external_backend("mem0", label="mem0 (cloud)")

    assert (backend["label"], backend["available"], backend["documents"]) == ("mem0 (cloud)", True, [])
    assert "no call that lists" in backend["note"]
    assert backend["editable"] is False, "no route writes a whole document, for any backend"


def test_a_provider_that_is_not_configured_is_not_available():
    """Configured-and-unlistable is somewhere to look; this is something to do."""
    backend = browse.external_backend("mem0", configured=False)

    assert backend["available"] is False
    assert "not configured" in backend["note"]


def test_a_provider_that_is_not_installed_says_that_instead():
    backend = browse.external_backend("zep", installed=False)

    assert backend["available"] is False and "not installed" in backend["note"]


def test_a_provider_with_no_description_is_labelled_by_its_name():
    assert browse.external_backend("zep")["label"] == "zep"


def test_the_raw_answer_names_the_profile_it_read():
    answer = browse.raw("jurist", [browse.external_backend("mem0")])

    assert answer["profile"] == "jurist"
    assert [backend["name"] for backend in answer["backends"]] == ["mem0"]


def test_the_filenames_are_the_ones_the_store_writes():
    """A copy that drifted would show a file nothing has written since."""
    try:
        from tools.memory_tool_store import MemoryStore
    except Exception:
        pytest.skip("hermes is not importable here")
    import hermie_plugin.memory as memory

    assert {target: MemoryStore._path_for(target).name for target in browse.TARGETS} == memory.RAW_FILENAMES


# -- providers ---------------------------------------------------------------


def test_no_external_provider_claims_to_be_enumerable(hermes, monkeypatch):
    """The interface has prefetch(query) and no listing call. Saying so is the feature."""
    web_memory = types.ModuleType("hermes_cli.web_server_memory")
    web_memory._discover_memory_provider_statuses = lambda: [
        {"name": "mem0", "description": "mem0", "available": True}
    ]
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_memory", web_memory)
    import hermie_plugin.memory as memory

    rows = {row["name"]: row for row in memory.providers()}

    assert rows["builtin"]["enumerable"] is True
    assert rows["mem0"]["enumerable"] is False


def test_the_builtin_is_listed_even_when_nothing_else_can_be(hermes):
    import hermie_plugin.memory as memory

    assert memory.providers()[0]["name"] == "builtin"


def discovering(monkeypatch, rows):
    web_memory = types.ModuleType("hermes_cli.web_server_memory")
    web_memory._discover_memory_provider_statuses = lambda: rows
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_memory", web_memory)


def test_a_raw_backend_is_available_only_where_this_gateway_could_use_it(hermes, monkeypatch):
    """`list` reports what exists; `raw` is asked what is stored, which is narrower."""
    discovering(
        monkeypatch,
        [
            {"name": "mem0", "description": "mem0 (cloud)", "available": True, "configured": True},
            {"name": "zep", "description": "Zep", "available": True, "configured": False},
            {"name": "other", "description": "Other", "available": False, "configured": True},
        ],
    )
    import hermie_plugin.memory as memory

    rows = {row["name"]: row for row in memory.external_backends()}

    assert rows["mem0"]["available"] is True and rows["mem0"]["label"] == "mem0 (cloud)"
    assert rows["zep"]["available"] is False and rows["other"]["available"] is False
    assert all(row["documents"] == [] and row["note"] for row in rows.values())


def test_a_gateway_too_old_to_say_whether_a_provider_is_configured_is_taken_at_its_word(
    hermes, monkeypatch
):
    discovering(monkeypatch, [{"name": "mem0", "description": "mem0", "available": True}])
    import hermie_plugin.memory as memory

    assert memory.external_backends()[0]["available"] is True


def test_the_builtin_is_not_reported_twice_when_the_discovery_names_it(hermes, monkeypatch):
    discovering(monkeypatch, [{"name": "builtin", "available": True}])
    import hermie_plugin.memory as memory

    assert memory.external_backends() == []
    assert [row["name"] for row in memory.providers()] == ["builtin"]


def test_a_gateway_that_cannot_list_providers_still_reads_its_own_files(hermes, monkeypatch):
    """The import is Hermes' private helper; losing it must not lose the answer."""
    broken = types.ModuleType("hermes_cli.web_server_memory")
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_memory", broken)
    import hermie_plugin.memory as memory

    assert memory.external_backends() == []


# -- the advert follows the switches -----------------------------------------


class FakeRuntime:
    def __init__(self, settings=None):
        self.settings = settings or {}

    def config(self, key, default=None):
        return self.settings.get(key, default)


def module_with(settings=None):
    import hermie_plugin.memory as memory

    return memory.MemoryModule(FakeRuntime(settings))


def test_every_capability_is_there_by_default():
    from hermie_plugin import contract

    assert module_with().capabilities() == [
        contract.CAP_MEMORY_BROWSE,
        contract.CAP_MEMORY_RAW,
        contract.CAP_MEMORY_EDIT,
    ]


def test_switching_editing_off_leaves_both_kinds_of_reading():
    from hermie_plugin import contract

    assert module_with({"memory.edit": False}).capabilities() == [
        contract.CAP_MEMORY_BROWSE,
        contract.CAP_MEMORY_RAW,
    ]


def test_switching_browsing_off_takes_the_rest_with_it():
    """An app that cannot list an entry cannot name one to replace or remove.

    Raw reading goes with it for a plainer reason: it is the same two files.
    """
    assert module_with({"memory.browse": False}).capabilities() == []
    assert module_with({"memory.browse": False, "memory.edit": True}).capabilities() == []
