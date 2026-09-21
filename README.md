# Hermie plugin

Hermie's gateway-side companion. It runs inside `hermes serve` as a Hermes
plugin and does two things today: it sends **push notifications** to the devices
that registered themselves, and it puts a little **context about the person and
their device** into a bot's system prompt. `/me` says who it thinks you are.

It has no inbound port, no relay, no account, and no credential of its own. The
devices that want notifications write themselves into the gateway's own profile
metadata; the plugin reads that from the inside.

```
hermes plugins install fullstackstudio-nl/hermie-plugin --enable
hermes gateway restart
```

That is the whole install. Hermes clones the repo into
`$HERMES_HOME/plugins/hermie/`, scans it, and enables it.

## What you get

| | |
|---|---|
| **A bot wrote something** | a notification, except on the device that says it is reading that chat |
| **A bot is asking for approval** | a notification with Allow and Deny, never suppressed |
| **A bot asked a question** | a notification, never suppressed |
| **A turn finished or failed** | a notification, if the device asked for those |
| **A cron job delivered** | a notification, recognised by the scheduler's own marker |
| **A cron job finished or failed** | a notification, if the device asked for those |
| **A bot you muted** | nothing, on any of your devices, until the mute lapses |

By default a notification says **who and what kind** — a bot's name and an event
type — and nothing about what was said. That is a deliberate default: a
notification is rendered on a lock screen by Apple, Google or a browser vendor.
Turn previews on per device in the app when you want the text.

Tapping Allow does not approve anything by itself. The app opens, connects to
the gateway, re-reads the open requests, and answers only if that request is
still open and still says what the notification said it did.

## Capabilities

The app does not test this plugin's version number. It reads a list of strings
out of the gateway's own `ui_meta`, under the `hermie-plugin` key, and asks for
a feature only when the string naming it is there. A capability is published
only when it can be honoured **on this gateway** — Web Push only where the
signing library imports, a type only where it is switched on — because a button
that cannot work is worse than one that is absent.

| Capability | Means |
|---|---|
| `push.expo` | Expo notifications can be sent |
| `push.webpush` | Web Push can be signed here |
| `push.preview` | a device may ask for message text in its payload |
| `push.mute` | a mute written by the app will be obeyed |
| `push.seen.per_chat` | a `{bot, at}` heartbeat is understood, so suppression is per chat |
| `push.type.turn_done` | "a turn finished" is switched on |
| `push.type.turn_failed` | "a turn failed" is switched on |
| `push.type.cron_done` | "a scheduled job finished" is switched on |
| `push.type.cron_failed` | "a scheduled job failed" is switched on |
| `push.cron.signal` | a cron run is recognised by the scheduler's marker, not by a guess |
| `ui_meta.per_user` | `hermie-app:<user id>` is read, so the app may move its bag |
| `context.system_prompt` | context is put into the bot's system prompt |
| `context.per_bot` | a per-bot note is rendered for the bot it names |
| `context.live` | a context edit reaches an **open** chat on its next turn |
| `command.me` | `/me` was accepted by this gateway |
| `plugin.update_check` | the advert carries the newest release tag |

An absent list means an absent plugin. A plugin too old to publish one, a
plugin that is installed but disabled, and no plugin at all are
indistinguishable, and all three mean the same thing: do not offer the feature.

## Requirements

- Hermes with the plugin hook surface (0.21 or newer; tested on 0.21.x).
- Nothing else. Web Push signing uses `cryptography`, which the Hermes runtime
  already ships; on a runtime without it the plugin simply does not advertise
  Web Push, and Expo keeps working.

## Configuration

Everything is optional. Settings live under `plugins.entries.hermie.settings` in
`config.yaml`:

```yaml
plugins:
  enabled: [hermie]
  entries:
    hermie:
      settings:
        modules:
          push: true
          context: true
        push:
          # Which event types this gateway may notify about at all. A device
          # still has to ask for a type before it receives one.
          types: [message, request, cron, cron_done, cron_failed,
                  turn_done, turn_failed]

          # "device" honours each device's own preview switch.
          # "never" forbids message text gateway-wide, whatever a device asked.
          preview: device

          # How recent a device's "I am looking at this chat" heartbeat must
          # be before a new-message notification is held back on that device.
          attached_window_seconds: 90

          # Grace period before a message notification goes out, so an app that
          # is opening can claim the chat first.
          delay_seconds: 5

          # Web Push only. The key is created on first use if this is empty.
          vapid_key_path: ""
          vapid_contact: "mailto:you@example.com"
        update:
          # Ask this repository, at most once an hour, whether a newer release
          # exists, and put the answer in the advert. Off by default; the advert
          # carries the version and the installed commit either way.
          check: false
        context:
          max_chars: 1200
          # Whose context to use when the gateway cannot say who is asking.
          # Empty is fine when only one person is registered.
          default_user: ""

          # Fill in Hermes' own HERMES_SESSION_USER_ID, _ID_ALT and _NAME for a
          # turn when they are empty and the plugin knows who is asking, so a
          # tool that reads them sees the person rather than nobody. Never
          # overwrites a value the gateway set. Hermes runs this hook on a
          # worker thread unless plugins.hook_callback_timeout is 0, and a
          # session variable set there does not reach the turn — see DESIGN.md.
          session_vars: true
```

Check what the plugin thinks it can do:

```
hermes plugins list
hermes plugins show hermie
hermes plugins doctor $HERMES_HOME/plugins/hermie
```

## Device context

The app can store, per person: a display name, free text about themselves,
device model and OS, app version, timezone and locale, and optional per-bot
notes. The plugin puts that in the bot's system prompt once per session, so it
does not appear in the transcript and does not grow with the conversation.

**Changing it reaches a chat that is already open.** Hermes renders a plugin's
prompt section once per session and then replays it, so an edit made mid-chat
would otherwise wait for the next one. Instead the turn after the edit carries
the new text, saying that it replaces what the prompt says; emptying it is
retracted in words, since the frozen copy cannot be taken back out. On every
other turn this costs one `stat` of `profile.yaml` and reads nothing.

On a gateway with authentication in front of it the plugin works out **which**
person sent a turn and picks their context. It asks three places in order: the
sender Hermes hands the hook, the login bound into the session variables, and —
because the dashboard route fills in neither while knowing perfectly well who
logged in — the gateway's own record of this live session. A login carries the
provider that issued it (`oidc:max`), the app registers the bare id (`max`), and
either spelling finds the other.

On a gateway with no authentication there is no identity to read anywhere, so it
uses the default — which is the right answer when one person is registered, and
no answer at all when several are.

### `/me`

Type `/me` in a session to see what the bot actually resolved. It answers on the
spot, without calling the model:

```
Hermie context for jurist

Talking to: Sebas
Worked out: from the login the gateway admitted this session under
Login:      self-hosted:ef11a9 → matched the registered id ef11a9
Device:     iPhone 17 Pro running iOS 27, app 1.4.0
Dates:      Europe/Amsterdam, nl-NL
About:      Runs FullStack Studio. Prefers short answers.
This bot:   Always cite the article number.
From:       hermie-app:ef11a9, updated 2026-09-21 02:19 UTC
```

When it says `nobody` it also says why, and what to do about it: accept the
sharing notice in Hermie's Settings → Context, then send a message.

> **On a shared gateway, read this.** The app keeps one metadata key per person,
> but Hermes' profile metadata is per profile: every key on it is handed to every
> client that can read the profile, so everyone with access to the gateway can
> see everyone else's context section and push registrations. A push token is only an address; a
> context section is a name, a device and whatever somebody wrote about
> themselves. Do not fill it in on a gateway you share with people you would not
> show it to.

### Scheduled jobs

A cron run is an ordinary agent session — Hermes fires no cron-specific hook —
so the plugin has to recognise one itself. It asks the scheduler's own marker
first: the `cron:<job id>:<execution id>` task id, then the `HERMES_CRON_SESSION`
variable, then the session id. The `platform` string is still read, last, and a
notification says which kind of answer it got, so the app can tell a fact from a
guess.

A notification carries the **job id** when the signal had one, which is the name
you gave the job.

> **`cron_failed` means the run's turn failed, not that the job did.** The
> scheduler decides a job's real outcome after the agent has gone — an
> exception, a delivery that did not go through, a quota hold — and fires
> nothing a plugin could hear. What a turn can see is its own failure and the
> `[CRON_FAILURE]` line an agent writes about itself. So a failed job is
> sometimes silent here, and never falsely reported.

## How this relates to Hermie Web's `--push`

`hermie-web --push` did the same job from outside: a second process holding its
own WebSocket to the gateway, its own long-lived credential in its own state
file, resuming every Bot Chat to watch it.

This plugin replaces it as the default path, and is strictly smaller:

| | `hermie-web --push` | this plugin |
|---|---|---|
| Processes | two | one |
| Credential | a gateway credential in a state file | none |
| Bot Chats held open | all of them, permanently | none |
| Events | by watching a transcript | from the gateway's own hooks |
| Install | a release artefact, a unit file | one command |

One thing the daemon could do and this cannot: notify about **one bot writing to
another**. Hermes fires no hook when that happens, so there is no such
notification and no switch pretending there could be. Everything else moved
across.

Run both and you will be notified twice. Pick one.

## Development

```
python -m pytest --rootdir=tests tests
```

The repo root is the plugin package — Hermes imports the directory — so `tests/`
builds the same package rather than inventing an import path production never
uses, and the run is rooted below the root's `__init__.py`.

## Updating

```
hermes plugins update hermie
hermes gateway restart
```

That is the whole update. It git-pulls the tree Hermes cloned, so nothing about
the install changes and **nothing is lost**: push registrations live in the
app's own metadata rather than in the plugin, and the plugin's small state file
(sent-event ids and retired devices) carries a version and is migrated forward
rather than replaced. A state file from a version older than this build is
migrated; one from a version this build has never heard of is left on disk
untouched and treated as empty for the run — losing dedupe history costs one
duplicate notification, while overwriting a newer file costs a downgrade its
data.

The advert tells the app when that command is worth running. It carries this
build's `version`, the commit the installed tree sits at, the repository it came
from, and the oldest app version this build can serve — enough for the app to
work out that a newer release exists, from the phone, on its own network.

If you would rather the gateway found out itself:

```yaml
plugins:
  entries:
    hermie:
      settings:
        update:
          check: true
```

One GET of this repository's public releases URL, at most once an hour and
cached across restarts, nothing identifying sent, and every failure treated as
"no newer release". It is off by default because a gateway that reaches out
without being asked is not what this plugin is.

There is no self-update and there will not be one. A plugin that can rewrite its
own code is a plugin that can rewrite its own code.

## Licence

MIT. See [LICENSE](LICENSE).
