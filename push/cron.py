"""Whether this turn belongs to a scheduled job, and how confident that answer is.

Hermes fires no cron hook. A cron run is an ordinary agent session, so the turn
hooks fire inside it and the plugin has to work out for itself that the turn had
nobody waiting on it. Until now it did that by looking for "cron" in the
session's `platform` string, which is a guess about a free-text field.

There are two better signals on the very same turn, and core itself prefers them:

- **`HERMES_CRON_SESSION`** — a session variable the cron scheduler binds to
  `"1"` for the duration of a run and to `""` everywhere else. This is the test
  core uses in `tools/approval_context.py::_is_cron_approval_context`, and
  `tools/approval.py` says in as many words that cron beats a platform marker
  because cron binds the platform for delivery routing only. It survives the
  bounded-hook worker thread, because Hermes dispatches a callback through
  `contextvars.copy_context().run(...)` and a copied context carries the values
  it was copied from. (Reading is safe there; it is *writing* one that is lost,
  which is the whole limit of `context/session_vars.py`.)
- **`task_id`** — already a kwarg on `post_llm_call`, `pre_llm_call` and
  `on_session_end`. The scheduler mints it as `cron:<job id>:<execution id>`, so
  it is both a signal and the only place a plugin can learn *which* job ran.

Both are asked before the platform string, which stays as the last resort it
always was. A turn that answers on either of the first two is not a heuristic
any more, and the advert says so.

**What is still not knowable in-process, and will not be.** A cron job's real
outcome is decided *after* the agent is gone: an exception out of `run_job`, a
delivery that failed, a quota hold, a lost claim, the `failure_streak` and
`last_status` on the job record, the executions ledger. None of it fires a hook
and none of it is reachable from a turn. What a turn can see is the turn: the
`[CRON_FAILURE]` marker the agent itself wrote, and whether the turn completed.
So `cron_failed` means "this run's turn failed or the agent declared failure",
which is a subset of "this job failed", and the README says that rather than
implying the plugin is watching the scheduler.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

# The session variable the cron scheduler binds for the length of a run. Core
# reads this same name to decide that an approval is unattended.
CRON_SESSION = "HERMES_CRON_SESSION"

# `cron:<job id>:<execution id>`, minted in the scheduler and handed to every
# turn hook as `task_id`.
TASK_PREFIX = "cron:"

# `cron_<job id>_<YYYYmmdd_HHMMSS>`, the session id a cron run is opened under.
SESSION_PREFIX = "cron_"

# What the agent writes on the first line, alone, to declare its own failure.
FAILURE_MARKER = "[CRON_FAILURE]"

# Which signal answered. These are reported so a person debugging a gateway can
# tell a fact from a guess, and so the advert can stop calling this a heuristic.
BY_TASK_ID = "task id"
BY_SESSION_VAR = "cron session variable"
BY_SESSION_ID = "session id"
BY_PLATFORM = "platform string"

# Only the last of them is a guess. `platform` is free text that a gateway, an
# adapter or a person can set to anything.
CERTAIN = (BY_TASK_ID, BY_SESSION_VAR, BY_SESSION_ID)


@dataclass(frozen=True)
class Cron:
    """A turn that belongs to a scheduled job."""

    job_id: str = ""
    source: str = ""

    @property
    def certain(self) -> bool:
        return self.source in CERTAIN


def _truthy(value: Any) -> bool:
    """The scheduler binds exactly ``"1"``; the rest is for a hand-set variable."""
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def job_of(task_id: Any) -> str:
    """The job id out of ``cron:<job id>:<execution id>``, or ``""``."""
    text = str(task_id or "")
    if not text.startswith(TASK_PREFIX):
        return ""
    return text[len(TASK_PREFIX) :].split(":", 1)[0]


def detect(kwargs: Mapping[str, Any], *, session_var: str = "") -> Optional[Cron]:
    """Whether this turn is a cron run, by the most direct signal that answers.

    `session_var` is what `HERMES_CRON_SESSION` currently reads; it is passed in
    rather than read here so the whole decision stays a pure function of what a
    hook was handed and what the gateway said.
    """
    job_id = job_of(kwargs.get("task_id"))
    if job_id:
        return Cron(job_id=job_id, source=BY_TASK_ID)
    if _truthy(session_var):
        return Cron(source=BY_SESSION_VAR)

    session_id = str(kwargs.get("session_id") or "")
    if session_id.startswith(SESSION_PREFIX):
        # `cron_<job id>_<stamp>`. The stamp is the last two underscore-joined
        # parts, so whatever is between the prefix and those is the job id — a
        # job id may itself contain an underscore.
        middle = session_id[len(SESSION_PREFIX) :].rsplit("_", 2)
        return Cron(job_id=middle[0] if len(middle) == 3 else "", source=BY_SESSION_ID)

    if "cron" in str(kwargs.get("platform") or "").lower():
        return Cron(source=BY_PLATFORM)
    return None


def declared_failure(assistant_response: Any) -> bool:
    """Whether the agent itself said this run failed.

    The scheduler accepts the marker only when it is alone on the first line
    (`cron/scheduler.py::_cron_failure_marker_error`), and so does this: a run
    that merely *mentions* the marker mid-paragraph has not declared anything.
    """
    text = str(assistant_response or "")
    return text.splitlines()[0].strip() == FAILURE_MARKER if text.strip() else False


def read_session_var() -> str:
    """`HERMES_CRON_SESSION` as this turn sees it, or ``""`` outside a gateway.

    The accessor lives in the context module because that is where the plugin
    keeps its one piece of knowledge about where Hermes puts session variables.
    Importing it does not switch that module on — it is a plain function with no
    state — and having two copies of "how do I reach Hermes' session variables"
    is how they drift.
    """
    try:
        from ..context.session_vars import hermes_session_context

        module = hermes_session_context()
        return str(module.get_session_env(CRON_SESSION, "") or "") if module is not None else ""
    except Exception:
        return ""
