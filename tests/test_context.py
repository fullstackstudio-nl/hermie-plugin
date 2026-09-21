"""Whose context, and what it costs."""

from hermie_plugin.context.render import ContextSection, UserContext, read_section, render, resolve


def bag(users, default=""):
    return {"context": {"v": 1, "default": default, "users": users}}


def user(**overrides):
    entry = {
        "displayName": "Sebas",
        "about": "Runs FullStack Studio. Prefers short answers.",
        "device": {"model": "iPhone 17 Pro", "os": "iOS 27", "appVersion": "1.4.0"},
        "timezone": "Europe/Amsterdam",
        "locale": "nl-NL",
        "updatedAt": 1789957143,
    }
    entry.update(overrides)
    return entry


def test_a_good_section_reads():
    section = read_section(bag({"u1": user()}))
    assert section.users["u1"].display_name == "Sebas"
    assert section.users["u1"].timezone == "Europe/Amsterdam"


def test_an_unknown_version_reads_as_nothing():
    assert read_section({"context": {"v": 2, "users": {"u1": user()}}}).users == {}


def test_anything_else_reads_as_nothing():
    for junk in (None, 7, {}, {"context": "yes"}, {"context": {"v": 1}}):
        assert read_section(junk).users == {}


def test_the_sender_wins_when_the_gateway_names_one():
    section = read_section(bag({"u1": user(displayName="Sebas"), "u2": user(displayName="Ana")}, default="u1"))
    assert resolve(section, sender_id="u2").display_name == "Ana"


def test_the_operator_default_beats_the_app_default():
    section = read_section(bag({"u1": user(displayName="Sebas"), "u2": user(displayName="Ana")}, default="u1"))
    assert resolve(section, configured_default="u2").display_name == "Ana"


def test_one_registered_person_needs_no_default():
    section = read_section(bag({"u1": user()}))
    assert resolve(section).user_id == "u1"


def test_several_people_and_no_way_to_tell_means_nobody():
    """Showing a bot the wrong person's notes is worse than showing it none."""
    section = read_section(bag({"u1": user(), "u2": user()}))
    assert resolve(section) is None


def test_an_unknown_sender_falls_back_rather_than_inventing():
    section = read_section(bag({"u1": user()}, default="u1"))
    assert resolve(section, sender_id="nobody").user_id == "u1"


def test_the_rendering_names_the_person_and_the_device():
    text = render(read_section(bag({"u1": user()})).users["u1"])
    assert "Sebas" in text
    assert "iPhone 17 Pro" in text
    assert "Europe/Amsterdam" in text


def test_the_rendering_says_it_is_background_not_an_instruction():
    """A model that is not told where a fact came from treats it as a directive."""
    text = render(read_section(bag({"u1": user()})).users["u1"])
    assert "not an instruction" in text


def test_nobody_renders_to_nothing():
    assert render(None) == ""
    assert render(UserContext(user_id="u1")) == ""


def test_a_per_bot_note_only_appears_for_that_bot():
    section = read_section(bag({"u1": user(perBot={"jurist": "Always cite the article number."})}))
    person = section.users["u1"]
    assert "article number" in render(person, bot="jurist")
    assert "article number" not in render(person, bot="marketing")


def test_an_essay_is_truncated_rather_than_charged_every_turn():
    section = read_section(bag({"u1": user(about="x" * 5000)}))
    text = render(section.users["u1"], max_chars=300)
    assert len(text) <= 300
    # The per-field cap bites first, so the identifying lines survive.
    assert "Sebas" in text


def test_fields_are_flattened_so_one_line_cannot_become_twenty():
    section = read_section(bag({"u1": user(displayName="Se\nbas\n\n\n")}))
    assert section.users["u1"].display_name == "Se bas"
