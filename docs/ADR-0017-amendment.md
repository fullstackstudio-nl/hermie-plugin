# Proposed amendment to ADR-0017

Intended to be merged into `docs/adr/0017-push-through-hermie-web.md` in the app
repository. The decision to notify from beside the gateway stands; what changes
is *which* process does it, and two of the four event types turn out not to be
producible at all.

---

## Amendment, 2026-09-21: push comes from a Hermes plugin, and Hermie Web's `--push` becomes the fallback

### Why this changed

ADR-0017 chose Hermie Web because it was "the only always-on thing this project
owns". That was true of the things *we* own. It was not true of the gateway,
which is always on by definition and which turns out to have a documented plugin
surface: hooks for the agent's lifecycle, a per-profile state store, a bounded
system-prompt contribution, and discovery through
`hermes plugins install <owner/repo> --enable`.

A plugin inside `hermes serve` removes, rather than adds:

- **No second process.** One thing to run instead of two.
- **No credential.** ADR-0017 accepted that "the daemon's credential is a gateway
  credential… anyone who can read the daemon's state file has that access". In
  process there is no such file, because there is nothing to authenticate to.
- **No pinned sessions.** The entire "one more thing to run, and it holds
  sessions open" consequence disappears. The daemon resumed every Bot Chat to
  watch it, and upstream never evicts a session whose transport is alive, so the
  resident set grew with the bot list. Hooks fire where the work already
  happens; nothing is resumed and nothing is held.
- **No OIDC problem.** ADR-0017's "an OIDC gateway whose provider issues no
  refresh token cannot run push" was a property of needing a long-lived
  credential. In process, that condition is gone entirely.

### What is decided

**Push is delivered by the `hermie` plugin, installed into the gateway with
`hermes plugins install fullstackstudio-nl/hermie-plugin --enable`.**
`hermie-web --push` remains supported for a gateway where a plugin cannot be
installed, and the two must not both run: they would notify the same device
twice.

The registration schema, the `seen` heartbeat, the payload policy and the
validated-action rule are **unchanged**. A device that registered for the daemon
is registered for the plugin. The app needs no change to keep working.

### What this costs, and it is not nothing

**Two of the four event types cannot be produced by a plugin.**

- **Bot-to-bot DM.** Hermes fires no hook when one arrives (`tools/bot_mode_dm.py`
  has no fire site). The `dm` type stays in the schema — a device may still ask
  for it and the daemon may still send it — but the plugin does not advertise it
  and never sends one. This is a regression against ADR-0017 as written, and it
  is accepted because the alternative is keeping a second process alive for one
  event type. Closing it means a hook upstream.
- **Cron delivery.** There are no hook fire sites in `hermes_cli/cron.py`. A cron
  run is an ordinary agent session, so the turn hooks fire inside it, but nothing
  carries a job id. The plugin recognises a cron delivery from the session's
  platform string, which is a heuristic: when it misfires, the message is
  notified as a `message`, which it also is.

**The plugin runs with the gateway's own trust.** It is in-process. It can see
what the gateway sees. Read against ADR-0017's own accepted risk, this is a
reduction rather than an addition — the daemon needed a gateway credential on
disk to obtain the same access, and now nothing does.

### Two additions

**Two new types: `turn_done` and `turn_failed`.** `on_session_end` reports
whether a turn completed, failed, or was interrupted. A finished long task and a
turn that died are both worth a buzz, and an *interrupted* turn is not — somebody
pressed stop, and they know. Both follow the existing rule that an absent type is
off, so no device that predates them starts receiving them.

**A capability advert, under a new `ui_meta` key.** ADR-0017 made
`hermie-app.push.endpoint` informational, written by the daemon. The plugin
writes a richer advert under its **own** key, `hermie-plugin`:

```json
{"v": 1, "version": "0.1.0",
 "capabilities": ["push.expo", "push.webpush", "push.preview",
                  "push.type.turn_done", "push.type.turn_failed",
                  "context.system_prompt"],
 "modules": {"push": "on", "context": "on", "presence": "planned"},
 "updatedAt": 1790001453}
```

Its own key, because `hermie-app` belongs to the app and carries a
compare-and-swap revision per ADR-0016; a write from the gateway side would make
the app's next write fail. Its own key is invisible to that revision.

The app reads capability **strings** and never compares version numbers, so an
older plugin simply offers less and a newer one adds strings an older app does
not ask for. An absent advert means an absent plugin, which means: do not offer
the feature. This is the mechanism that lets a gateway nobody ever updates keep
working with an app that keeps shipping.

### A second thing the plugin does

The same plugin injects **device context** — a person's name, device, timezone,
locale and their own free text — into a bot's system prompt, from a `context`
section the app writes beside the registrations under `hermie-app`. It is
rendered once per session and never appears in the transcript.

This belongs in the same plugin rather than a second one for the same reason the
modules exist at all: a person installs a plugin once. It is recorded here
because it puts something new into `hermie-app` and because it changes the
privacy picture — ADR-0017's accepted "`ui_meta` is per profile, not per user"
now covers a display name and free text somebody wrote about themselves, not just
a push token and a set of toggles. On a shared gateway that is a real difference,
and the app should say so where the field is filled in.

### What is unchanged

Everything else in ADR-0017: the registration schema and its `v`, the app never
talking to the notifier, the payload saying who rather than what, `preview` being
per device, `seen` being a heartbeat rather than a protocol fact, requests and
cron deliveries never being suppressed, and every action being re-validated
against the gateway's own open requests before it is answered.
