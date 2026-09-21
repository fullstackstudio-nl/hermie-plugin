# Hermie plugin

Hermie's gateway-side companion. It runs inside `hermes serve` as a Hermes
plugin and does two things today: it sends **push notifications** to the devices
that registered themselves, and it puts a little **context about the person and
their device** into a bot's system prompt.

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
| **A bot wrote something** | a notification, unless the app says somebody is reading that chat |
| **A bot is asking for approval** | a notification with Allow and Deny, never suppressed |
| **A bot asked a question** | a notification, never suppressed |
| **A turn finished or failed** | a notification, if the device asked for those |
| **A cron job delivered** | a notification, recognised by the session's platform |

By default a notification says **who and what kind** — a bot's name and an event
type — and nothing about what was said. That is a deliberate default: a
notification is rendered on a lock screen by Apple, Google or a browser vendor.
Turn previews on per device in the app when you want the text.

Tapping Allow does not approve anything by itself. The app opens, connects to
the gateway, re-reads the open requests, and answers only if that request is
still open and still says what the notification said it did.

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
          types: [message, request, cron, turn_done, turn_failed]

          # "device" honours each device's own preview switch.
          # "never" forbids message text gateway-wide, whatever a device asked.
          preview: device

          # How recent a device's "I am looking at a chat" heartbeat must be
          # before a new-message notification is held back.
          attached_window_seconds: 90

          # Grace period before a message notification goes out, so an app that
          # is opening can claim the chat first.
          delay_seconds: 5

          # Web Push only. The key is created on first use if this is empty.
          vapid_key_path: ""
          vapid_contact: "mailto:you@example.com"
        context:
          max_chars: 1200
          # Whose context to use when the gateway cannot say who is asking.
          # Empty is fine when only one person is registered.
          default_user: ""
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

On a gateway with authentication in front of it the plugin knows **which**
person sent a turn and picks their context. On a gateway with no authentication
there is no user identity to read, so it uses the default — which is the right
answer when one person is registered, and no answer at all when several are.

> **On a shared gateway, read this.** The app keeps one metadata key per person,
> but Hermes' profile metadata is per profile: every key on it is handed to every
> client that can read the profile, so everyone with access to the gateway can
> see everyone else's context section and push registrations. A push token is only an address; a
> context section is a name, a device and whatever somebody wrote about
> themselves. Do not fill it in on a gateway you share with people you would not
> show it to.

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

One thing the daemon could do and this cannot: **bot-to-bot DMs**. Hermes fires
no hook when one arrives, so the plugin does not advertise that type and a device
that asked for it never receives one. Everything else moved across.

Run both and you will be notified twice. Pick one.

## Development

```
python -m pytest --rootdir=tests tests
```

The repo root is the plugin package — Hermes imports the directory — so `tests/`
builds the same package rather than inventing an import path production never
uses, and the run is rooted below the root's `__init__.py`.

## Licence

MIT. See [LICENSE](LICENSE).
