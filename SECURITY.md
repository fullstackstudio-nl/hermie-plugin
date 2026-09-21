# Security

## Reporting

Report a vulnerability privately to **security@fullstackstudio.nl**. Please do
not open a public issue for anything that affects a running gateway.

Include what you can: the Hermes version, the plugin version from
`hermes plugins list`, and what an attacker would gain. You will get an
acknowledgement within three working days.

## What this plugin can reach

It runs **inside** `hermes serve`, in the gateway's own process. It can see what
the gateway sees. That is the trust level; it is the same as the gateway's host,
which is where it runs.

It holds one secret of its own: a VAPID private key it mints on first use,
written `0600` in its own data directory. It is used to sign Web Push requests
and nothing else. It holds no gateway credential, because in-process it needs
none — which is one fewer secret on disk than the external daemon it replaces.

## What it exposes

Nothing inbound. The plugin opens no port and registers no route. The only way
into the notification path is a write to the gateway's profile metadata, which
the gateway itself authenticates.

Outbound it talks to two kinds of address, both of them the device's own:

- `exp.host`, to send an Expo push. No secret is involved; the token is the
  address.
- whatever push endpoint a browser's `PushSubscription` named, over TLS, with an
  encrypted payload only that subscription can open.

## What travels

By default: a bot's name and an event type. Message text travels only when a
device turned previews on **and** the gateway's `push.preview` allows it — the
gateway setting is a ceiling, never a floor.

A notification is never an instruction. An approval notification carries a
request id as a hint; the app re-reads the gateway's open requests and answers
only if that request is still open and still says what the notification said.

## Known and accepted

- **Profile metadata is per profile, not per user.** Everyone with access to a
  gateway can read everyone else's push registrations and context sections on it.
  A registration is a send address; a context section can be a name, a device
  and free text somebody wrote about themselves. On a shared gateway, that is
  shared.
- **Push services see metadata.** Apple, Google and browser push services learn
  that a device received something, when, and from which server. They cannot
  learn what it said.
- **A compromised gateway can do this already.** It could always read every
  transcript. Push adds the ability to make a device buzz.
