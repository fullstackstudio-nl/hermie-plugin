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

No listener of its own. For push, the only way into the notification path is a
write to the gateway's profile metadata, which the gateway itself authenticates.

It does mount three routes on the dashboard's own HTTP server, all under
`/api/plugins/hermie/`:

| Route | Does |
|---|---|
| `GET /memory/list`, `/search`, `/graph`, `/raw` | reads a profile's memory, parsed into entries or as it is stored |
| `POST /memory/edit` | adds, replaces or removes an entry |
| `POST /context/turn` | claims the next turn of a live dashboard session for the caller |
| `PATCH /profiles/{name}` | sets a profile's display name |

**All three sit behind the dashboard's own authentication, and nothing finer.**
Hermes authenticates a dashboard request with process-wide middleware and then
hands every authenticated caller every route it has — core's own included; this
plugin does not add a role, an owner or a permission on top, because there is
nothing on the request to build one from. A signed-in caller is therefore an
operator of these routes exactly as they already are of core's. See **Known and
accepted** below for what that means for each route, and what a switch below
does and does not limit.

Outbound, push talks to two kinds of address, both of them the device's own:

- `exp.host`, to send an Expo push. No secret is involved; the token is the
  address.
- whatever push endpoint a browser's `PushSubscription` named, over TLS, with an
  encrypted payload only that subscription can open.

With `update.check: true` (off by default), one more outbound call: a GET of
this repository's public releases URL, at most once an hour, with nothing
identifying sent.

## The turn claim

`POST /api/plugins/hermie/context/turn` lets the person actually sending a
message in a shared Bot Chat put their own name in front of the model for that
one turn, instead of the chat's opener's — which is what every other source of
identity names, on every turn, until this exists. What it can and cannot do is
the whole of its safety:

- **It can only speak for the caller.** The identity is read off the request's
  own verified dashboard session, never off the body, so nobody can claim a
  turn in somebody else's name.
- **It can only reach a live dashboard session.** The session id has to be a
  key of the dashboard's own session table at the moment of the call — never a
  session key, a durable id, or an id for a session that has since moved on —
  so it cannot touch another platform's conversation, a messaging turn or a
  cron run, at all.
- **It is spent by one model turn and expires in 30 seconds.** Two claims on
  one session inside that window leave the later one standing, and an unspent
  claim is simply dropped, never acted on later.
- **It never overrides a real sender.** It replaces a hook's named sender only
  when that sender is itself a dashboard login — the chat's opener, in
  practice — and leaves a messaging platform's user id or a bot's name exactly
  as Hermes gave it.

So the worst a signed-in caller can do here is put their own context section in
front of somebody else's turn on a dashboard chat both of them can already
reach, for up to 30 seconds. That is the same trust every signed-in caller
already has over every route on this list, not a new one.

## The memory routes and the display-name write

Both are switches per profile, in `config.yaml`, and **all three default to
on**. Turning one off:

```yaml
plugins:
  entries:
    hermie:
      settings:
        memory:
          browse: false   # every /memory/* route refuses with 403
          edit: false     # reading still works; POST /memory/edit refuses
        profiles:
          edit: false     # PATCH /profiles/{name} refuses with 403
```

`memory.browse` gates reading a profile's memory, including `raw`, which reads
the two files as they are actually stored; `memory.edit` gates writing to it and
has no effect while `browse` is off. `profiles.edit` gates the one thing the
display-name route can do — write a presentation label, never the profile's
canonical id, its directory, or anything else in it.

None of the three switches add a role or an owner: they are a way to close a
route on a profile whose operator does not want it reachable at all, not a way
to let some signed-in callers use it and refuse others. The "who may call it"
question is answered once, above, for every route on this list.

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
- **The memory browser trusts the dashboard's own auth, not a role.** Any
  signed-in caller can read and edit any profile's memory through the routes
  above. That is inherited, not invented: Hermes gives a route no identity to
  check and core's own memory routes already work this way. `memory.browse` is
  a way to turn the whole surface off for a profile whose operator does not want
  it reachable at all — which core's own `/api/memory` route does not offer.
- **A signed-in caller can claim any live session's next turn.** See **The turn
  claim** above for exactly what that is bounded to; it is the same trust every
  signed-in caller already has over every route here, not a new exposure.
- **Push services see metadata.** Apple, Google and browser push services learn
  that a device received something, when, and from which server. They cannot
  learn what it said.
- **A compromised gateway can do this already.** It could always read every
  transcript, edit every memory and rename every profile. Push adds the ability
  to make a device buzz.

## Supported versions

| Version | Supported |
|---|---|
| `main` | Yes — fixes land here first |
| Whatever `hermes plugins update hermie` last pulled | Yes, once you have updated |
| Anything older | No |

There is no tagged release archive to pin against: `hermes plugins install` and
`hermes plugins update` both clone or pull the tree at `main`, so keeping current
is `hermes plugins update hermie && hermes gateway restart`.
