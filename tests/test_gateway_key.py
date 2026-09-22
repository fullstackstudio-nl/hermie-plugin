"""The name two programs give one gateway without ever comparing notes.

The vector at the bottom is the whole point of the file: the app computes this
string in TypeScript and this plugin computes it in Python, and neither can ask
the other whether it got it right.
"""

from hermie_plugin.push import events, gateway_key
from hermie_plugin.push.registrations import Registration, Section, registration_of


# -- the algorithm -----------------------------------------------------------


def test_it_matches_the_published_vector():
    """Pinned as a literal, because the value's whole job is to be agreed on.

    The same string is asserted in the app's
    `packages/gateway-client/src/gateway-key.test.ts` and in Hermie Web's copy.
    A change here that does not change all three is a device that stops
    recognising its own notifications.
    """
    assert gateway_key.gateway_key_of("https://gateway.example.com:8443") == "bf796761db84e312"


def test_it_is_sixteen_hex_digits():
    key = gateway_key.gateway_key_of("https://gateway.example.com:8443")
    assert len(key) == 16
    assert gateway_key.is_gateway_key(key)


def test_one_gateway_written_two_ways_is_one_key():
    """A path prefix in a configuration, and a host somebody typed in capitals."""
    assert gateway_key.gateway_key_of("https://Gateway.Example.com:8443/hermes") == (
        gateway_key.gateway_key_of("https://gateway.example.com:8443")
    )


def test_a_default_port_is_the_same_gateway_as_no_port():
    """`new URL(...).origin` drops it, so this drops it."""
    assert gateway_key.origin_of("https://gateway.example.com:443") == "https://gateway.example.com"
    assert gateway_key.origin_of("http://gateway.example.com:80") == "http://gateway.example.com"
    assert gateway_key.origin_of("wss://gateway.example.com:443") == "wss://gateway.example.com"


def test_a_port_a_scheme_and_a_host_are_all_separating():
    keys = {
        gateway_key.gateway_key_of(address)
        for address in (
            "https://gateway.example.com:8443",
            "https://gateway.example.com:9443",
            "http://gateway.example.com:8443",
            "https://other.example.com:8443",
        )
    }
    assert len(keys) == 4


def test_a_string_that_is_not_an_address_names_no_gateway():
    """An unreadable address sharing a key with every other one would switch a
    device onto whichever gateway happened to be listed first."""
    for address in ("gateway.example.com", "", "https://", "not a url", None, 7):
        assert gateway_key.gateway_key_of(address) == ""


def test_a_scheme_nothing_connects_on_says_nothing():
    """A browser answers the opaque string "null" here; hashing that would give
    every exotic address one shared key."""
    assert gateway_key.origin_of("ftp://gateway.example.com") == ""
    assert gateway_key.origin_of("hermie://chat/jurist") == ""


def test_an_ipv6_literal_keeps_its_brackets():
    assert gateway_key.origin_of("https://[2001:db8::1]:8443") == "https://[2001:db8::1]:8443"


def test_only_the_shape_a_key_has_is_read_as_one():
    assert gateway_key.is_gateway_key("bf796761db84e312") is True
    assert gateway_key.is_gateway_key("BF796761DB84E312") is False
    assert gateway_key.is_gateway_key("NOTHEX0000000000") is False
    assert gateway_key.is_gateway_key("bf796761db84e31") is False
    assert gateway_key.is_gateway_key(None) is False


# -- where the key comes from ------------------------------------------------


def expo_row(**overrides):
    row = {
        "v": 1,
        "transport": "expo",
        "token": "ExponentPushToken[abcdefghijklmnopqrstuv]",
        "platform": "ios",
        "types": {"message": True},
        "preview": False,
        "updatedAt": 1789957143,
    }
    row.update(overrides)
    return row


def test_a_registration_carries_the_key_its_own_device_computed():
    parsed = registration_of("i1", expo_row(gatewayKey="bf796761db84e312"))
    assert parsed.gateway_key == "bf796761db84e312"


def test_a_row_claiming_something_that_is_not_a_key_carries_none():
    """A row saying its key is "yes" would put "yes" on a notification, and the
    device comparing keys would match nothing — a failure that looks like none."""
    assert registration_of("i1", expo_row(gatewayKey="yes")).gateway_key == ""
    assert registration_of("i1", expo_row(gatewayKey=17)).gateway_key == ""
    assert registration_of("i1", expo_row()).gateway_key == ""


def test_the_device_that_registered_outranks_the_operator():
    """The row is what that device will compare against; the setting is what
    somebody believes the gateway is called."""
    configured = gateway_key.gateway_key_of("https://other.example.com")
    assert gateway_key.key_for("bf796761db84e312", configured) == "bf796761db84e312"


def test_the_configured_origin_answers_for_a_row_that_carries_none():
    configured = gateway_key.gateway_key_of("https://gateway.example.com:8443")
    assert gateway_key.key_for("", configured) == "bf796761db84e312"
    assert gateway_key.key_for(None, configured) == "bf796761db84e312"


def test_nothing_anywhere_is_no_key_rather_than_a_made_up_one():
    assert gateway_key.key_for("", "") == ""
    assert gateway_key.key_for(None, "not-a-key") == ""


def test_the_plugin_setting_beats_the_dashboards_declaration():
    """`dashboard.public_url` is the dashboard's address. Where the gateway is
    reached somewhere else, `push.public_url` is how an operator says so."""
    key = gateway_key.configured_key(
        "https://gateway.example.com:8443", read=lambda: "https://dashboard.example.com"
    )
    assert key == "bf796761db84e312"


def test_the_dashboards_declaration_is_used_when_nothing_overrides_it():
    key = gateway_key.configured_key("", read=lambda: "https://gateway.example.com:8443")
    assert key == "bf796761db84e312"


def test_a_gateway_that_cannot_name_itself_says_nothing():
    assert gateway_key.configured_key("", read=lambda: "") == ""
    assert gateway_key.configured_key("localhost:8443", read=lambda: "also not a url") == ""


def test_hermes_being_absent_is_not_an_error():
    """Every test here runs without Hermes on the path, which is the point."""
    assert gateway_key.dashboard_public_url() == ""


# -- and what travels --------------------------------------------------------


def test_the_payload_carries_the_key_and_omits_an_empty_one():
    note = events.from_assistant_message(
        bot="jurist", session_id="s1", turn_id="t1", assistant_response="hello", at=10
    )
    assert note.payload(preview=False, gateway_key="bf796761db84e312")["gatewayKey"] == (
        "bf796761db84e312"
    )
    assert "gatewayKey" not in note.payload(preview=False)


def test_two_devices_on_two_gateways_get_their_own_key():
    """One person, one bot, two gateways. Each copy must name the one it came
    from, or a tap opens the wrong gateway's chat."""
    note = events.from_assistant_message(
        bot="b", session_id="s", turn_id="t", assistant_response="hello", at=10
    )
    here = Registration(
        installation_id="i1", transport="expo", platform="ios", types={"message": True},
        preview=False, updated_at=1000, token="ExponentPushToken[a]",
        gateway_key="bf796761db84e312",
    )
    there = Registration(
        installation_id="i2", transport="expo", platform="ios", types={"message": True},
        preview=False, updated_at=1000, token="ExponentPushToken[b]",
        gateway_key=gateway_key.gateway_key_of("https://other.example.com"),
    )
    targets = events.recipients(
        note,
        Section(registrations=[here, there]),
        now=1000,
        attached_window_seconds=90,
        enabled_types=events.TYPES,
        gateway_preview="device",
        retired=lambda installation_id, updated_at: False,
    )
    payloads = [
        note.payload(
            preview=preview,
            gateway_key=gateway_key.key_for(registration.gateway_key, ""),
        )
        for registration, preview in targets
    ]

    assert [payload["gatewayKey"] for payload in payloads] == [
        "bf796761db84e312",
        gateway_key.gateway_key_of("https://other.example.com"),
    ]
