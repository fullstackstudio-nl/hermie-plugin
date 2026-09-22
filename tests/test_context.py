"""Whose context, and what it costs."""

from hermie_plugin.context.render import (
    FRAMING,
    ContextSection,
    Orientation,
    UserContext,
    read_section,
    read_sections,
    render,
    resolve,
    same_user,
)


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


def test_an_unknown_sender_never_borrows_the_only_registered_person():
    """A default is somebody's decision; "the only one here" is a guess.

    The field case this comes from is a shared chat: the gateway named a login
    the app has never seen, and the answer must not be the notes of whoever else
    happens to be registered. With a default set the app has said who to assume
    (the test above); with no default there is nobody to assume.
    """
    section = read_section(bag({"u1": user(displayName="Sebas")}))

    assert resolve(section).user_id == "u1", "a gateway that names nobody still uses the one person"
    assert resolve(section, sender_id="somebody-else") is None


def test_the_rendering_names_the_person_and_the_device():
    text = render(read_section(bag({"u1": user()})).users["u1"])
    assert "Sebas" in text
    assert "iPhone 17 Pro" in text
    assert "Europe/Amsterdam" in text


def test_the_rendering_says_it_is_background_not_an_instruction():
    """A model that is not told where a fact came from treats it as a directive."""
    text = render(read_section(bag({"u1": user()})).users["u1"])
    assert "not an instruction" in text


# -- and says where it came from ---------------------------------------------
#
# A bot that is told facts and not told what they are has to be taught by hand
# that the person has a profile, that it lives in their app, and that there is
# more of it elsewhere. That is the one thing this feature must not ask for.


def test_the_rendering_says_where_the_facts_came_from():
    text = render(read_section(bag({"u1": user()})).users["u1"])
    assert "Hermie app" in text
    assert "Hermie plugin on this gateway" in text
    assert "Settings → Context" in text


def test_the_rendering_says_what_may_be_done_with_them():
    text = render(read_section(bag({"u1": user()})).users["u1"])
    assert "address them by name" in text
    assert "timezone and locale" in text
    assert "the device they are on" in text


def test_it_says_what_to_do_when_something_is_missing():
    assert "asking them" in render(read_section(bag({"u1": user()})).users["u1"])


def test_a_pointer_is_given_only_where_it_leads_somewhere():
    """`/me` and the memory browser exist on some gateways and not on others."""
    person = read_section(bag({"u1": user()})).users["u1"]

    plain = render(person)
    assert "/me" not in plain
    assert "memory" not in plain

    both = render(person, orientation=Orientation(command=True, memory=True))
    assert "`/me`" in both
    assert "memory" in both


def test_the_paragraph_is_written_in_one_place():
    """The renderer quotes the sentences; it does not spell them again."""
    wanted = Orientation(command=True, memory=True)
    text = render(read_section(bag({"u1": user()})).users["u1"], orientation=wanted)
    for sentence in wanted.lines():
        assert sentence in text


def test_a_tight_cap_drops_a_whole_sentence_rather_than_cutting_one():
    """Half a sentence about where to look is worse than none of one."""
    person = read_section(bag({"u1": user()})).users["u1"]
    wide = render(person, orientation=Orientation(command=True, memory=True), max_chars=4000)
    assert wide.endswith(FRAMING)

    tight = render(person, orientation=Orientation(command=True, memory=True), max_chars=len(wide) - 60)
    assert len(tight) <= len(wide) - 60
    assert tight.endswith(FRAMING)
    assert "…" not in tight
    assert len(tight.split("\n")) < len(wide.split("\n"))
    for line in tight.split("\n"):
        assert line in wide.split("\n"), "a line was cut rather than dropped"


def test_the_persons_own_words_are_what_the_budget_is_for():
    """The paragraph gives way first; it is the same on every gateway."""
    person = read_section(bag({"u1": user(about="x" * 600)})).users["u1"]
    text = render(person, max_chars=900)

    assert len(text) <= 900
    assert "x" * 600 in text


def test_a_lead_line_is_inside_the_cap_like_everything_else():
    person = read_section(bag({"u1": user()})).users["u1"]
    assert render(person, lead="Read this first.", max_chars=120).startswith("Read this first.")
    assert len(render(person, lead="Read this first.", max_chars=120)) <= 120


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


# -- one key per person ------------------------------------------------------


def test_every_key_contributes_its_person():
    section = read_sections(
        [("u1", bag({"u1": user(displayName="Sebas")})), ("u2", bag({"u2": user(displayName="Ana")}))]
    )
    assert sorted(section.users) == ["u1", "u2"]


def test_the_per_user_key_wins_over_the_legacy_one():
    section = read_sections(
        [
            ("", bag({"u1": user(displayName="Stale"), "u2": user(displayName="Ana")})),
            ("u1", bag({"u1": user(displayName="Sebas")})),
        ]
    )
    assert section.users["u1"].display_name == "Sebas"
    # And the person who has not moved yet is still there.
    assert section.users["u2"].display_name == "Ana"


def test_a_stranger_in_somebody_elses_key_never_beats_their_own():
    section = read_sections(
        [
            ("u1", bag({"u1": user(displayName="Sebas"), "u2": user(displayName="Copied")})),
            ("u2", bag({"u2": user(displayName="Ana")})),
        ]
    )
    assert section.users["u2"].display_name == "Ana"


def test_the_legacy_default_is_the_default():
    section = read_sections(
        [("", bag({"u1": user(), "u2": user()}, default="u2")), ("u1", bag({"u1": user()}, default="u1"))]
    )
    assert section.default_user == "u2"


def test_per_user_keys_that_disagree_name_no_default():
    section = read_sections(
        [("u1", bag({"u1": user()}, default="u1")), ("u2", bag({"u2": user()}, default="u2"))]
    )
    assert section.default_user == ""
    assert resolve(section) is None


# -- the login's provider prefix ---------------------------------------------
#
# The gateway hands out `self-hosted:<uuid>`, `oidc:<sub>` or `basic:<name>`;
# the app registers the bare id `/api/auth/me` returns. One person, two
# spellings, and the section may have been written in either of them.


def test_a_self_hosted_login_finds_the_bare_id_the_app_registered():
    section = read_section(bag({"ef11a9": user(displayName="Sebas"), "ana": user(displayName="Ana")}))
    assert resolve(section, sender_id="self-hosted:ef11a9").display_name == "Sebas"


def test_a_basic_login_finds_the_bare_name():
    section = read_section(bag({"max": user(displayName="Max"), "ana": user(displayName="Ana")}))
    assert resolve(section, sender_id="basic:max").display_name == "Max"


def test_a_bare_sender_finds_the_prefixed_entry_the_app_stored():
    section = read_section(bag({"basic:max": user(displayName="Max"), "ana": user(displayName="Ana")}))
    assert resolve(section, sender_id="max").display_name == "Max"


def test_the_exact_spelling_still_wins():
    section = read_section(
        bag({"oidc:max": user(displayName="Prefixed"), "max": user(displayName="Bare")})
    )
    assert resolve(section, sender_id="oidc:max").display_name == "Prefixed"
    assert resolve(section, sender_id="max").display_name == "Bare"


def test_a_url_shaped_subject_is_never_split_at_its_scheme():
    """`https://…` has a colon that is a scheme, not a provider."""
    section = read_section(
        bag({"//accounts.example.com/12345": user(displayName="Nobody"), "ana": user(displayName="Ana")})
    )
    assert resolve(section, sender_id="https://accounts.example.com/12345") is None


def test_a_url_shaped_subject_matches_itself_whole():
    section = read_section(
        bag({"https://accounts.example.com/12345": user(displayName="Sebas"), "ana": user(displayName="Ana")})
    )
    assert resolve(section, sender_id="https://accounts.example.com/12345").display_name == "Sebas"


def test_the_provider_comes_off_a_url_subject_but_the_scheme_stays_on():
    section = read_section(
        bag({"https://accounts.example.com/12345": user(displayName="Sebas"), "ana": user(displayName="Ana")})
    )
    assert resolve(section, sender_id="oidc:https://accounts.example.com/12345").display_name == "Sebas"


def test_a_prefixed_sender_never_borrows_a_different_persons_entry():
    section = read_section(bag({"ef11a9": user(displayName="Sebas"), "ana": user(displayName="Ana")}))
    assert resolve(section, sender_id="self-hosted:9999") is None


def test_two_providers_are_two_logins_however_alike_the_names_look():
    section = read_section(bag({"basic:max": user(displayName="Max"), "ana": user(displayName="Ana")}))
    assert resolve(section, sender_id="oidc:max") is None


def test_a_bare_sender_that_fits_two_logins_names_nobody():
    section = read_section(
        bag({"basic:max": user(displayName="Basic Max"), "oidc:max": user(displayName="OIDC Max")})
    )
    assert resolve(section, sender_id="max") is None


def test_an_unknown_prefixed_sender_still_falls_back_to_the_default():
    section = read_section(
        bag({"ef11a9": user(displayName="Sebas"), "ana": user(displayName="Ana")}, default="ef11a9")
    )
    assert resolve(section, sender_id="oidc:9999").display_name == "Sebas"


def test_the_prefix_is_only_read_off_something_shaped_like_one():
    assert same_user("self-hosted:ef11a9", "ef11a9")
    assert same_user("max", "basic:max")
    assert not same_user("https://host/12345", "//host/12345")
    assert not same_user("oidc:max", "basic:max")
    assert not same_user(":max", "max")
    assert not same_user("oidc:", "oidc")
    assert not same_user("", "")
