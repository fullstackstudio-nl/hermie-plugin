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

from hermie_plugin.context import FROZEN_SESSIONS, ContextModule
from hermie_plugin.context.render import RETRACTED, SUPERSEDES
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


def test_a_session_that_was_never_frozen_is_unchanged():
    """No section registered, so every turn is a live one. That is the old path."""
    module = module_for(one())

    added = module.on_pre_llm_call(session_id="s1", sender_id="ef11a9")
    assert added is not None and not added["context"].startswith(SUPERSEDES)


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
