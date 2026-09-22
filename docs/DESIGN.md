# Design

This is the gateway-side half of Hermie. It runs inside `hermes serve` as a
Hermes plugin, it has no inbound network surface of its own, and it holds no
credential the gateway did not already hold.

It replaces the `hermie-web --push` daemon as the default path. The daemon needed
a second process, a second credential and a WebSocket connection of its own, and
it kept every Bot Chat resident on the gateway because a watched session is a
pinned session. A plugin is inside the process that already has all of that.

---

## 1. What Hermes actually offers a plugin

Everything below was read out of Hermes 0.21.x on the test VM and exercised
against a live gateway. The parts that do **not** exist matter more than the
parts that do, because ADR-0017 assumed four event types and only two of them
have a hook.

### Hooks that exist and are used

| Hook | Fires | Kwargs this plugin reads |
|---|---|---|
| `post_llm_call` | once per turn, after the model answered | `session_id`, `turn_id`, `assistant_response`, `platform` |
| `on_session_end` | once per turn, at the end | `session_id`, `turn_id`, `completed`, `failed`, `interrupted` |
| `pre_approval_request` | an agent stopped to ask for approval | `surface`, `session_key`, `description`, `request_id`, `turn_id` |
| `pre_tool_call` | before every tool call | `tool_name`, `args`, `session_id`, `tool_call_id` |
| `pre_llm_call` | before every model call | `session_id`, `sender_id` |

Dispatch is signature-inspected: a callback declaring `**kwargs` receives the
whole payload and keeps receiving fields that are added later, while a narrow
signature silently stops seeing new ones. Every callback here takes `**kwargs`,
and `hermes plugins doctor` checks for exactly that.

`pre_tool_call` is a **policy** hook: it fails closed, so a callback that hangs
blocks the tool. This plugin's does nothing but read four fields and hand a
value to a queue.

### Hooks that do not exist

- **No cron hook.** There are no hook fire sites anywhere in `cron/`. A cron run
  is an ordinary agent session, so the turn hooks fire inside it — and, it turns
  out, they carry enough to recognise one without guessing. §3 has the details.
  What is still missing is the scheduler's own verdict: an exception out of
  `run_job`, a delivery that failed, a quota hold, the `failure_streak` and
  `last_status` on the job record and the executions ledger are all written
  after the agent is gone, and none of them fires anything.
- **No bot-to-bot message hook.** `tools/bot_mode_dm.py` has no fire site, so
  one bot writing to another **cannot be produced by a plugin**. ADR-0017 listed
  it as a notification type; there is no such type here, because a switch that
  turns nothing on is worse than no switch. This is the one place where the
  plugin is strictly less capable than the daemon ADR-0017 described, and it is
  a gap in Hermes, not in the design.
- **No clarify hook.** `clarify` is an ordinary tool, so it is caught through
  `pre_tool_call`. The clarify request id is minted inside the gateway's
  blocking prompt and is not visible to a plugin, so a clarify notification
  carries no id: the app opens the chat and finds the open question itself.
- **No client-presence list.** `session.active_list` reports the calling
  connection's own session and nothing about anybody else's, exactly as
  ADR-0017 found. Suppression therefore stays the `seen` heartbeat the app
  writes. Nothing here changes that.

### Other surfaces

- **`ctx.register_system_prompt_section(id, content, position, max_chars)`** —
  bounded text frozen into a session's system prompt, rendered once per session
  and persisted by core verbatim. Limits: `after_memory` is the only accepted
  position, 4000 characters per section, 8000 across all plugins, 32 sections.
  The mapping a callable receives carries `session_id`, `model`, `provider`,
  `platform`, `profile_name`, `cwd` — **and no user identity**. Core builds it
  from the agent object alone (`agent/system_prompt.py::_plugin_session_info`),
  so there is no field to add one. The section is rendered on the caller's own
  thread, after the gateway has bound the session variables for the turn, so
  the *session* user is reachable from inside it even though the mapping never
  names one.
- **`ctx.state`** — a per-profile JSON store at
  `$HERMES_HOME/plugin-data/<namespace>/state.json`, atomic, file-locked,
  10 MiB quota, `get`/`set`. This is where the plugin's state lives.
- **`ctx.get_config(key, default)`** — reads
  `plugins.entries.hermie.settings.<key>`. The manifest's `config_schema`
  validates types and warns, but **Hermes never merges a schema `default` into
  the config**, so every default that applies is the one at the call site.
- **`ctx.profile_name`** — the active profile. A Hermie bot *is* a Hermes
  profile, so this is the bot's name.
- **`ctx.spawn_task(coro)`** — a supervised asyncio task, cancelled on unload.
  It needs a running loop, so it is not usable from a synchronous hook; the
  sender here is a plain daemon thread instead.
- **HTTP routes** — possible, on the dashboard web server, and not used. A
  plugin shipping `dashboard/manifest.json` with an `"api"` key gets its
  module-level FastAPI `router` mounted at `/api/plugins/<name>/`
  (`hermes_cli/web_server_dashboard.py::_mount_plugin_api_routes`, called once at
  import from `hermes_cli/web_server.py`). `register(ctx)` has nothing to do with
  it: there is no `register_route` on the plugin context, and the two mechanisms
  are disjoint. Three properties of that surface, checked against 0.21.3, are why
  the advert lives in `ui_meta` instead:

  - **Auth is binary and anonymous.** There are no per-route dependencies —
    no `Depends(...)` anywhere in `hermes_cli/` — only middleware, which either
    401s the request or lets it through. The loopback credential is a
    process-ephemeral shared token (`X-Hermes-Session-Token`), not a person. On a
    gated deployment a handler *can* read `request.state.session`, but there is
    no role, no admin flag and no ownership model, so **any authenticated caller
    can reach any route**. A route cannot refuse a user; there is nothing to
    refuse them by.
  - **There is no profile scoping.** A plugin handler runs under the dashboard
    process's `HERMES_HOME` whatever profile the caller meant. Core's own routes
    opt in by declaring a `profile` parameter and wrapping the body in
    `web_server_profiles._config_profile_scope`, which is private; the shipped
    plugins do not, and the kanban plugin carries a comment saying the mismatch
    is a known hazard. A multiplexed gateway therefore needs a plugin to
    reimplement core's scoping against core's private helpers.
  - **It is a second address.** Which is what ADR-0017 refused, and it is still
    the smaller point next to the first two.

  So anything the app needs goes through `ui_meta`, over the connection it
  already has — **except the memory browser**, which cannot: it is a
  request/response surface over far more data than a profile file should carry,
  and the WebSocket has no memory method to borrow (`profiles.remember_onboarding`
  writes USER.md on the default profile with a fixed key set, and `/memory` over
  `slash.exec` is the write-approval queue, not a reader). §9 is what that costs
  and what holds it in.
- **No identity, anywhere in the plugin API.** No hook kwarg, no prompt-section
  field and no context object names the person a dashboard session was admitted
  for, even though the gateway stamped it on the session record. §4 explains
  what the plugin does about that and what it costs.
- **`gateway.session_context`** — the session variables tools read: `ContextVar`s
  named after the old `HERMES_SESSION_*` environment variables, read through
  `get_session_env(name)` (the variable if it was ever bound here, else the real
  environment). `set_session_vars(...)` binds them all at once and
  `clear_session_vars` blanks them; there is no public setter for one variable,
  so writing one means the `_VAR_MAP` entry `get_session_env` itself reads.
- **`ui_meta` is not part of the plugin API.** It lives in `profile.yaml`, as
  `ui_meta: {key: value}` beside `_ui_meta_revisions: {key: int}` (the gateway's
  per-key compare-and-swap). A plugin reaches it by reading that file.

---

## 2. The contract, because nobody updates a plugin

A gateway that was set up once and works is a gateway nobody logs into again.
Every future version of the app will meet plugins older than itself, so the app
must ask rather than assume.

The plugin writes a versioned advert into the gateway's own `ui_meta`, under its
own **`hermie-plugin`** key:

```yaml
hermie-plugin:
  v: 1
  version: 0.3.0
  capabilities: [command.me, context.system_prompt, push.expo, push.mute,
                 push.preview, push.seen.per_chat, push.type.turn_done,
                 push.type.turn_failed, push.webpush, ui_meta.per_user]
  modules: {push: "on", context: "on", presence: planned, ...}
  limits: {payloadBytes: 3500, contextChars: 1200}
  updatedAt: 1790001453
```

Four rules make this work in both directions:

1. **A capability is a string, not a version comparison.** The app tests for
   `"push.webpush"`. It never tests `version >= "0.4.0"`. A newer plugin adds a
   string; an older app does not ask for it. `version` exists only for the
   human-readable "a plugin update is available" line.
2. **A capability is claimed only when it can be honoured *on this gateway*.**
   `push.webpush` is advertised only when the signing library imports here, and
   `push.type.turn_done` only when that type is switched on. An app that sees a
   capability will offer a button, and a button that cannot work is worse than
   one that is absent.
3. **An absent advert means an absent plugin.** A plugin too old to write the
   key, a plugin that is disabled, and no plugin at all are indistinguishable,
   and all three mean: do not offer the feature.
   Two capabilities exist because of that rule rather than to offer a button:
   `ui_meta.per_user` says this gateway reads `hermie-app:<user id>`, and
   `push.seen.per_chat` says it understands a heartbeat that names a chat. An
   app that moves its bag or changes its heartbeat shape in front of a plugin
   that predates the move fails **silently** — nobody is notified, or nothing is
   suppressed — so the app asks first and writes the older shape until the
   string is there.
4. **The plugin never writes `hermie-app`.** That key belongs to the app, which
   holds a compare-and-swap revision for it; a write from behind would make the
   app's next write fail. The plugin reads it and publishes under its own key,
   whose revision no app version touches.

The advert is removed on unload. A gateway that is killed rather than unloaded
leaves it behind, which is what `updatedAt` is for.

### The keys the app writes, and the move to one per person

The app used to keep everything in one shared `hermie-app` key. It is moving to
**one key per person**, `hermie-app:<user id>`, where `<user id>` is the gateway
identity the app resolved for the signed-in person — `owner` on a token gateway.
Push registrations and the `context` section are written there from now on.

The plugin reads **both**, for one version:

| | |
|---|---|
| `hermie-app:<user id>` | preferred, and it names the person it describes |
| `hermie-app` | still read, names nobody, loses every tie |

Merging is in one fixed order — legacy first, then the per-user keys sorted —
and the order *is* the precedence. Two consequences, and both are the point:

- **A device is a device.** The same installation id under two keys is one
  registration, the per-user one. While the app writes both during the
  migration, nobody is notified twice.
- **A person is whoever their own key says.** A `context` entry for `u1` found
  under `hermie-app:u2` is read — an app mid-migration may well have copied a
  whole bag across — but it never beats what `hermie-app:u1` says about `u1`.

The section-wide context `default` is taken from the legacy bag, because a
per-user bag can only sensibly name itself. Without a legacy bag, per-user bags
that agree on one name set it and per-user bags that disagree set nothing, which
falls through to "the only registered person" and then to nobody.

Which person a *hook* is about is unchanged: `sender_id` when the gateway named
one, then `context.default_user`, then the app's own `default`, then the only
registered person. What changed is that a registration now knows whose it is,
because it knows which key it came from — that is what `mutes` and the per-chat
`perBot` switches are checked against, and what the push module means by "every
device of a user".

The plugin still writes none of these keys. `write_key` refuses `hermie-app` and
every `hermie-app:<user id>` by the same rule and for the same reason: they carry
the app's compare-and-swap revision.

### Modules

One plugin, several modules, because a person installs a plugin once — asking
them to install five is asking them to install none.

| Module | State | What it does |
|---|---|---|
| `push` | ships, on by default | notifications |
| `context` | ships, on by default | per-device context in the system prompt |
| `sessions` | planned | per-user sessions |
| `presence` | planned | who is watching a chat |
| `transcripts` | planned | transcript cache for a fast chat open |
| `search` | planned | search index with row ids |
| `attachments` | planned | attachment catalogue |
| `usage` | planned | usage statistics |

A planned module is named, has its config key reserved, and is advertised as
`planned`. So the app can tell "too old" from "switched off" from "not built
yet", and the config surface does not change shape when one lands.

### Upgrades

`hermes plugins update hermie` exists and git-pulls the installed tree, so the
path is one command:

```
hermes plugins update hermie
hermes gateway restart
```

There is no plugin-driven self-update, and there will not be: a plugin cannot
run `hermes plugins install` for itself without shelling out as the gateway
user, and a plugin that can rewrite its own code is a plugin that can rewrite
its own code. What the plugin does instead is make the advert enough to decide
with.

| Field | Says |
|---|---|
| `version` | this build's release version |
| `maxContract` | the newest advert shape this build can write (equal to `v` today) |
| `minAppVersion` | the oldest app this build can serve |
| `source.repo` | which repository an update comes from |
| `source.ref` | the commit the installed tree sits at |
| `source.latest` | the newest release tag — **only** when the check is switched on |

`minAppVersion` is the one version comparison in this contract and it runs the
other way from §2's rule. The app must never test the plugin's version, because
a plugin older than the string it wants simply does not publish it. A plugin
naming a floor is different: it is the only way to tell somebody on a very old
app why nothing works — accepting that an app too old to read the field was
never going to be told anything anyway.

`source.ref` is read from `.git/HEAD` and the ref it names, with no subprocess
and no network, because `hermes plugins install` clones the repo and so the
installed tree is a checkout. Loose refs, packed refs and the detached HEAD that
`--ref <sha>` leaves behind all answer.

**Asking the repository what the newest release is, is off by default.** The
advert already carries the version and the commit, so an app can work out that
an update exists on its own network, and a gateway that makes an unprompted
outbound request is a surprise on a product whose pitch is that it has no relay,
no account and no credential. `update.check: true` switches it on: one GET of a
public URL, at most once an hour (cached in the state file, so hourly means
hourly across restarts), nothing identifying sent, and every failure — including
no outbound route at all — is cached as "no newer release" rather than retried
on every load. The capability `plugin.update_check` is advertised only when an
answer actually came back.

There is no HTTP route for this. Hermes can mount one for a plugin, on the
dashboard server, and §1 says why this plugin does not use it; a version string
the advert already carries is not a reason to open an address.

What an upgrade must never cost is state. `state.py` carries a version and a
migration table; a state file from a version this build does not know is **left
on disk untouched** and treated as empty for the run, because losing dedupe
history costs one duplicate notification while overwriting a newer file costs a
downgrade its data. Version 2 added the update-check cache, and the test that
matters for it is not that the new key appeared but that `sent` and `retired`
came through unchanged — a lost retirement talks to a dead device again, and a
lost claim buzzes somebody twice about a message they already read.
Registrations are not the plugin's state at all — they live in the app's
`ui_meta` and survive any plugin change, including removal.

---

## 3. Push

### From hook to notification

| Event | Hook | Type |
|---|---|---|
| a bot wrote something | `post_llm_call` | `message` |
| a turn finished | `on_session_end` (`completed`) | `turn_done` |
| a turn failed | `on_session_end` (`failed`/not completed) | `turn_failed` |
| a turn was interrupted | `on_session_end` (`interrupted`) | *nothing — somebody pressed stop* |
| approval requested | `pre_approval_request` (not `surface: smart`) | `request` |
| a question asked | `pre_tool_call` (`tool_name == "clarify"`) | `request` |
| cron delivered | `post_llm_call` inside a cron run | `cron` |
| a cron job declared its own failure | `post_llm_call`, `[CRON_FAILURE]` on the first line | `cron_failed` |
| a cron run's turn finished | `on_session_end` (`completed`) inside a cron run | `cron_done` |
| a cron run's turn failed | `on_session_end` (`failed`/not completed) inside a cron run | `cron_failed` |

### Recognising a cron run

Hermes fires no cron hook, so this is a question the plugin answers for itself.
It used to answer it by looking for "cron" in the session's `platform` string,
which is free text. Two better signals ride the same turn, and core prefers
both of them over the platform string — `tools/approval.py` says so in as many
words, because cron binds the platform for delivery routing only.

| Asked | What it is | A fact? |
|---|---|---|
| `task_id` | the scheduler mints `cron:<job id>:<execution id>` | yes, and it is the only place a **job id** reaches a hook |
| `HERMES_CRON_SESSION` | bound to `"1"` for the run, `""` outside it | yes; this is the test core's own unattended-approval check uses |
| `session_id` | opened as `cron_<job id>_<stamp>` | yes |
| `platform` | free text | no — the last resort it always was |

Reading the session variable works inside a bounded hook: Hermes runs a
callback through `contextvars.copy_context().run(...)`, and a copied context
carries the values it was copied from. It is *writing* one that is lost, which
is the whole limit of the session-variable shim in §4.

The payload says which kind of answer it got, as `cronCertain`, so an app can
label a notification "scheduled job" and mean it. `push.cron.signal` advertises
that this gateway has the marker at all.

**What a turn cannot see is whether the JOB failed.** It can see whether the
*turn* failed, and it can see the `[CRON_FAILURE]` marker the agent wrote on
its own first line. Everything else the scheduler decides — after the agent is
gone, with no hook — so `cron_failed` means "this run's turn failed, or the
agent said it failed", which is a subset of "this job failed". Closing that gap
needs a fire site in `cron/scheduler.py`, which is a change to Hermes.

### Dedupe

Every notification has an id derived from the identity of the fact, not from a
counter: `sha256(kind, session, request/turn id)`. The same approval described
twice collides on purpose; a finished turn and a failed turn on the same turn id
do not. Claims live in the state file for 24 hours.

### Suppression

A `message` is suppressed **on the device that is reading that chat**, and only
there. The app writes a heartbeat per device saying which bot it has open:

```yaml
hermie-app:owner:
  push:
    seen:
      <installation id>: {bot: jurist, at: 1789957143}
```

A device is skipped when its own heartbeat is within the window (90s by
default) and names this bot, after a short delay (5s) that lets an opening app
claim the chat. Every other device of the same person is still notified — a
phone in a pocket should buzz while the same person reads that chat on a
laptop — and a device reading a *different* bot is notified too.

A heartbeat that is a bare number is the older shape: it says a chat was open
without saying which, so it suppresses every chat on that one device. It is read
for one version. A heartbeat may also ride the registration entry itself, as
`seen` beside `transport` and `token`, for an app that keeps a device's "what am
I looking at" next to the device; where both exist the **newest** wins, since
the question is whether somebody is looking *now*.

`request`, `cron` and `turn_failed` are **never** suppressed. This is the
heuristic ADR-0017 described and it still fails towards a redundant notification
for a chat somebody is already reading, which is the right direction — it is now
one notification on one device rather than silence on all of them.

### Mutes

A person can silence a bot. The app writes it into that person's own bag, beside
`push` and `context`, because a mute is a fact about a person and a bot rather
than about a transport:

```yaml
hermie-app:owner:
  mutes:
    jurist: 0             # forever
    marketing: 1790000000 # until this unix second
```

`0` is forever. An `until` that has already passed is **not** a mute: the app is
not obliged to come back and tidy up a lapsed entry, and a gateway that read a
lapsed one as live would go quiet for good. A value that is not a whole
non-negative number is ignored, which leaves the bot notifying — the recoverable
direction.

A mute outranks every per-type switch, including the types that are never
suppressed: `request`, `cron` and `turn_failed` are silent too while it holds.
Suppression is a guess about whether somebody is already reading; a mute is
somebody saying no, and the two should not be weighed against each other.

It covers **every device that person registered**, and only that person's — which
is exactly what the per-user key made knowable, since a registration now carries
the user id of the key it was read from. A mute in the legacy `hermie-app` bag
applies to the devices still registered there and to nothing else.

The gateway advertises `push.mute` whether or not a mute exists yet: the string
says this gateway will obey one, which is what the app needs before it offers
the switch. A copy of the map under `push.mutes` is read as well, for an app
that files it with the rest of the push settings; the top-level one wins.

### The switches a device carries

A registration's `types` bag is read over **every** type this plugin can send
— the seven in the hook table at the top of this section:

```yaml
hermie-app:owner:
  push:
    registrations:
      <installation id>:
        types: {message: true, request: true, cron: true,
                cron_done: true, cron_failed: true,
                turn_done: true, turn_failed: true}
```

There is one list of type names and both halves use it — `PUSH_TYPES` in
`push/registrations.py`, which `push/events.py` re-exports as `TYPES`. It was
two tuples once and they had drifted: the sender knew about `cron_done` and
`cron_failed`, the reader did not, and a row asking for either was parsed as
asking for nothing. Two types this gateway advertises and can send were
unreachable from a device, which is the kind of gap nobody reports because it
looks like silence.

**An absent type is OFF, and it is never inferred from a neighbour.** A device
registered before the app had the two cron switches keeps exactly the behaviour
it has today; nothing starts buzzing because a gateway was updated. In
particular `cron` does not imply them: that one is "a scheduled job delivered
something", which is a different question from "a job you were not watching
ended". The app supplies the other direction — its two new switches default to
on — so a device gets them when it next writes its row.

Above all of this sit the two rules that do not move: `push.types` is the
gateway-wide ceiling a device cannot switch its way past, and a mute is the
strongest word in the room.

### Per-chat switches

A device's registration carries a switch per type, and a person can override any
of them for one chat. The app writes those overrides beside the registrations,
because the decision belongs to the READER rather than to a device — somebody
who silences one bot's cron deliveries means it on their phone and on their Mac:

```yaml
hermie-app:owner:
  push:
    perBot:
      jurist: {cron: false}
      marketing: {turn_failed: true}
```

The bag is **partial on purpose**. A type it does not name follows the global
switch *as the global switch moves*; a full copy of every type would freeze each
one at whatever it happened to be on the day somebody touched one of them. The
fold is `effective_types` in `push/registrations.py`, written as the Python half
of `effectivePushTypes` in the app's `packages/gateway-client/src/push.ts` — the
app's switch screen and this gateway's decision must not be two rules that
merely happen to agree today.

An override is honoured only when it is a boolean; anything else is not an
answer and leaves the global switch standing. An override cannot turn on a type
`push.types` has switched off gateway-wide, which is the same ceiling every
device setting meets.

**A mute still outranks it.** An override is a preference about a type; a mute
is somebody saying no to the bot. The two are not weighed against each other,
and a chat with `{message: true}` on a muted bot is silent.

The section version is deliberately **not** bumped for `perBot`. `v` is checked
per ROW and an unreadable row is DROPPED, so a bump would not protect the key
from an older notifier — it would unregister the device and make the phone go
quiet. `push.per_bot` in the advert is what says this gateway reads it, the same
way `push.mute` says a mute will be obeyed.

### Payload

Bot name and event type. Nothing else, unless the device turned `preview` on
**and** the gateway allows it — the gateway setting is a ceiling, never a floor.
`preview: never` overrides a device that asked; `preview: device` never turns one
on. Payloads are kept under ~3.5 KB because APNs caps around 4 KB.

| Field | When | What |
|---|---|---|
| `v`, `type`, `bot`, `at`, `eventId` | always | the shape, the kind, the bot, the second, the dedupe id |
| `sessionId` | where known | the session the turn happened in |
| `sessionKind` | where readable | `canonical` \| `branch` \| `other` |
| `gatewayKey` | where known | which gateway sent it |
| `requestId` | approvals | the request to re-read |
| `cron`, `cronCertain`, `jobId` | cron runs | that it was scheduled, how sure, and which job |
| `preview` | opt-in | the text |

An approval payload carries `requestId`. That is a hint, not an instruction:
tapping Allow opens the app, which connects to the gateway, re-reads the open
requests, and responds only if that request is still open and still says what
the notification said. A forged "Allow" for a destructive command opens an app
that finds no such request and says so.

`cron`, `cronCertain` and `jobId` ride every notification raised inside a
scheduled run — including an **approval or a question**, which stay `request`
notifications because somebody is still being asked. Those two hooks carry no
`task_id`, so the answer there comes from `HERMES_CRON_SESSION` or from a
`cron_…` session id; core fills in `session_id` on an approval hook when the
context has one.

### Which gateway sent it

A device can be set up against several gateways, and a notification saying only
"researcher" leaves an app with two `researcher`s to choose between. The app
keys its own storage by a random local id, which is exactly the wrong thing to
put on a wire: it is minted on one device and nothing outside that app has ever
seen it.

So the payload carries a key derived from the gateway's public ADDRESS:
**FNV-1a, 64-bit, over the UTF-8 bytes of the origin, as 16 lowercase hex
digits.** The algorithm is the artefact rather than the implementation — it
exists in the app's `packages/gateway-client/src/gateway-key.ts`, in Hermie
Web's own zero-dependency copy, and here — and all three prove themselves
against one pinned vector:

```
https://gateway.example.com:8443  ->  bf796761db84e312
```

The ORIGIN and not the address, so a path prefix somebody added to a
configuration does not stop a device recognising its own notifications. A
default port is dropped the way `new URL(...).origin` drops it. A string that is
not an address answers `""`, which every caller reads as "this names no
gateway" — the one collision that would matter is an unreadable address sharing
a key with every other unreadable one.

**Where the origin comes from, in order:**

1. **the key the device wrote on its own registration** — `gatewayKey` beside
   `transport` and `token`, computed by the app from the very address that
   device connects to. Both sides then agree *by construction*;
2. **an origin the operator declared** — `push.public_url`, else Hermes'
   `dashboard.public_url` (env `HERMES_DASHBOARD_PUBLIC_URL`), read through
   core's own resolver so the precedence and the validation are core's.

The registration wins, and that reversal of the obvious order is the whole
decision. A configured public URL is what an operator *believes* the gateway is
called; a registration's key is what a device actually *reached*. Where they
disagree the operator's answer would silently stop every device recognising its
own notifications, and the failure would look like nothing at all. The fallback
exists because it is the only answer available for a row written before the app
carried one.

A row's claim is **checked, not copied**: a `gatewayKey` that is not sixteen hex
digits is read as absent. A payload with no key is what every notifier before
this sent, and the app reads it the way it always did — open that chat on the
gateway that is live.

It is not a secret and not a security boundary. Anything that can reach a
device's push token already knows which gateway it came from, and a tap arriving
with a forged key selects a gateway the owner has already configured.

### Which conversation it belongs to

A bot used to have exactly one chat, so naming the bot named the conversation.
The app now branches a conversation and retires the one `/new` puts away, so a
turn can happen in a session nobody is looking at and a tap has to land on the
right one. `sessionId` was always there; `sessionKind` is what lets the app
choose a destination before it has resolved anything.

Upstream's session row has no parent field and no kind field — the app says so
in `features/sessions/session-model.ts` and classifies by title. This reads the
same three titles out of the same registry, through `get_session_title`:

| Title | Kind |
|---|---|
| exactly `Bot Chat` | `canonical` — the registry key ADR-0007 gives a bot |
| `Branch` or `Branch · …` | `branch` |
| anything else, including `Bot Chat · <date time>` | `other` |

`other` rather than the app's own `past`, because `past` is "everything else the
app decided to list" and a gateway cannot know that. What it can say is "not the
canonical chat and not a branch".

**A session that cannot be read says nothing at all**, and the field is omitted:
no Hermes, no registry, an id it has never seen, a row with no title. Guessing
`other` is the answer that would send a tap to the wrong screen, and an absent
field is read exactly as every notification was read before this existed.

The title is read **once per notification, on the sender's thread**, beside the
HTTP calls rather than on the agent's path. It is deliberately not cached: a
branch somebody promotes to Bot Chat changes its title, and a stale kind opens
the wrong conversation for as long as the cache holds. One indexed row read is
cheaper than that mistake.

### Sending

- **Expo** — `https://exp.host/--/api/v2/push/send`, no secret, the token is the
  address. Batches of 100. Tickets are read immediately; `DeviceNotRegistered`
  retires that registration.
- **Web Push** — VAPID (RFC 8292) and aes128gcm (RFC 8188/8291), implemented
  against `cryptography`, which the Hermes runtime already ships. `pywebpush`
  would pull `py_vapid` and `http_ece` for roughly two hundred lines of
  arithmetic. The VAPID key is minted on first use, written `0600`, and never
  rotated automatically. 404 and 410 retire the subscription.

**Retirement is recorded in the plugin's own state, never by editing the app's
registration.** That entry lives under the app's `ui_meta` key and a write there
would fight its compare-and-swap. A retired registration is skipped until the
device writes a fresh entry whose `updatedAt` moves past the retirement.

### Threading

Every hook this plugin registers sits on the agent's own path — `post_llm_call`
runs between the model answering and the person seeing it. So a hook builds a
decision, hands it to a bounded queue (256) and returns; one daemon thread does
the talking. A full queue drops with a warning, because a notification that is
already late is worth less than a turn that is still fast.

One consequence, found on the test VM: in a **short-lived CLI process** the
daemon thread dies at exit before the delayed send fires. In `hermes serve`,
which is what this is for, the process outlives it.

---

## 4. Context

The app writes a `context` section beside the push registrations under its own
key — `hermie-app:<user id>`, or the legacy `hermie-app` — per user a display name, free text, device model and OS, app
version, timezone, locale, and optional per-bot notes.

### Which surface carries it

Two Hermes surfaces can, and they are good at opposite things.

`register_system_prompt_section` is rendered **once** per session and replayed
verbatim, so it never appears in the transcript as a message and never grows
with the conversation. What it cannot do is be handed who is asking — though it
can ask the session variables, which are bound before the prompt is built.

`pre_llm_call` is handed `sender_id` and therefore knows exactly who is asking.
What it cannot do is be free: whatever it returns is appended to the user's
message on **every turn** it fires.

So the module uses both and uses the expensive one as rarely as possible: the
frozen section carries the resolved default user, and `pre_llm_call` contributes
text only where that section cannot cover the turn — the person sending it is
somebody else, what it says has changed underneath them, or there is no frozen
section at all because the chat is older than the plugin. On the single-user
gateway that every Hermie install is today, the last of those fires once on each
chat that predates the install and the others never fire, so the per-turn cost
settles back to zero.

### Does the plugin know who sent the prompt?

**Yes, on an authenticated gateway**, from one of two places.

`pre_llm_call` receives `sender_id`, which is the agent's `_user_id`. On a
WebSocket session that is the `auth_user_id` stamped on the session record from
the transport's login (`tui_gateway/server.py::_transport_auth_user_id`). No
marker in the message text is needed, and none is used.

On the dashboard's own WebSocket route that field is often empty while the
gateway plainly does know the login. Two more places can say so, and both the
hook and the frozen section ask them in turn:

- **`HERMES_SESSION_USER_ID`**, when something bound it. The variables are
  bound when the turn is prepared
  (`tui_gateway/prompt_turn.py::_prepare_turn_input`) and are still bound when
  the prompt is built and when `pre_llm_call` fires, but the dashboard route
  binds every other field and leaves this one empty, so on a stock gateway this
  usually answers nothing. It is asked first anyway: it costs one lookup, and a
  gateway that *does* fill it has said something more direct than anything the
  plugin can work out.
- **The gateway's own session record** — see below, and the reason the feature
  works at all on a gateway nobody has patched.

Three limits, all real:

1. **An ungated gateway names nobody.** With no auth in front of it there is no
   `auth_user_id` on the record and nothing in the session variables either, so
   every source is empty and only the resolved default applies.
2. **`sender_id` is the session's creator, not necessarily this turn's typist.**
   It is stamped when the session is created; a second person attaching to an
   existing session does not change it.
3. **The frozen section is as of session start, so the per-turn path carries
   the change.** Core renders a plugin's section once and replays the bytes it
   persisted; only a rebuild boundary — a new session, or compaction calling
   `invalidate_system_prompt` — makes it render again, and a plugin cannot ask
   for one. See "An edit made while a chat is open" below for what happens
   instead.

### An edit made while a chat is open

Somebody opens Settings → Context, corrects their timezone, and goes back to
the chat they were already in. The system prompt still says the old one, and
will until that chat is rebuilt — which on a long-running Bot Chat may be
never. "Resolve per turn" has to mean the *content* as well as the person, or
the app is left telling people their change takes effect in a new chat.

So `pre_llm_call` also asks, every turn, whether what it froze still holds:

| | |
|---|---|
| `profile.yaml` has not moved | nothing; the turn costs one `stat` |
| it moved, this section reads the same | nothing, and the new stamp is remembered so the parse happens once |
| it moved and now says something else | the new text rides the user message, saying it replaces the frozen copy |
| it moved and now says nothing | a line saying the background was removed and should be disregarded |

The gate is a `stat` of `profile.yaml` — `(mtime_ns, size)`, taken *before* the
read so an edit landing between the two is noticed rather than swallowed. The
app writes that file constantly, for push registrations and `seen` heartbeats,
so a moved stamp is only ever a reason to go and look; the decision is made by
comparing the rendered text against what was frozen. Two writes inside one
filesystem timestamp tick collide, which costs a missed look and not a wrong
answer: the next write moves the stamp again.

Both copies are in the prompt at that point, because the frozen one cannot be
withdrawn. That is why the newer one says out loud that it wins — a model
handed two descriptions of one person and no ordering will average them. The
same reasoning is why a *cleared* context is retracted in words rather than
left standing: somebody who deletes what they wrote about themselves has
usually deleted it on purpose, and silence would leave it in the prompt for the
rest of the session.

What is remembered per session is the resolved user, the frozen text and that
stamp, capped at 512 sessions. A gateway that is up for months sees an
unbounded number of session ids, and evicting the least recently frozen costs a
redundant injection on a chat nobody has touched since.

The capability is `context.live`. Without it the app has to say "this takes
effect in your next chat", which is a sentence no app should have to write.

### A chat that began before the section existed

The same constraint has a second half, and it is the older one. A session whose
prompt was built before the plugin was installed — or before it had a section —
has no frozen copy at all, and core will not build that prompt again for the
life of the session. `top_up` cannot help: it tops up a copy, and there is
none. A Bot Chat that has been open for weeks would stay the one place where the
person is a stranger, however carefully the app filled the profile in.

So the next turn of such a session says the whole thing once. It is the same
text, rendered the same way, with one line in front of it saying that it is
reaching this chat for the first time and replaces nothing — the mirror of the
superseding note, and for the same reason: pointing a model at a correction it
cannot find is pointing it at nothing.

Once is the difficulty, not the saying. Whatever a `pre_llm_call` callback
returns rides the user message on the turn it fires, so a decision nothing
remembers is a decision taken again on every turn for the rest of the chat. The
introduction therefore leaves the same record a frozen section leaves — resolved
user, text, `profile.yaml` stamp, capped at 512 sessions — and leaves it even
when nothing was said, so a gateway with nobody registered stops asking instead
of resolving for ever. From the turn after, the chat is on the ordinary stale
check: one `stat`, and nothing else until something moves.

The record carries one more bit, which is where the older copy sits. A frozen
section is in the system prompt; an introduced one was said in the chat. The
notes that follow it are worded accordingly — "it replaces what was said about
them earlier in this chat", not "what the system prompt says" — because on an
introduced session the prompt says nothing at all.

### Resolution order

A fresh claim on this turn (see "A shared chat names its opener on every turn"
below) → the sender the hook was
handed → the login on the gateway's live session record
→ the sender bound into the session variables → the operator's
`context.default_user` → the app's own `default` → the only registered person,
if there is exactly one **and the gateway named nobody**. With several
registered people and no way to tell who is asking, or with a sender the app has
no row for and no default naming anyone, **nothing is injected**: showing a bot
the wrong person's notes is worse than showing it none.

The first four are one question asked in four places, ordered by how recent
the answer is. A claim is made by the person pressing send, seconds before the
turn; everything after it was settled when the session was created. The session variables are bound once, when a session is created,
and go on naming whoever opened it for as long as it lives; a Bot Chat is
shared, so on a later turn that may well be somebody who is no longer typing.
The live record is the one the turn runs on, so it is asked first. Nothing
further down is reached while a rung above answers.

The "only registered person" rung is a guess, where the two defaults are
somebody's statement about who to assume. A guess never answers for a gateway
that did name somebody: a login the app has no row for is a person the app knows
nothing about, and handing them the one registered person's notes is how one
person's profile reaches another.

### A shared chat

The section frozen into the prompt describes whoever the session started under.
Each turn is resolved again, and the record kept per session remembers both the
person the chat was last told about and the sender that was worked out for, so a
turn from the same sender costs one `stat` and nothing else. When the sender
changes, the next turn carries the new person once, with a line saying that
somebody else is sending this turn and that this replaces what the chat was told
about who that is — worded for where the older copy sits, as every other note
is. A sender the app has no row for gets nothing about anybody; if the chat had
been told about somebody else, that is withdrawn in one line, once.

A record that describes nobody — the chat was opened by a login without a row —
does not stand in the way: the first turn from somebody who does resolve is
introduced, with the same line a chat that predates the plugin gets.

### The dashboard names nobody to a hook, so the plugin asks the dashboard

A dashboard session is created from an authenticated WebSocket upgrade, and the
login is stamped on the session record as `auth_user_id` there and then. It is
simply never handed onwards: `pre_llm_call` gets the agent's `_user_id`, which
that route leaves empty, and a prompt section gets a mapping built from the
agent alone. The gateway knows and does not say.

The plugin runs inside `hermes serve`, in the same interpreter as the server
holding that record. So when nothing else names a sender, `live_session.py`
looks the session up in `tui_gateway.server._sessions` — by the runtime id that
table is keyed on, then through the server's own `_session_for_key` for the
durable key — and reads `_session_auth_user_id(record)`. The ids it tries are
the hook's `session_id` and Hermes' own `HERMES_UI_SESSION_ID`,
`HERMES_SESSION_ID` and `HERMES_SESSION_KEY`, because the two id spaces meet
here and which one names this turn depends on where it came from.

**This reads a private module belonging to somebody else, and that is the
honest cost of the feature.** Three rules hold it to the smallest version of
itself:

- **It never imports the gateway.** The lookup is `sys.modules.get`, so a
  process where the dashboard server is not already loaded — a messaging
  gateway, the CLI, a test — answers "nobody" instead of importing a server
  module and running it to ask a question it could not have answered.
- **It never takes the server's locks.** Reading the table by key is a plain
  dict lookup; the fallback is the server's own helper, which takes the session
  lock only long enough to snapshot. Nothing here holds anything while it works.
- **It reads, checks and returns.** No record is mutated, nothing is cached,
  every attribute is checked for rather than assumed, and anything unexpected
  costs the sender rather than the turn. A gateway that does not have these
  names is a gateway that cannot answer, which is the same as not being asked.

The value comes back as `<provider>:<user id>`, which is the next section.

### A shared chat names its opener on every turn, so the app claims the turn

Everything above names the login that CREATED the session. The hook's
`sender_id` is the agent's `_user_id`, set once from the record's
`auth_user_id` when the agent is built. A second window attaching to the same
session turns the record's transport slot into a fan-out that names no login,
and the turn thread rebinds that slot, so nothing reachable during a turn says
who pressed send. `turn_author` exists, but only for bot-to-bot deliveries. On a
shared Bot Chat every rung therefore answers "the opener", on every turn.

The one place the gateway does know who is sending is an authenticated HTTP
request: the dashboard's auth middleware attaches the session it verified to
`request.state.session`, with the same provider and user id a WebSocket ticket
is minted from. So the app says so itself, just before `prompt.submit`:

    POST /api/plugins/hermie/context/turn
    {"session_id": "<runtime session id>"}

and `pre_llm_call` asks for a claim before it asks anything else
(`context/turn_claim.py`).

- **The identity is the login's, never the body's.** The route builds
  `<provider>:<user id>` from the verified session, stripped the way the server
  builds `auth_user_id`, so a claim and a hook sender are the same kind of
  string. A request that names no person — a gateway without a login, where the
  legacy token admits everybody as nobody, or a service token — is refused with
  403 and claims nothing. Malformed input is a 400, a body not sent as
  `application/json` a 415; an unauthenticated request never reaches the
  handler, because the middleware answers 401 first.
- **Which id.** The app knows the runtime session id: the `session_id` that
  `session.create` and `session.resume` return and `prompt.submit` takes. A
  second window attaches to the same record, so both people submit with the
  same one. During the turn Hermes binds it as `HERMES_UI_SESSION_ID`, and that
  is the exact match.

  **Only a live runtime id is ever a key.** The route answers 404 unless the id
  is a key of the dashboard's own session table, looked up directly and never
  through `_session_for_key`, so a session key or a durable id sent in its
  place is refused rather than stored. That is not tidiness: other platforms'
  sessions have keys too (`agent:main:telegram:dm:…`), a messaging turn binds
  no runtime id, and a claim stored under such a key would put the claimer's
  section — and their name in `HERMES_SESSION_USER_*` — in front of somebody
  else's Telegram turn.

  The hook itself gets the agent's durable session id (which moves when a
  session is compacted), and a session also has a durable key; the route reads
  both off the live record it just found, without taking the table's lock, and
  keeps them beside the claim as aliases. A turn with no runtime id bound is
  matched through those recorded aliases and nothing else — its ids are never
  compared with claim keys. The aliases never override an exact answer: two
  windows can hold two runtime sessions on one stored session, and a turn that
  knows its runtime id must not spend a claim made for the other.
- **A claim stands in only for a dashboard login.** It exists to correct one
  thing, the dashboard naming the opener as every turn's sender, so it replaces
  a hook sender only when there is none on a turn with a runtime id bound (a
  dashboard turn; a scheduled or background turn with neither is not the turn
  anybody claimed), or when it is spelled as a dashboard login:
  a provider prefix the dashboard signs people in with (its registry of sign-in
  providers, read from `sys.modules`, never imported), the claimer's own or the
  opener's. A messaging platform's user id or a bot's name is somebody Hermes
  named correctly, and the claim is left unspent for the turn it was made for.
  The test is worked out before the store's lock is taken and applied under
  it, so the claim that is spent is always the claim that was judged.
- **One claim, one model turn, 30 seconds.** `pre_llm_call` spends the claim it
  uses. Building the prompt looks at it without spending it, so a prompt built
  for this turn already describes the claimer and the hook then has nothing to
  add. A claim nothing spent is ignored and dropped 30 seconds after it was
  made: the app claims immediately before it submits, so a claim is normally
  spent within a second or two, and the window is kept as short as a slow
  network allows because an unspent claim is the whole of the exposure below.
- **The app claims for a model turn and nothing else — this is the guarantee.**
  A claim is for a `prompt.submit` that starts a model turn. The app must not
  claim before a slash command: a command is answered without `pre_llm_call`,
  so no turn spends the claim, and it would wait for the next turn from a
  client that does not claim. **The plugin cannot prevent this; only the app's
  rule does.** Hermes calls a plugin command's handler bare, on its RPC pool
  (`_dispatch_plugin` and the plugin branch of `slash.exec`), with no session
  variables bound. So `/me` cannot see a claim on a real gateway: it neither
  reports one nor spends one. It still tries to spend one afterwards, which is
  harmless and would only matter on a gateway that binds the session for a
  command; nothing here relies on it.
- **Last claim wins.** Two people claiming one session inside the window leave
  the later claim standing, and the next turn is resolved for that person. The
  gateway runs one turn per session at a time and the app claims immediately
  before it submits, so claim order is turn order almost always. Where it is
  not: A claims, B claims, A's submit lands first — A's turn is resolved for B,
  and B's turn falls back to the opener. The window for that is the gap between
  one person's claim and their submit, which is one round trip.
- **Limits, stated.** A prompt that does not start a turn of its own — a busy
  session's input steered into the running turn — never reaches
  `pre_llm_call`, so its claim is left for the next turn inside the 30 seconds;
  every app turn claims first, so that next turn's own claim replaces it, and a
  turn from a client that claims nothing (Hermes' own dashboard or TUI, an older
  app) can inherit it. The same holds for a slash command the app claimed for
  against the rule above. A prompt queued behind a long turn may start after its claim has expired,
  and is then resolved as before. A turn run by an isolated compute worker
  (`dashboard.turn_isolation`, off by default) runs its hook in another process
  that has no store, and is resolved as before as well.
- **Bounded, in memory, one store.** At most 256 sessions hold a claim, oldest
  dropped first; nothing is written to disk and nothing leaves the process.
  Hermes imports this plugin for its hooks under one module name and the
  dashboard loads a second copy of the package for its routes, so module state
  would be two stores that never meet. The store lives in `sys.modules` under a
  fixed name that both copies find, one store per `SHAPE`. What is trusted is
  the shape number, never the class: each copy defines its own `TurnClaims`, so
  a class check would make the second copy to ask refuse the first copy's store
  and the two would silently stop sharing. A test loads the package under two
  module names in one process and claims through one, spends through the other.
  A copy that disagrees about the shape gets a fresh store, any change to the
  store's shape bumps `SHAPE`, and a store this code cannot use at all is
  replaced by a private one with a warning in the log, because a claim that
  cannot cross copies is a feature that has quietly stopped working.

The capability is `context.turn_claim`. It is advertised wherever the context
module is on, like the memory strings: whether the route is reachable is the
dashboard's business. An app that gets a 404 goes on without a claim — from a
plugin without the route, or for a session that is not live on this dashboard,
the answer is the same.

### The two spellings of one id

A gateway login carries the provider that issued it — `self-hosted:<uuid>`,
`oidc:<sub>`, `basic:<name>` — and the app registers a person under the bare id
`/api/auth/me` hands back: `<uuid>`, `<name>`. The same person, spelled two
ways, and which way a section was written in depends on which end wrote it.

So a **sender** matches an entry by either form: exact, the part after the
first colon when the sender carries a prefix, or the prefixed entry when the
sender is bare. Three things that rule deliberately does not do:

- **It never splits a URL.** An OIDC subject may be a URL, and the colon in
  `https://accounts.example.com/12345` is a scheme. A prefix is only read off a
  colon that is not followed by `//`, so `oidc:https://…` loses the provider
  and keeps the subject whole, and a bare `https://…` is left alone entirely.
- **It never crosses providers.** `oidc:max` and `basic:max` are two logins
  that happen to share a name, and are as likely to be two people as one.
- **It gives up on a tie.** A bare `max` that fits both `basic:max` and
  `oidc:max` names nobody, the same answer this module gives to every other
  ambiguity.

Only the sender is read this leniently. `context.default_user` and the app's
own `default` are written by hand against the ids the app registers, so they
are matched as written.

### Telling Hermes who is asking

Hermes carries the identity of a turn in session variables, and tools read them:
a cron job's `user_id`, a kanban card's author, a background watcher's owner. On
the paths Hermie uses they are sometimes empty while the plugin *does* know who
is asking — from `sender_id`, or from the gateway's own session record, and the
app's metadata names the registered person either way. On the dashboard route
that makes this shim the thing that puts the login where a tool can read it.

So `pre_llm_call` fills in `HERMES_SESSION_USER_ID`, `_ID_ALT` and `_NAME` for
that call, under `context.session_vars` (on by default). Four rules keep it a
shim rather than a policy:

1. **Nothing right is overwritten.** If `HERMES_SESSION_USER_ID` already names
   the person sending this turn — in either spelling of the id — the shim writes
   nothing at all, and where nothing is bound each of the three is written only
   when it is itself empty. The one exception is a bound login that names
   somebody ELSE, which on a shared chat is whoever opened it: then all three
   are rewritten for the resolved sender, the name and alternative id included
   (emptied when the app has no row for them), because that is the line Hermes
   puts in front of the model as "User:" and it would otherwise contradict the
   context section. A turn that names nobody leaves a bound login alone. The
   day Hermes fills them in per turn, this becomes a no-op that nobody has to
   come back and remove.
2. **The id may be the gateway's, the name may not be.** The sender — handed
   to the hook, or read off the gateway's own session record — is a fact and
   is used as-is, provider prefix and all. The display name and the
   alternative id come from that person's own entry, so a sender the app has
   never seen gets an id and no name rather than somebody else's.
3. **Asking is not filling.** `context.session_vars: false` switches off this
   write. It does not switch off *reading* `HERMES_SESSION_USER_ID` to find out
   who is asking — that is the resolution order above, it writes nothing, and
   an operator who turns the shim off has asked not to be written to rather
   than asked to be treated as a stranger.
4. **It is written where Hermes keeps it, not in the environment.** The write
   goes to the same `ContextVar` `get_session_env` reads. Setting a process-wide
   environment variable instead would outlive the turn and reach every other
   session in the gateway, which is the bug the `ContextVar`s replaced.

**And one limit, which is the whole size of the feature.** `pre_llm_call` is one
of the hooks Hermes runs under `plugins.hook_callback_timeout` (30s by default),
and a bounded callback runs on a worker thread through
`contextvars.copy_context().run(...)`. A variable set inside a copied context is
discarded with that context, so under the default timeout the write never
reaches the turn. With `plugins.hook_callback_timeout: 0` the callback runs on
the caller's own thread and the write lands where the rest of the turn reads it.
The shim is built to be harmless either way — every gate above it is free, and
the metadata read happens only once the variables are known not to name the
sender already.

### `/me`

The context module's whole job is to be invisible, which makes it a bad thing
to debug by reading a prompt. `/me` answers the question directly, in the
session, without a model call: there is nothing a model could add, and somebody
checking whether their identity reached the gateway should not pay for a turn
to find out.

It resolves exactly what a turn resolves and reports it: the person, **which
rung answered** (the hook's sender, the live session record, the session
variables, the configured default, the app's own, the only registered person, or
nobody), the login id and the registered id it matched, the device line,
timezone and locale, the "about" text, this bot's own note, and which of the
app's ui_meta keys the entry came from with when it was written.

When the answer is nobody it says why — nobody registered, a sender that
matches nobody, or several people and no way to tell — and gives the one action
that fixes it: accept the sharing notice in Hermie's Settings → Context, then
send a message.

Two rules, both the same rules as everything else here: it names ids and
nothing else (no token, key, endpoint or configuration value goes near it), and
the answer is bounded. The capability `command.me` is advertised only when
Hermes actually took the registration — core answers `None` when the name is
already taken, and a capability names what is there, not what shipped.

### Saying where the facts came from

The facts say what the person is like. They do not say what a bot is reading,
and until this version a bot had to be told by hand that the person has a
profile, that it lives in their app, that a plugin carries it and that there is
more of it elsewhere. A feature whose whole job is to save somebody that
explanation cannot require it.

So the section carries a short, fixed paragraph of its own, in this order:

1. where these details come from — the person's own profile in their Hermie
   app, reaching the bot through this plugin, kept current;
2. what may be done with them — the name, the timezone and locale for dates and
   language, the device for phrasing;
3. who decides what is in them — the person, in Hermie under Settings → Context;
4. `/me`, which prints what is being shared and how it was worked out;
5. the profile's memory, which holds what they said in earlier chats;
6. and that anything missing is something they have not shared, so asking is the
   only way to know.

Two rules keep it honest. Every sentence is permissive rather than directive —
it is context, and a paragraph of orders in a system prompt is a paragraph the
person did not write. And the two sentences that point somewhere are said only
where that place will answer: (4) needs the command registration Hermes may
refuse, (5) needs the memory module switched on, and a bot pointed at something
that is not there is worse off than one pointed nowhere. That is the capability
rule applied to prose. The capability is `context.orientation`.

### Bounding

Per-field caps first (display name 80, about 600, per-bot note 400), then a
whole-section cap (1200 by default, hard-capped at core's 4000). Newlines are
flattened, so one field cannot become twenty lines. The rendered text ends by
saying it is background the person set in their app and not an instruction for
this turn — a model that is not told where a fact came from will treat it as a
directive.

Under a tight cap the orientation paragraph is what gives way, a whole sentence
at a time and from the end, before anything the person wrote is touched. Half a
sentence about where to look is worse than none of one, and the budget exists
for their own words: on a section that was already at the cap before this
version, the same bytes come out.

---

## 5. Configuration

Read through `ctx.get_config`, so the real path is
`plugins.entries.hermie.settings.<key>` in `config.yaml`:

```yaml
plugins:
  enabled: [hermie]
  entries:
    hermie:
      settings:
        modules: {push: true, context: true}
        push:
          types: [message, request, cron, cron_done, cron_failed,
                  turn_done, turn_failed]
          preview: device          # or "never"
          attached_window_seconds: 90
          delay_seconds: 5
          public_url: ""           # default: Hermes' own dashboard.public_url
          vapid_key_path: ""       # default: the plugin's own data dir
          vapid_contact: "mailto:you@example.com"
        context:
          max_chars: 1200
          default_user: ""
          session_vars: true       # fill HERMES_SESSION_USER_* when they are empty
```

## 6. State

`$HERMES_HOME/plugin-data/agent-plugin-hermie-<hash>/state.json`, through
`ctx.state`. It holds sent-event ids (24h) and retired installation ids. All of
it is disposable in the "one redundant notification" direction; none of it is a
credential and none of it is a registration.

Two things a reader might expect to find here are deliberately **not** stored.
The gateway key is derived — from the registration that is about to be sent to,
or from configuration — so there is no copy to go stale when somebody
re-addresses their gateway. A session's kind is read when the notification is
sent, for the reason §3 gives: a branch that gets promoted changes its title,
and a cached kind opens the wrong conversation. The one thing held in memory is
the CONFIGURED key, worked out once per load, because neither the setting nor
`dashboard.public_url` moves while a gateway runs.

The VAPID private key sits beside it, `0600`. It is the one secret this plugin
holds, and it is one it minted itself.

---

## 8. Setting a profile's display name

Core serves this for one profile only. Its `PATCH /api/profiles/{name}` sets a
display name on `default`; on every other profile the same call renames the
profile itself, which moves its directory and every client's handle for it. An
app that only wants a friendlier label therefore uses this plugin's
`PATCH /api/plugins/hermie/profiles/{name}` (see the README), which writes the
same `display_name` key through the same `hermes_cli.profiles.write_profile_meta`
and changes nothing else. What follows is core's route as read out of Hermes
0.21.3, kept because an app still meets it for `default` and for real renames.

**The route is `PATCH /api/profiles/{name}`**, on the dashboard server, behind
the same auth as everything else there. Not `POST …/rename`.

```
PATCH /api/profiles/default
{"new_name": "Jurist"}
```

`new_name` is the only body key (`ProfileRename` in `hermes_cli/web_models.py`).

**The `default` profile is the case that matters**, because a Hermie bot usually
is one. Its home *is* the installation root, so it cannot be renamed; Hermes
turns the call into a presentation-only display name and says so in the answer,
keeping the canonical id:

```json
{"ok": true, "name": "default", "display_name": "Jurist", "path": "/…/.hermes"}
```

Any other profile is really renamed — directory, wrapper script, service,
active-profile pointer — and answers without `display_name`:

```json
{"ok": true, "name": "jurist", "path": "/…/.hermes/profiles/jurist"}
```

| Status | When |
|---|---|
| 400 | `ValueError` or `FileExistsError` — a name over 64 characters, an invalid or reserved id, an empty new name for `default`, a target that already exists |
| 404 | `FileNotFoundError` — no such profile |
| 500 | anything else |

What the setter validates is **only** `.strip()` and a 64-character maximum
(`hermes_cli/profiles.py::set_profile_display_name`). No character set, no
uniqueness: two profiles may carry the same display name, and one may equal
another's canonical id. Passing `""` clears it — the key is removed from
`profile.yaml` and the label falls back to the id — but `rename_profile` refuses
an empty new name for `default` before the setter sees it, so clearing that one
is not reachable over this route.

**Reading it back** is `profiles.list` over the WebSocket the app already holds:
the roster row carries `display_name`. `bot_title` is not a row field — it is
`ui_meta["hermes-bots"]["title"]`, which the same row carries under `ui_meta`
and which `profiles.configure` can write with the per-key compare-and-swap.

**There is no WebSocket method that sets a display name or renames a profile.**
`groups.rename` renames a room, `pet.rename` a mascot, `session.title` a
session, and `/rename` is a stub pointing at `/title`. So this one call goes
over HTTP while the rest of the app's profile work stays on the socket.

---

## 9. The memory browser

The first part of this plugin to answer HTTP; the turn claim in §4 is the only
other. It mounts the way §1 describes —
`dashboard/manifest.json` naming `plugin_api.py`, whose module-level `router`
core mounts at `/api/plugins/hermie/` — and it is the exception to the rule in
§1 rather than a change of mind about it. The alternatives were weighed and
none of them works: `ui_meta` is a profile file that every client reads on every
roster paint, and the gateway's WebSocket has no memory method to borrow.

### Who may call it

**Whoever is signed in to the dashboard, and that is the whole of it.** Hermes
authenticates a dashboard request with process-wide middleware and then hands
every authenticated caller every route — core's `/api/memory/reset` included —
with no role, owner or permission for a route to check. (It does say WHO was
admitted, on `request.state.session`; `context/turn` in §4 uses that as the
identity a turn is claimed for, and nothing here uses it as a permission.) So these routes treat a
signed-in caller as an operator of the machine. There is no per-user
authorization here because there is nothing to build one from, and a check that
could only re-read the same shared token the middleware already checked would be
a lie in the shape of a safeguard. The README says so where somebody deciding
whether to share a gateway will read it.

Two things the plugin *can* get wrong, and both are held by a test: a path that
landed outside `/api/plugins/hermie/` would be a route nothing gates, and a
WebSocket would not be gated at all, because HTTP middleware does not see an
upgrade. There is no socket here. A third test asserts the prefix is absent from
core's own public-path allowlist, so the gate demonstrably covers us.

### Which profile

**`profile` is required on every route.** A plugin handler is handed none and
otherwise runs under whichever home the dashboard process started with, which on
a multiplexed gateway is somebody else's memory. The name is rejected before it
reaches any Hermes function if it carries a separator, a parent reference, a
null or surrounding space — not sanitised, rejected, because a profile is an
identifier the gateway already knows rather than a path to be cleaned — and then
checked for membership in the gateway's own list.

Scoping itself is `hermes_constants.set_hermes_home_override`, which is public,
context-local, and deliberately does not touch `os.environ` (a process-wide
write would reach every other thread in the gateway). Set and reset happen on
the one thread that does the work, in a `finally`, so a failed request cannot
leave another profile's home bound. Under it, `MemoryStore._path_for` resolves
inside that profile and the lock it takes is that profile's `MEMORY.md.lock`.

### What it will and will not do

- **Two targets, `memory` and `user`.** The store dispatches on a bare
  `target == "user"` and the tool layer refuses anything else; a third would be
  our invention.
- **Every write is `MemoryStore.add` / `replace` / `remove`**, so the file lock,
  the external-drift backup and the char limits are Hermes' own, and the
  response is the store's own result dict rather than a translation of it.
- **An entry is named by its text.** A memory file has no ids — entries are
  `"\n§\n"`-joined text — so a listing mints positional ones, and a position is
  only a way to look an entry up. The text is what goes to the store, which
  matches on text itself, so an index that went stale between a read and a write
  cannot delete the entry that moved into its place. A stale index is an error.
- **A graph pages over entries, not over nodes.** A page that filled its node
  cap would silently drop entries and an app paging through would never learn
  it had missed one. The node and edge caps are a last defence; `truncated`
  says when one bit. Topics are cheap by design — capitalised phrases that are
  not sentence openers, `@handles`, `#hashtags`, ISO dates — and a topic that is
  nonsense is a node nobody clicks rather than a wrong answer.
- **An external provider is listed and never enumerated.** `MemoryProvider` has
  `prefetch(query)` returning opaque formatted text and no call that returns
  entries; mem0's own surface is `search(query, top_k)` with no `get_all`. So
  every external row carries `enumerable: false`. That is the gap, and naming
  the provider while saying it cannot be opened is the honest version of it.
- **A backend can also be read as it is stored.** Everything above answers a
  memory the store has already parsed, which is the shape to edit it in and the
  wrong shape for "what is in there": a heading, a blank line the store kept, a
  delimiter that ended up inside an entry and the file's real order are all
  invisible in a list of entries. So `raw` reads the two files itself — decoded
  and otherwise untouched — from the directory Hermes resolves per call, and
  reports every backend beside them. It writes nothing, and it is behind
  `memory.browse` because it is the same reading of the same files.

  Three answers there are deliberately distinct, because collapsing any two of
  them tells somebody their memory is empty when it is not: a file that is
  absent is left out of the answer while one that exists and is bare is sent as
  empty; a backend that is set up and cannot enumerate is available with no
  documents and its own sentence saying why; and a backend this gateway does not
  really have — not installed, or named in the config with nothing to
  authenticate with — is not available at all. `available` is therefore narrower
  here than on a `list` row, where it keeps the discovery's own meaning: that
  route reports what exists, this one is asked what is stored. Each document is
  capped at 256 KiB with `truncated` beside the full `chars`, which is a ceiling
  on a file that has gone wrong rather than a page — there is no way to ask for
  the rest and no intention of adding one.
- **Both halves switch off per profile**, through that profile's own config —
  which is the right scope, since the operator of a profile decides whether its
  memory can be opened. `memory.edit` without `memory.browse` is not a state:
  an app that cannot list an entry cannot name one to replace.

---

## 7. Threat model

Unchanged from ADR-0017, with three differences, all of them reductions.

**Covered.**

- *Nothing on the network can make a device buzz.* There is still no inbound
  endpoint. The plugin adds no listener, and the dashboard route it could have
  used is deliberately not used.
- *A stolen Expo token cannot read anything.* It is a send address.
- *A spoofed push cannot act.* Every action is re-validated against the
  gateway's own open requests before a response is sent.
- *Content stays on the gateway by default.* With `preview` off, the transports
  carry a bot name and a type.

**Accepted.**

- *The plugin runs with the gateway's own trust.* It is in-process, so it can see
  what the gateway sees. This is **less** exposure than the daemon, which needed
  a long-lived gateway credential in a state file a second process could read;
  here there is no such credential at all.
- *Traffic analysis.* Apple, Google and any browser push service learn that a
  device received a notification, when, and from which server.
- *The memory browser trusts the dashboard's own auth.* Any signed-in caller can
  read and edit any profile's memory. That is inherited, not invented: Hermes
  gives a route no identity to check and core's own routes already work this
  way. It is a reduction only in that `memory.browse` turns it off, which is a
  switch core's `/api/memory` does not have. §9.
- *A signed-in person can claim any session's next turn.* `context/turn` takes
  any session id, and a claim decides whose context section the next turn of
  that session carries. It cannot make a turn resolve to anybody but the caller
  — the identity is the caller's own login — and only a live dashboard session
  can be claimed, never another platform's, so the worst it does is put the
  caller's own section in front of somebody else's dashboard turn, for one
  turn, within 30 seconds. That is the same trust every signed-in caller already has over
  every route here. §4.
- *`ui_meta` is per profile, not per user.* The app's key is per person now
  (`hermie-app:<user id>`), but `ui_meta` itself is not: every key on the
  profile is handed to every client that can read the profile, so two people on
  one gateway can still see each other's registrations and each other's context
  section. That section holds a display name, a device
  model, a timezone and free text the person wrote about themselves, which is
  more personal than a push token. Anyone running a shared gateway should know
  that before filling it in; it is stated in the README and it is the reason the
  `sessions` module is on the list.
- *Push is only as reliable as the thing running it.* A gateway that is not
  running sends nothing. Notifications are best effort and the app never treats
  their absence as information.

---

## 8. Install

```
hermes plugins install fullstackstudio-nl/hermie-plugin --enable
hermes gateway restart
```

Hermes clones the repo into `$HERMES_HOME/plugins/<manifest name>/` — so
`plugins/hermie/`, taken from `name:` in `plugin.yaml`, not from the repo name.
The manifest and `__init__.py` must sit at the repo **root**, which they do. The
tree is scanned by `tools/plugin_guard` before anything moves, declared Python
dependencies are resolved against core and every other enabled plugin (there are
none to resolve), and `--enable` writes `plugins.enabled`.

`--ref` takes a 40-character SHA only, so pinning is exact.
