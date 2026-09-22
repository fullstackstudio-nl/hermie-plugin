# Changelog

Notable changes per release. Capabilities are listed by the string the app tests
for, because that is what the app tests for — a version number here is for
people.

## Unreleased

### Added

- `profiles.display_name` — the app can rename a bot's profile label from its
  own settings screen. `PATCH /api/plugins/hermie/profiles/{name}` with
  `{"display_name": "…"}` writes only that key, in that profile's own
  `profile.yaml`, through the same `write_profile_meta` Hermes' own
  `PATCH /api/profiles/{name}` calls for the `default` profile — never the
  canonical id, the directory or anything a real rename would move, so it works
  the same way on every profile rather than only on `default`. Refused with 400
  for a name that is empty after trimming, longer than 60 characters or
  carrying a control character; 404 for a profile this gateway does not have;
  403 per profile via the new `profiles.edit` setting, off means read-only the
  same way `memory.edit` does.

- `context.turn_claim` — **the person typing in a shared chat gets their own
  context, not the opener's.** Hermes names the login that created a session to
  every hook for the life of that session: the hook's `sender_id` is set once
  from the record's `auth_user_id`, a second window leaves the record naming
  nobody new, and the turn thread names nobody at all. So every rung the plugin
  had answered "whoever opened the chat", on every turn, whoever typed it.

  The app now claims the turn just before `prompt.submit`, with
  `POST /api/plugins/hermie/context/turn` and `{"session_id": "<runtime session
  id>"}`, and `pre_llm_call` asks for a claim before it asks anything else. The
  identity is the dashboard login the request was authenticated as, spelled
  `<provider>:<user id>` like the gateway spells it; nothing in the body can
  name anyone. 204 on success, 400 for a missing or malformed id, 404 for an id
  that is not a live runtime session on this dashboard (a session key or a
  durable id is refused, never stored), 415 for a body not sent as JSON, 403
  for a request that is not signed in as a person. A claim is spent by the one
  model turn that uses it and ignored after 30 seconds; two claims on one
  session inside that window leave the later one standing. The turn is matched
  by the runtime id Hermes binds for it, and only where none is bound by the
  durable key and agent session id the route read off the live record. A claim
  replaces a hook sender only when that sender is spelled as a dashboard login,
  so a messaging platform's user or a bot is never overridden, and it stands in
  for an empty sender only on a turn with a runtime id bound. Building the
  prompt reads a claim without spending it; `/me` names it as the rung that
  answered and spends it where it can find it. The app claims for a
  `prompt.submit` that starts a model turn and never for a slash command, which
  is what keeps a claim from outliving its turn. The hooks and the dashboard
  run separate copies of the package, and the store is shared between them by
  its shape number, never by class.
  At most 256 claims are held, in memory, in one store both copies of the
  plugin share; nothing is written and nothing leaves the process.

- `memory.raw` — **a backend can be read as it is stored.** The three browsing
  routes all answer a memory the store has already parsed into entries, which is
  the shape to edit it in and the wrong shape for the question "what is in
  there": a heading, a blank line the store kept, a delimiter that ended up
  inside an entry and the order the file really has are none of them visible in a
  list of rows. And for an external provider there was nothing at all — `list`
  names it and marks it `enumerable: false`, which says it exists and says
  nothing about what it holds. `GET /memory/raw?profile=[&backend=]` answers one
  card's worth per backend: the built-in one with each file as it is held,
  delimiters included, and every other one with either its documents or the
  reason it has none.

  The empty answers are the part that had to be got right, because collapsing
  any two of them tells somebody their memory is empty when it is not. A file
  that is **absent** is left out of the answer, while one that exists and is
  bare is sent with `content: ""`. A backend that is set up and cannot be listed
  is available with no documents and its own sentence about why — which is every
  external provider, since the interface offers `prefetch(query)` and nothing
  that returns what it holds. A backend this gateway does not really have, either
  not installed or named in the config with nothing to authenticate with, is not
  available; `available` is therefore narrower on this route than on a `list`
  row, where it still means "the package imports", because that route reports
  what exists and this one is asked what is stored. A file that exists and cannot
  be decoded is named in the backend's note rather than shown as empty, since
  empty is a claim about its contents.

  It reads the files itself, from the directory Hermes resolves per call so the
  profile scope still moves it, and it writes nothing. Each document is capped at
  256 KiB with `truncated: true` beside the full `chars` — a ceiling on a file
  that has gone wrong, not a page, since there is no way to ask for the rest.
  Gated by `memory.browse` alone: it is the same reading of the same files, and a
  second switch for one permission is a switch somebody has to find before the
  feature works. `editable` follows `memory.edit` for the day a write exists;
  nothing writes a whole document today.

- `context.orientation` — the rendered section now says what it is. A bot was
  told the facts about the person and nothing about where they came from, so
  somebody had to sit and explain the plugin to their bot before the feature
  worked at all. The section carries a short fixed paragraph instead: that these
  details are the person's own profile in their Hermie app, arriving through
  this plugin and kept current; that the name, the timezone and locale, and the
  device are there to be used; that `/me` prints what is being shared and
  Settings → Context is where the person changes it; that earlier chats are in
  the profile's memory rather than here; and that anything missing is something
  to ask about. It is permissive throughout, because it is context and not
  instruction. The two sentences that point somewhere are said only where that
  place answers — `/me` needs the registration Hermes can refuse, the memory
  line needs the memory module switched on — which is the capability rule
  applied to prose. Written in one place, so every path renders the same
  paragraph.

- **A chat that was already open when the plugin arrived learns who it is
  talking to.** `context.live` tops up a frozen section that has gone stale, but
  a session whose prompt was built before the plugin existed has no section to
  top up and core will never build that prompt again — so the longest-running
  Bot Chat was the one place the person stayed a stranger. Its next turn now
  carries the whole section once, with a line saying it is reaching this chat
  for the first time and replaces nothing. Once is the point, since anything
  returned there rides the user message: it leaves the same record a frozen
  section leaves, under the same 512-session bound, even when nothing was said,
  so a gateway with nobody registered stops asking rather than resolving on
  every turn. A session that never had a section and then gets an edit is told
  that the newer copy beats what was said earlier *in the chat* — the system
  prompt, on that session, says nothing to beat.

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

- **The context section now says whether the gateway knows who is talking to
  the bot.** It resolved that all along and threw it away: a person named by
  their own turn claim and a person picked out of the app's `default` rendered
  byte for byte the same, and every section ended by saying it was background
  the person set in their app and not an instruction — right about what somebody
  wrote about themselves, and a reason to discount the one thing in the section
  a model could have relied on. So a bot could not answer "who am I talking
  to?" however well the gateway knew.

  The rung is carried into the rendering and splits in two. On a **verified**
  rung — the turn claim, the sender Hermes hands the hook, the login on the
  gateway's live session record — the section states it as fact, in its own
  sentence, before the background: `The gateway verified that this turn was sent
  by "<name>", signed in as <provider>:<user id>.` It takes the place of the
  plain `You are talking to …` line rather than repeating it, and the framing
  line stops contradicting it: `Who sent this turn is the gateway's own
  statement and can be relied on. The rest is background…`. On every other
  rung — the configured default, the app's own, the only registered person, and
  the session variables, which name whoever *opened* a shared chat rather than
  whoever is typing — it says the opposite as plainly: `The gateway could not
  confirm who sent this turn. What follows is the default profile it falls back
  to, not a person it identified.` The verified sentence never renders on a rung
  that verified nobody; a rung this build cannot place renders neither, which is
  also what every caller written before this gets.

  The display name is now quoted inside a sentence the framing no longer covers,
  so it is cleaned as the untrusted input it is: the 80-character cap stays,
  line breaks and control characters come out (including the ones Python counts
  as whitespace and a terminal does not, and the bidi overrides that reorder
  what is drawn), markup that could open a heading, a fence, a quote or a link
  is removed, and what is left is quoted — wherever the name is rendered, not
  only in the assertion — so a sentence buried in a name reads as part of the
  name and cannot imitate a sentence of the section's own. The login is cleaned
  the same way. `You are talking to Ana.` is therefore now `You are talking to
  "Ana".`

  What the gateway checked is a fact about a *turn*, so it is kept out of the
  per-session record: the copy a chat is remembered by carries no such sentence,
  and the copy it is sent does. Otherwise a chat whose turns are sometimes
  claimed and sometimes not would pay an injection each way round, each of them
  announcing that the person had changed their profile. The cost is that a
  section frozen as unconfirmed is not corrected on its own account — it is
  asserted from the start wherever the prompt was built with a sender in reach,
  which is every dashboard session, and otherwise waits for the next copy the
  chat is sent.

- **What became of a turn claim is now in the log**, which nothing said before,
  so a gateway where the feature had quietly stopped working looked exactly like
  one where nobody had claimed anything. Every line begins `hermie: turn claim`:
  `spent`, `refused` (with why it may not stand in for the turn) and `discarded`
  at `info`, `absent` at `debug`, because a turn with no claim is every turn on
  every gateway whose app does not claim. A line carries the session the turn
  runs on and the provider half of a login (`oidc`, `basic`) — never the user
  half, never a name, and nothing from the message. The store's shape number is
  unchanged, so both copies of the plugin go on sharing one store.

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

### Fixed

- **A shared Bot Chat describes the person sending the turn, not the one who
  opened it.** The session variables are bound once, when a session is created,
  and were asked before the gateway's live session record; on a chat opened by a
  login the app has no row for, every later turn resolved to that login, found
  nothing and said nothing — while Hermes' own "User:" line went on naming the
  opener. A sender is now worked out per turn from the hook, then the live
  record, then the session variables, then the section's `default`. When that
  person is not the one the chat was told about, the next turn carries them
  once, with a line saying somebody else is sending this turn; a record that
  described nobody no longer stands in the way, and the first person who does
  resolve is introduced. A sender without a row gets nothing about anybody, and
  "the only registered person" answers only where the gateway named nobody at
  all. With `context.session_vars` on, `HERMES_SESSION_USER_*` is rewritten for
  the resolved sender when it names somebody else, so "User:" agrees with the
  section.
