# Contributing

## Running the tests

```
python -m pytest --rootdir=tests tests
```

`--rootdir=tests` is not optional. The repo root is the plugin package, because
that is how Hermes loads a plugin, so it has an `__init__.py`; without the flag
pytest walks up from `tests/`, finds it, and tries to import the root as a module
called `__init__`.

## Validating against a real Hermes

The two checks that matter, both of which run the real discovery path:

```
hermes plugins validate /path/to/hermie-plugin   # the catalog admission gate
hermes plugins doctor   /path/to/hermie-plugin   # imports it and calls register()
```

`doctor` catches the things unit tests cannot: a hook name that does not exist,
a callback without `**kwargs`, and drift between `provides_hooks` in the manifest
and what `register()` actually registers.

## House rules

- **Every hook callback takes `**kwargs`.** Hermes inspects a callback's
  signature and passes only the parameters it declares, so a narrow signature
  silently stops receiving fields that are added later.
- **Never block in a hook.** They sit on the agent's own path, and `pre_tool_call`
  fails *closed* — a slow callback blocks a tool. Build a decision, hand it to
  the queue, return.
- **Never write the `hermie-app` ui_meta key.** It belongs to the app, which
  holds a compare-and-swap revision for it. `uimeta.write_key` refuses it.
- **A capability is claimed only when it can be honoured on this gateway.** Not
  when the code supports it somewhere.
- **Reaching into Hermes internals goes through `Runtime`.** Modules take the
  runtime, not `ctx`, so the places this plugin depends on Hermes stay
  countable — and so the fake in `tests/test_plugin.py` stays small. If that
  fake has to grow, say why in the pull request.
- **State changes need a migration.** Bump `STATE_VERSION`, add the entry to
  `MIGRATIONS`, and add a test. A state file from an unknown future version is
  left alone, never overwritten.

## Commits

Conventional commits, present tense, describing the behaviour that changed
rather than the files that moved.
