"""Which gateway a notification came from, as a string two programs agree on.

A device can be set up against several gateways at once, and a notification that
says only "researcher" leaves an app with two `researcher`s to choose between.
The app keys its own storage by a random local id, which is exactly the wrong
thing to put on a wire: it is minted on one device and nothing outside that app
has ever seen it. So the wire carries a key derived from the gateway's public
ADDRESS, which both sides can arrive at on their own.

**The algorithm is FNV-1a, 64-bit, over the UTF-8 bytes of the origin**, printed
as 16 lowercase hex digits. It is a specification rather than an implementation
detail, because it already exists three times: in the app's
`packages/gateway-client/src/gateway-key.ts`, in Hermie Web's own copy, and
here. All three prove themselves against one pinned vector,
``https://gateway.example.com:8443`` -> ``bf796761db84e312``, which is what
`tests/test_gateway_key.py` asserts as a literal.

**It is not a secret and it is not a security boundary.** Anything that can
reach a device's push token already knows which gateway it came from. The key
only has to be stable and not to collide: a tap that arrives with a forged one
selects a gateway the owner has already configured and opens a chat on it, which
is the whole of what a notification may do.

## Where the origin comes from, and why that order

Two answers exist and they are asked in this order:

1. **The key the device wrote on its own registration**, `gatewayKey` beside
   `transport` and `token`. The app computes it from the very address that
   device connects to, at the moment it registers, and it is the string that
   device will compare against. So the two sides agree *by construction* — the
   only way this can be wrong is a device that changed its own address, and its
   next registration write carries the new key.
2. **An origin the operator declared**, `push.public_url` in this plugin's
   settings, else Hermes' own `dashboard.public_url` (or
   `HERMES_DASHBOARD_PUBLIC_URL`). Used only for a row that carries no key of
   its own, which is every row written by an app build older than this.

The registration wins, and that is the reversal of the obvious order on purpose.
A configured public URL is what an operator *believes* the gateway is called; a
registration's key is what a device actually *reached*. Where they disagree the
operator's answer would silently stop every device recognising its own
notifications, and the failure would look like nothing at all — a payload with a
key nobody matches is read as "no key" and the app opens whichever gateway is
live. The fallback exists because it is the only answer available for an older
row, and a gateway that can name itself is better than a payload that says
nothing.

Neither answer is required. When there is no key the field is simply absent,
which is exactly how every notification behaved before this existed.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

FNV_OFFSET = 0xCBF29CE484222325
FNV_PRIME = 0x100000001B3
MASK = 0xFFFFFFFFFFFFFFFF

# The schemes an origin is taken over. WHATWG calls these "special" and drops a
# port that is the scheme's default, which is what makes
# `https://gateway.example.com` and `https://gateway.example.com:443` one key.
DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}

_KEY = re.compile(r"^[0-9a-f]{16}$")


def fnv1a64(text: str) -> str:
    """FNV-1a over the UTF-8 bytes, as 16 lowercase hex digits."""
    digest = FNV_OFFSET
    for byte in text.encode("utf-8"):
        digest = ((digest ^ byte) * FNV_PRIME) & MASK
    return f"{digest:016x}"


def origin_of(address: Any) -> str:
    """``scheme://host[:port]`` lowercased, or ``""`` when that is not an address.

    This is `new URL(address).origin.toLowerCase()` for the four schemes an app
    can reach a gateway on, and deliberately nothing else. A path, a query, a
    fragment and any userinfo are dropped — `https://gateway.example.com/hermes`
    and `https://gateway.example.com` are one gateway reached two ways, and a
    path prefix somebody added to a configuration must not stop a device
    recognising its own notifications.

    Two divergences from the browser, both towards saying nothing:

    - a scheme outside the four is ``""`` here, where a browser would answer the
      opaque string ``"null"``. Hashing that would give every unparseable
      address one shared key, which is the one collision that matters;
    - a non-ASCII host is ``""`` unless it survives IDNA encoding, because the
      browser normalises with UTS#46 and Python's codec does not, and a key the
      app cannot reproduce is worse than no key.
    """
    text = str(address or "").strip()
    if "://" not in text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""

    scheme = (parts.scheme or "").lower()
    if scheme not in DEFAULT_PORTS:
        return ""
    try:
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        # A port that is not a number. The address names no gateway.
        return ""
    if not host:
        return ""
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except Exception:
            return ""
    # urlsplit strips the brackets off an IPv6 literal; an origin carries them.
    if ":" in host:
        host = f"[{host}]"
    if port is None or port == DEFAULT_PORTS[scheme]:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def gateway_key_of(address: Any) -> str:
    """The key for one gateway address, or ``""`` when it is not an address.

    An empty answer is deliberate and every caller checks it: a payload carrying
    no key must read as "this names no gateway" rather than as a key that
    happens to match every other unreadable address.
    """
    origin = origin_of(address)
    return fnv1a64(origin) if origin else ""


def is_gateway_key(value: Any) -> bool:
    """Whether this is a string this module could have produced.

    Keys arrive from the app's own `ui_meta`, which is a wire like any other, so
    a row's claim is checked rather than copied onto a notification.
    """
    return isinstance(value, str) and bool(_KEY.match(value))


def dashboard_public_url() -> str:
    """The absolute base URL the operator declared for this gateway, or ``""``.

    `dashboard.public_url` (env `HERMES_DASHBOARD_PUBLIC_URL`) is the one place
    in Hermes where somebody writes down what this machine is called from
    outside. It is the dashboard's URL rather than the gateway socket's, and on
    a Hermie deployment those are the same origin behind the same proxy — where
    they are not, `push.public_url` is the setting that says so.

    Read through core's own resolver so the precedence, the validation and the
    warning about a malformed value are core's rather than a second copy here.
    Absent Hermes, absent config and a malformed value all answer ``""``.
    """
    try:
        from hermes_cli.dashboard_auth.prefix import resolve_public_url  # type: ignore

        return str(resolve_public_url() or "")
    except Exception:
        return ""


def configured_key(configured: Any, *, read=dashboard_public_url) -> str:
    """The fallback key for a registration that carries none of its own.

    *configured* is this plugin's `push.public_url`; `read` is the gateway's own
    declaration. Passed in rather than read here so the whole decision stays a
    pure function of what the gateway said.
    """
    key = gateway_key_of(configured)
    if key:
        return key
    return gateway_key_of(read())


def key_for(registration_key: Optional[str], fallback: str) -> str:
    """The key one device's copy of a notification carries.

    The device's own answer first — see the note at the top of this module — and
    the operator's declaration only where the row is silent.
    """
    if is_gateway_key(registration_key):
        return str(registration_key)
    return fallback if is_gateway_key(fallback) else ""
