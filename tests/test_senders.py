"""The two transports, driven without a network."""

import json

import pytest

from hermie_plugin.push import expo, webpush


# -- Expo --------------------------------------------------------------------


def test_a_mangled_token_is_caught_before_a_round_trip():
    assert expo.is_expo_token("ExponentPushToken[abc]") is True
    assert expo.is_expo_token("ExpoPushToken[abc]") is True
    assert expo.is_expo_token("not-a-token") is False
    assert expo.is_expo_token("") is False


def test_tickets_line_up_with_the_messages_that_produced_them():
    def post(url, body):
        return {"data": [{"status": "ok", "id": "r1"}, {"status": "ok", "id": "r2"}]}

    tickets = expo.send(
        [{"to": "ExponentPushToken[a]"}, {"to": "ExponentPushToken[b]"}], post=post
    )
    assert [t.token for t in tickets] == ["ExponentPushToken[a]", "ExponentPushToken[b]"]
    assert [t.receipt_id for t in tickets] == ["r1", "r2"]


def test_a_dead_device_is_recognised_from_a_ticket():
    def post(url, body):
        return {"data": [{"status": "error", "message": "gone", "details": {"error": "DeviceNotRegistered"}}]}

    ticket = expo.send([{"to": "ExponentPushToken[a]"}], post=post)[0]
    assert ticket.device_gone is True


def test_a_refused_batch_retires_nobody():
    """A request that failed says nothing about whether a device still exists."""

    def post(url, body):
        raise OSError("network is down")

    tickets = expo.send([{"to": "ExponentPushToken[a]"}], post=post)
    assert tickets[0].status == "error"
    assert tickets[0].device_gone is False


def test_a_body_without_tickets_is_not_read_as_success():
    def post(url, body):
        return {"errors": [{"code": "PUSH_TOO_MANY_EXPERIENCE_IDS"}]}

    tickets = expo.send([{"to": "ExponentPushToken[a]"}], post=post)
    assert tickets[0].status == "error"
    assert tickets[0].device_gone is False


def test_receipts_that_have_no_answer_yet_are_simply_absent():
    def post(url, body):
        return {"data": {"r1": {"status": "ok"}}}

    found = expo.receipts(["r1", "r2"], post=post)
    assert set(found) == {"r1"}


def test_the_message_carries_the_payload_and_the_two_visible_strings():
    message = expo.message_for(
        "ExponentPushToken[a]", {"type": "request", "bot": "jurist"}, title="jurist", body="Needs your approval"
    )
    assert message["to"] == "ExponentPushToken[a]"
    assert message["data"]["type"] == "request"
    assert message["channelId"] == "request"
    assert message["title"] == "jurist"


def test_a_batch_over_the_limit_is_refused_rather_than_silently_cut():
    with pytest.raises(ValueError):
        expo.send([{"to": "x"}] * (expo.MAX_BATCH + 1), post=lambda url, body: {"data": []})


# -- Web Push ----------------------------------------------------------------

cryptography = pytest.importorskip("cryptography")


def subscription():
    """A browser's half of the exchange, generated the way a browser would."""
    import os

    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    return key, webpush.b64(webpush.public_key_bytes(key)), webpush.b64(os.urandom(16))


def test_base64url_round_trips_without_padding():
    assert webpush.unb64(webpush.b64(b"\x00\xff\x10")) == b"\x00\xff\x10"
    assert "=" not in webpush.b64(b"abc")


def test_a_key_is_minted_once_and_then_reused(tmp_path):
    path = tmp_path / "vapid.pem"
    first = webpush.load_or_create_key(path)
    again = webpush.load_or_create_key(path)
    assert webpush.public_key_bytes(first) == webpush.public_key_bytes(again)
    assert path.stat().st_mode & 0o777 == 0o600


def test_an_unreadable_key_is_not_overwritten(tmp_path):
    """Overwriting would make every push service treat this sender as new."""
    path = tmp_path / "vapid.pem"
    path.write_text("this is not a PEM file")
    with pytest.raises(Exception):
        webpush.load_or_create_key(path)
    assert path.read_text() == "this is not a PEM file"


def test_the_vapid_header_is_scoped_to_one_push_service(tmp_path):
    import base64

    key = webpush.load_or_create_key(tmp_path / "vapid.pem")
    header = webpush.vapid_header(key, "https://updates.push.services.mozilla.com/wpush/v2/abc", "mailto:a@b.c")

    assert header.startswith("vapid t=")
    token = header[len("vapid t=") :].split(",")[0]
    claims = json.loads(webpush.unb64(token.split(".")[1]))
    assert claims["aud"] == "https://updates.push.services.mozilla.com"
    assert claims["sub"] == "mailto:a@b.c"
    assert claims["exp"] > 0


def test_a_payload_encrypts_and_the_browser_can_open_it(tmp_path):
    """The whole RFC 8291 derivation, checked by decrypting it back."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    user_key, p256dh, auth = subscription()
    body = webpush.encrypt(b'{"type":"request"}', p256dh, auth)

    salt, record_size, id_len = body[:16], int.from_bytes(body[16:20], "big"), body[20]
    sender_public = body[21 : 21 + id_len]
    ciphertext = body[21 + id_len :]
    assert record_size == webpush.RECORD_SIZE
    assert id_len == 65

    # Replay the derivation from the receiving side.
    sender = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), sender_public)
    shared = user_key.exchange(ec.ECDH(), sender)
    key_info = b"WebPush: info\x00" + webpush.public_key_bytes(user_key) + sender_public
    ikm = webpush._hkdf(webpush.unb64(auth), shared, key_info, 32)
    content_key = webpush._hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = webpush._hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)

    plaintext = AESGCM(content_key).decrypt(nonce, ciphertext, None)
    assert plaintext == b'{"type":"request"}\x02'


def test_every_message_gets_fresh_material(tmp_path):
    _, p256dh, auth = subscription()
    first = webpush.encrypt(b"x", p256dh, auth)
    again = webpush.encrypt(b"x", p256dh, auth)
    assert first[:16] != again[:16]  # salt
    assert first[21:86] != again[21:86]  # ephemeral public key


def test_gone_is_gone_and_a_blip_is_not(tmp_path):
    key = webpush.load_or_create_key(tmp_path / "vapid.pem")
    _, p256dh, auth = subscription()

    def answering(status):
        def request(url, body, headers):
            assert headers["Content-Encoding"] == "aes128gcm"
            assert headers["Authorization"].startswith("vapid t=")
            return status, ""

        return request

    assert webpush.send(key, "https://push.example/x", p256dh, auth, {}, request=answering(201)).ok is True
    assert webpush.send(key, "https://push.example/x", p256dh, auth, {}, request=answering(410)).device_gone is True
    assert webpush.send(key, "https://push.example/x", p256dh, auth, {}, request=answering(500)).device_gone is False
