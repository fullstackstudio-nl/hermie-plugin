# Changelog

Notable changes per release. Capabilities are listed by the string the app tests
for, because that is what the app tests for — a version number here is for
people.

## Unreleased

### Added

- `push.session_kind` — a payload says whether its session is the bot's
  canonical chat, a branch, or neither, so a tap can open the right conversation
  now that a bot has more than one. Read from the session's title, the same
  signal and the same three titles the app classifies by, and advertised only
  where this gateway can actually read one. A session it cannot read carries no
  kind at all rather than a guess, and the title is read once per notification
  rather than cached, because a branch that gets promoted changes its title.

- `cron`, `cronCertain` and `jobId` now ride an **approval or a question raised
  inside a scheduled run** as well as the cron deliveries and turn endings that
  already carried them. They stay `request` notifications — somebody is still
  being asked — but "this is a job you are not watching" is the most useful
  thing a lock screen can add to a question. Neither hook carries a `task_id`,
  so the answer there comes from `HERMES_CRON_SESSION` or a `cron_…` session id.

- `push.per_bot` — the `push.perBot` overrides the app writes beside the
  registrations are read and folded over each device's own switches, by the same
  rule (and the same function name) the app's switch screen uses. The bag is
  partial on purpose: a type nobody overrode keeps following the global switch
  as it moves. A mute still outranks all of it — an override is a preference
  about a type, a mute is somebody saying no to the bot. The section version is
  not bumped, for the reason ADR-0016 gives: `v` is checked per row and an
  unreadable row is dropped, so a bump would unregister the device rather than
  protect the key.

- `push.gateway_key` — every payload names the gateway it came from, as FNV-1a
  (64-bit) over the gateway's public origin in 16 lowercase hex digits. A device
  can be set up against several gateways, and a notification saying only
  "researcher" leaves an app with two of them to choose between. The algorithm
  is specified rather than shared — it exists in the app's
  `packages/gateway-client/src/gateway-key.ts`, in Hermie Web's zero-dependency
  copy and now here — and all three pin the vector
  `https://gateway.example.com:8443` -> `bf796761db84e312`. The key is taken
  from the `gatewayKey` the device itself wrote on its registration, because
  that is the string that device will compare against and the two sides then
  agree by construction; a row written before the app carried one falls back to
  the new `push.public_url` setting, else Hermes' own `dashboard.public_url`.
  A row's claim is checked rather than copied, and a payload that can name no
  gateway simply carries no key — which is how every notification before this
  behaved.

- `memory.browse` and `memory.edit` — a memory browser for a profile's
  `MEMORY.md` and `USER.md`, served at `/api/plugins/hermie/memory/`
  (`list`, `search`, `graph`, `edit`). This is the plugin's first HTTP surface
  and the exception to the rule in DESIGN.md §1: `ui_meta` cannot carry it and
  the gateway's WebSocket has no memory method to borrow. It runs behind the
  dashboard's own authentication, which is all-or-nothing — any signed-in caller
  is treated as an operator, as on every core route — and the README says so in
  the shared-gateway warning. `profile` is required and validated against the
  gateway's real profile list; the store is opened under that profile's home
  through the public `set_hermes_home_override`, so Hermes' own file lock, drift
  check and char limits apply. External providers are listed with
  `enumerable: false`, because the provider interface has no call that returns
  entries.

- `plugin.update_check`, and an advert that says which build is installed —
  `version`, `maxContract`, `minAppVersion`, and a `source` naming the
  repository and the commit the installed tree sits at (read from `.git`, no
  subprocess, no network). That is enough for the app to say "a plugin update is
  available" without the gateway reaching anywhere. A gateway-side check of the
  newest release tag exists behind `update.check: true` — one GET, cached an
  hour across restarts, nothing identifying sent — and is off by default.

### Changed

- The docs now carry the exact shape of Hermes' own `PATCH /api/profiles/{name}`
  (DESIGN.md §8), which is how a display name is set. The plugin does not
  duplicate it: any authenticated dashboard caller can already reach core's
  route, and a plugin route could not have applied the per-user check that would
  have justified a second one.

- State is version 2, adding the update-check cache. `sent` and `retired` come
  through a migration unchanged; a state file from an unknown future version is
  still left on disk untouched.

- `push.type.cron_done`, `push.type.cron_failed` and `push.cron.signal` — a cron
  run is now recognised by the scheduler's own marker (the `cron:<job id>:…`
  task id, then `HERMES_CRON_SESSION`, then the `cron_<job id>_<stamp>` session
  id) rather than by looking for "cron" in a free-text `platform` string, which
  stays as the last resort. A notification carries the job id and says whether
  the signal was a fact or a guess. `cron_failed` covers the agent's own
  `[CRON_FAILURE]` line and a cron turn that failed; the scheduler's verdict on
  the job itself is decided after the agent is gone and fires no hook, so it is
  out of reach and the README says so.

- A device's own `types` are read over **every** type this plugin can send, so a
  registration that says `cron_done: true` or `cron_failed: true` is honoured.
  The reader and the sender kept separate lists of type names and the reader's
  was the shorter one, which left the two cron endings above sendable but
  impossible to ask for. There is one list now. A type a row does not name is
  still **off** and is never inferred from `cron` — no device starts receiving
  something it never asked for because a gateway was updated — and the app's own
  two switches, which default to on, reach a device when it next registers.
  `push.types` remains the gateway-wide ceiling and a mute still outranks all of
  it.

- `context.live` — a context edit made while a chat is open reaches that chat on
  its next turn. Hermes renders a plugin's system prompt section once per
  session and replays the bytes it persisted, and a plugin cannot ask for a
  re-render, so the per-turn hook now carries the change instead: it compares
  the app's `profile.yaml` stamp against the one taken when the section was
  frozen and, when the rendered text has actually changed, sends the new copy
  saying it replaces the frozen one. A cleared context is retracted in words.
  On a turn where nothing changed this costs one `stat` and no read.
