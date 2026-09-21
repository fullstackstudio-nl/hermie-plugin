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

- **No cron hook.** There are no hook fire sites in `hermes_cli/cron.py`. A cron
  run is an ordinary agent session, so the turn hooks fire inside it, but
  nothing carries a job id or says "this was a cron". The plugin recognises a
  cron delivery only by the session's `platform` string, which is a heuristic;
  when it misfires the message is notified as a message, which it also is.
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
- **HTTP routes** — possible, but not over the gateway's own port. A plugin
  shipping `dashboard/manifest.json` with an `"api"` key gets a FastAPI router
  mounted at `/api/plugins/<name>/` on the **dashboard** web server. That is a
  second address, which is the thing ADR-0017 refused, so the plugin does not
  use it. The capability advert goes in `ui_meta` instead, where the app is
  already looking.
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
because it knows which key it came from — that is what `mutes` is checked
against, and what the push module means by "every device of a user".

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

`hermes plugins update <name>` exists and git-pulls the installed tree, so the
manual path is one command. There is no plugin-driven self-update: a plugin
cannot run `hermes plugins install` for itself without shelling out as the
gateway user, and a plugin that can rewrite its own code is a plugin that can
rewrite its own code. The app shows "a plugin update is available" by comparing
the advert's `version` against what it knows, and shows the command.

What an upgrade must never cost is state. `state.py` carries a version and a
migration table; a state file from a version this build does not know is **left
on disk untouched** and treated as empty for the run, because losing dedupe
history costs one duplicate notification while overwriting a newer file costs a
downgrade its data. Registrations are not the plugin's state at all — they live
in the app's `ui_meta` and survive any plugin change, including removal.

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
| cron delivered | `post_llm_call` with a cron-ish `platform` | `cron` (heuristic) |

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

### Payload

Bot name and event type. Nothing else, unless the device turned `preview` on
**and** the gateway allows it — the gateway setting is a ceiling, never a floor.
`preview: never` overrides a device that asked; `preview: device` never turns one
on. Payloads are kept under ~3.5 KB because APNs caps around 4 KB.

An approval payload carries `requestId`. That is a hint, not an instruction:
tapping Allow opens the app, which connects to the gateway, re-reads the open
requests, and responds only if that request is still open and still says what
the notification said. A forged "Allow" for a destructive command opens an app
that finds no such request and says so.

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
text only when the person sending this turn is somebody else. On the single-user
gateway that every Hermie install is today, the second path never fires and the
per-turn cost is zero.

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

### Resolution order

The sender the hook was handed → the sender bound into the session variables →
the login on the gateway's live session record → the operator's
`context.default_user` → the app's own `default` → the only registered person,
if there is exactly one. With several registered people and no way to tell who
is asking, **nothing is injected**: showing a bot the wrong person's notes is
worse than showing it none.

The first three are one question asked in three places, and a gateway that
answers more than one answers the same thing in each. They are ordered by what
they cost and by how direct they are: a field handed to us, then a variable
lookup, then a read of somebody else's table. None of them is reached while a
cheaper one answers.

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

1. **Nothing is overwritten.** If `HERMES_SESSION_USER_ID` already holds a
   value, the shim writes nothing at all; each of the three is written only
   when it is itself empty. The day Hermes fills them on this path, this
   becomes a no-op that nobody has to come back and remove.
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
the metadata read happens only once the variables are known to be empty.

### `/me`

The context module's whole job is to be invisible, which makes it a bad thing
to debug by reading a prompt. `/me` answers the question directly, in the
session, without a model call: there is nothing a model could add, and somebody
checking whether their identity reached the gateway should not pay for a turn
to find out.

It resolves exactly what a turn resolves and reports it: the person, **which
rung answered** (the hook's sender, the session variables, the live session
record, the configured default, the app's own, the only registered person, or
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

### Bounding

Per-field caps first (display name 80, about 600, per-bot note 400), then a
whole-section cap (1200 by default, hard-capped at core's 4000). Newlines are
flattened, so one field cannot become twenty lines. The rendered text ends by
saying it is background the person set in their app and not an instruction for
this turn — a model that is not told where a fact came from will treat it as a
directive.

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
          types: [message, request, cron, turn_done, turn_failed]
          preview: device          # or "never"
          attached_window_seconds: 90
          delay_seconds: 5
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

The VAPID private key sits beside it, `0600`. It is the one secret this plugin
holds, and it is one it minted itself.

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
