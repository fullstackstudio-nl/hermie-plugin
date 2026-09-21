# Changelog

Notable changes per release. Capabilities are listed by the string the app tests
for, because that is what the app tests for — a version number here is for
people.

## Unreleased

### Added

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

- `context.live` — a context edit made while a chat is open reaches that chat on
  its next turn. Hermes renders a plugin's system prompt section once per
  session and replays the bytes it persisted, and a plugin cannot ask for a
  re-render, so the per-turn hook now carries the change instead: it compares
  the app's `profile.yaml` stamp against the one taken when the section was
  frozen and, when the rendered text has actually changed, sends the new copy
  saying it replaces the frozen one. A cleared context is retracted in words.
  On a turn where nothing changed this costs one `stat` and no read.
