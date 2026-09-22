"""An edit made while a chat is open, and when the bot hears about it.

Core renders a plugin's system prompt section ONCE per session and then replays
the bytes it persisted (`agent/system_prompt.py::_frozen_plugin_prompt_sections`
keeps them on `_plugin_system_prompt_sections_snapshot` and re-reads them out of
the stored prompt on a resume). A plugin cannot ask for a re-render; only a
rebuild boundary — a new session, or compaction calling
`invalidate_system_prompt` — clears that snapshot.

So "resolve per turn" cannot be left to the frozen section. The identity is
resolved per turn, which is what `test_session_vars.py` covers; this file covers
the other half, which is that the *content* can go stale under a person who is
still typing into the same chat.

The whole design constraint is that the check is on the agent's own path. It is
one `stat` of `profile.yaml` on a turn where nothing happened, and it reads the
app's metadata only once that `stat` says something moved.
"""

from hermie_plugin import contract
from hermie_plugin.context import FROZEN_SESSIONS, ContextModule
from hermie_plugin.context.render import (
    INTRODUCED,
    RETRACTED,
    RETRACTED_IN_CHAT,
    SUPERSEDES,
    SUPERSEDES_IN_CHAT,
)
from hermie_plugin.context.session_vars import SessionVars


class FakeRuntime:
    """A gateway's worth of surface: the app's bags, and whether they moved.

    `stamp` stands in for `profile.yaml`'s `(mtime_ns, size)`. `reads` counts
    the YAML parses and `stamps` counts the `stat`s, because the cost of this
    feature on a quiet turn is the thing most worth holding still.
    """

    def __init__(self, sections, settings=None, bot="jurist"):
        self.sections = sections
        self.settings = settings or {}
        self.bot = bot
        self.stamp = (1000, 40)
        self.reads = 0
        self.stamps = 0

    def config(self, key, default=None):
        return self.settings.get(key, default)

    def bot_name(self):
        return self.bot

    def app_sections(self):
        self.reads += 1
        return self.sections

    def app_stamp(self):
        self.stamps += 1
        return self.stamp

    def edited(self, sections):
        """The person changed their profile: the file moved and says something new."""
        self.sections = sections
        self.stamp = (self.stamp[0] + 1, self.stamp[1] + 3)

    def touched(self):
        """The file moved for something that is not this: a token, a heartbeat."""
        self.stamp = (self.stamp[0] + 1, self.stamp[1])


def bag(users, default=""):
    return {"context": {"v": 1, "default": default, "users": users}}


def one(**overrides):
    entry = {"displayName": "Sebas", "about": "Prefers short answers."}
    entry.update(overrides)
    return [("", bag({"ef11a9": entry}, default="ef11a9"))]


def module_for(sections, settings=None):
    # `SessionVars(None)` is a gateway whose session variables are out of
    # reach, which is every process that is not `hermes serve`. It keeps the
    # shim out of the way so these tests are about the context and nothing else.
    return ContextModule(FakeRuntime(sections, settings), session_vars=SessionVars(None))


def freeze(module, session_id="s1"):
    return module.render_section({"session_id": session_id, "profile_name": "jurist"})


# -- the change gets through -------------------------------------------------


def test_an_edit_reaches_the_very_next_turn_of_an_open_chat():
    """The decision of 2026-09-21 was per turn, and a frozen section is not."""
    module = module_for(one())
    assert "Prefers short answers." in freeze(module)

    module.runtime.edited(one(about="Wants the long version now."))

    added = module.on_pre_llm_call(session_id="s1", sender_id="ef11a9")
    assert added is not None, "an edit made mid-chat never reached the bot"
    assert "Wants the long version now." in added["context"]


def test_the_newer_copy_says_it_beats_the_one_in_the_system_prompt():
    """Both are in the prompt. A model told nothing would average two profiles."""
    module = module_for(one())
    freeze(module)
    module.runtime.edited(one(about="Wants the long version now."))

    assert module.on_pre_llm_call(session_id="s1", sender_id="ef11a9")["context"].startswith(SUPERSEDES)


def test_an_ungated_gateway_gets_it_too():
    """The single-user install names nobody, and is the install this is for.

    Nothing anywhere says who is asking, so the frozen section resolved the one
    registered person without being told. That is exactly the person whose edit
    has to get through, so "no sender" must not end the turn early.
    """
    module = module_for(one())
    freeze(module)
    module.runtime.edited(one(about="Wants the long version now."))

    added = module.on_pre_llm_call(session_id="s1", sender_id="")
    assert added is not None and "Wants the long version now." in added["context"]


def test_clearing_it_is_said_out_loud_rather_than_left_standing():
    """A frozen section cannot be withdrawn, so the retraction is the only move."""
    module = module_for(one())
    freeze(module)
    module.runtime.edited([("", bag({}))])

    assert module.on_pre_llm_call(session_id="s1", sender_id="ef11a9") == {"context": RETRACTED}


def test_the_bot_note_for_this_chat_changes_too():
    module = module_for(one(perBot={"jurist": "Cite the article number."}))
    assert "Cite the article number." in freeze(module)

    module.runtime.edited(one(perBot={"jurist": "Never cite anything."}))

    assert "Never cite anything." in module.on_pre_llm_call(session_id="s1", sender_id="ef11a9")["context"]


def test_the_superseding_copy_is_bounded_like_every_other_one():
    """The lead line is inside the cap, not glued on outside it."""
    module = module_for(one(about="x" * 600), settings={"context.max_chars": 120})
    freeze(module)
    module.runtime.edited(one(about="y" * 600))

    assert len(module.on_pre_llm_call(session_id="s1", sender_id="ef11a9")["context"]) <= 120


# -- and costs nothing on every other turn -----------------------------------


def test_a_turn_where_nothing_changed_reads_nothing():
    """One `stat`, no YAML. This is the overwhelmingly common turn."""
    module = module_for(one())
    freeze(module)
    reads = module.runtime.reads

    assert module.on_pre_llm_call(session_id="s1", sender_id="ef11a9") is None
    assert module.runtime.reads == reads, "a quiet turn parsed the profile anyway"
    assert module.runtime.stamps == 2, "a quiet turn should cost exactly one stat"


def test_a_file_that_moved_for_something_else_injects_nothing():
    """A push registration or a heartbeat moves `profile.yaml` constantly."""
    module = module_for(one())
    freeze(module)
    module.runtime.touched()

    assert module.on_pre_llm_call(session_id="s1", sender_id="ef11a9") is None


def test_a_file_that_moved_for_something_else_is_looked_at_once():
    """Otherwise every turn for the rest of the session pays for the parse."""
    module = module_for(one())
    freeze(module)
    module.runtime.touched()
    module.on_pre_llm_call(session_id="s1", sender_id="ef11a9")
    reads = module.runtime.reads

    assert module.on_pre_llm_call(session_id="s1", sender_id="ef11a9") is None
    assert module.runtime.reads == reads


def test_the_same_edit_is_not_re_injected_every_turn_afterwards():
    module = module_for(one())
    freeze(module)
    module.runtime.edited(one(about="Wants the long version now."))

    assert module.on_pre_llm_call(session_id="s1", sender_id="ef11a9") is not None
    assert module.on_pre_llm_call(session_id="s1", sender_id="ef11a9") is None


def test_a_rebuild_boundary_re_freezes_and_the_turn_goes_quiet_again():
    """Compaction clears core's snapshot and calls the section again."""
    module = module_for(one())
    freeze(module)
    module.runtime.edited(one(about="Wants the long version now."))

    freeze(module)  # core re-rendered: the prompt now carries the new text
    assert module.on_pre_llm_call(session_id="s1", sender_id="ef11a9") is None


def test_another_person_still_gets_their_own_context():
    """The sender the section does not cover is the path that already existed."""
    sections = [("", bag({"ef11a9": {"displayName": "Sebas"}, "ana": {"displayName": "Ana"}}, default="ef11a9"))]
    module = module_for(sections)
    assert "Sebas" in freeze(module)

    assert "Ana" in module.on_pre_llm_call(session_id="s1", sender_id="ana")["context"]


def test_what_is_remembered_per_session_is_bounded():
    """A gateway up for months sees an unbounded number of session ids."""
    module = module_for(one())
    for index in range(FROZEN_SESSIONS + 20):
        freeze(module, session_id=f"s{index}")

    assert len(module.frozen) == FROZEN_SESSIONS
    assert "s0" not in module.frozen
    assert f"s{FROZEN_SESSIONS + 19}" in module.frozen


def test_a_first_copy_does_not_claim_to_replace_an_empty_one():
    """A section that rendered empty put nothing in the prompt to supersede."""
    module = module_for(one(displayName="", about=""))
    assert freeze(module) == ""

    module.runtime.edited(one(displayName="Sebas", about="Prefers short answers."))

    added = module.on_pre_llm_call(session_id="s1", sender_id="ef11a9")
    assert "Sebas" in added["context"]
    assert not added["context"].startswith(SUPERSEDES)


def test_there_is_nothing_to_retract_when_nothing_was_frozen():
    module = module_for(one(displayName="", about=""))
    freeze(module)

    module.runtime.edited([("", bag({}))])

    assert module.on_pre_llm_call(session_id="s1", sender_id="ef11a9") is None


# -- a chat that began before any of this ------------------------------------
#
# The report this came from: "make sure existing sessions learn this too". A
# session whose system prompt was built before the plugin was installed carries
# no section, and core will never build that prompt again — so a Bot Chat that
# has been open for weeks would stay the one place where the person is a
# stranger.


def test_a_session_whose_prompt_never_had_the_section_is_introduced():
    module = module_for(one())

    added = module.on_pre_llm_call(session_id="s1", sender_id="ef11a9")
    assert added is not None, "a chat that predates the plugin never learned who it is talking to"
    assert "Sebas" in added["context"]


def test_the_introduction_says_it_is_new_here_rather_than_a_correction():
    """There is nothing in this prompt to supersede, and saying so aims at nothing."""
    module = module_for(one())

    added = module.on_pre_llm_call(session_id="s1", sender_id="ef11a9")
    assert added["context"].startswith(INTRODUCED)
    assert not added["context"].startswith(SUPERSEDES)


def test_the_introduction_carries_the_same_framing_as_every_other_copy():
    module = module_for(one())

    added = module.on_pre_llm_call(session_id="s1", sender_id="ef11a9")
    assert "not an instruction" in added["context"]
    assert "Hermie app" in added["context"]


def test_the_introduction_happens_once_and_never_again():
    """Whatever this returns rides the user message, on every turn it fires."""
    module = module_for(one())
    assert module.on_pre_llm_call(session_id="s1", sender_id="ef11a9") is not None

    for _ in range(5):
        assert module.on_pre_llm_call(session_id="s1", sender_id="ef11a9") is None


def test_an_ungated_gateway_is_introduced_too_and_only_once():
    """Nobody is named anywhere, which is the single-user install this is for."""
    module = module_for(one())

    added = module.on_pre_llm_call(session_id="s1", sender_id="")
    assert added is not None and "Sebas" in added["context"]
    assert module.on_pre_llm_call(session_id="s1", sender_id="") is None


def test_an_introduction_names_the_person_who_is_actually_asking():
    """The per-sender rule is the same one the rest of the module follows."""
    sections = [("", bag({"ef11a9": {"displayName": "Sebas"}, "ana": {"displayName": "Ana"}}, default="ef11a9"))]
    module = module_for(sections)

    added = module.on_pre_llm_call(session_id="s1", sender_id="ana")
    assert "Ana" in added["context"]
    assert "Sebas" not in added["context"]


def test_a_gateway_with_nobody_registered_introduces_nothing():
    """A heading with nothing under it teaches a model that the section is noise."""
    module = module_for([("", bag({}))])

    assert module.on_pre_llm_call(session_id="s1", sender_id="ef11a9") is None
    assert module.on_pre_llm_call(session_id="s1", sender_id="") is None


def test_a_person_who_wrote_nothing_is_not_introduced_as_an_empty_heading():
    module = module_for(one(displayName="", about=""))

    assert module.on_pre_llm_call(session_id="s1", sender_id="ef11a9") is None
    reads = module.runtime.reads
    assert module.on_pre_llm_call(session_id="s1", sender_id="ef11a9") is None
    assert module.runtime.reads == reads, "an empty section was worked out again the next turn"


def test_a_frozen_session_is_never_introduced():
    """Core rendered the section into this prompt; there is nothing to introduce."""
    module = module_for(one())
    freeze(module)

    assert module.on_pre_llm_call(session_id="s1", sender_id="ef11a9") is None


def test_an_edit_after_an_introduction_beats_what_was_said_in_the_chat():
    """Not what the system prompt says: on this session it says nothing at all."""
    module = module_for(one())
    module.on_pre_llm_call(session_id="s1", sender_id="ef11a9")
    module.runtime.edited(one(about="Wants the long version now."))

    added = module.on_pre_llm_call(session_id="s1", sender_id="ef11a9")
    assert added["context"].startswith(SUPERSEDES_IN_CHAT)
    assert not added["context"].startswith(SUPERSEDES)


def test_clearing_it_after_an_introduction_is_retracted_where_it_was_said():
    module = module_for(one())
    module.on_pre_llm_call(session_id="s1", sender_id="ef11a9")
    module.runtime.edited([("", bag({}))])

    assert module.on_pre_llm_call(session_id="s1", sender_id="ef11a9") == {"context": RETRACTED_IN_CHAT}


def test_an_introduction_costs_the_same_quiet_turn_afterwards():
    """One `stat` and no read, exactly like a session core froze."""
    module = module_for(one())
    module.on_pre_llm_call(session_id="s1", sender_id="ef11a9")
    reads = module.runtime.reads

    assert module.on_pre_llm_call(session_id="s1", sender_id="ef11a9") is None
    assert module.runtime.reads == reads


def test_what_an_introduction_remembers_is_bounded_like_the_rest():
    module = module_for(one())
    for index in range(FROZEN_SESSIONS + 20):
        module.on_pre_llm_call(session_id=f"s{index}", sender_id="ef11a9")

    assert len(module.frozen) == FROZEN_SESSIONS


# -- the section explains itself ---------------------------------------------


def test_the_frozen_section_says_where_it_came_from():
    """The report: "right now I have to teach a bot that it must look in the plugin"."""
    text = freeze(module_for(one()))

    assert "Hermie app" in text
    assert "Hermie plugin on this gateway" in text
    assert "Settings → Context" in text


def test_the_command_is_named_only_once_this_gateway_took_it():
    module = module_for(one())
    assert "/me" not in freeze(module)

    module.command_registered = True
    assert "`/me`" in freeze(module, session_id="s2")


def test_the_memory_browser_is_named_only_where_it_will_answer():
    assert "memory" in freeze(module_for(one()))
    assert "memory" not in freeze(module_for(one(), settings={"modules.memory": False}))
    assert "memory" not in freeze(module_for(one(), settings={"memory.browse": False}))


def test_the_capability_says_the_section_explains_itself():
    assert contract.CAP_CONTEXT_ORIENTATION == "context.orientation"
    assert contract.CAP_CONTEXT_ORIENTATION in module_for(one()).capabilities()


def test_a_file_that_moved_for_something_else_says_nothing_after_an_introduction():
    """A push registration or a heartbeat moves `profile.yaml` constantly."""
    module = module_for(one())
    module.on_pre_llm_call(session_id="s1", sender_id="ef11a9")
    module.runtime.touched()

    assert module.on_pre_llm_call(session_id="s1", sender_id="ef11a9") is None


def test_the_introduction_is_bounded_like_every_other_copy():
    """Including the line in front of it, which is inside the cap, not glued on."""
    module = module_for(one(about="x" * 600), settings={"context.max_chars": 200})

    assert len(module.on_pre_llm_call(session_id="s1", sender_id="ef11a9")["context"]) <= 200
