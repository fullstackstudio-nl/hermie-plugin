"""The push module: hooks in, notifications out.

The sending itself happens on a worker thread. Every hook this module registers
sits on the agent's own path — `post_llm_call` runs between the model answering
and the user seeing it — so nothing here may wait on a network. The hook builds
a decision, hands it to a bounded queue and returns; a daemon thread does the
talking. A full queue drops with a warning, because a notification that is
already late is worth less than a turn that is still fast.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import contract
from . import events, expo, webpush
from .registrations import Section, read_sections

logger = logging.getLogger(__name__)

NAME = "push"

QUEUE_SIZE = 256


class PushModule:
    """One instance per loaded plugin, holding the sender thread and the settings."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.queue: "queue.Queue[Optional[tuple]]" = queue.Queue(maxsize=QUEUE_SIZE)
        self.worker: Optional[threading.Thread] = None
        self.lock = threading.Lock()
        self._vapid_key = None

    # -- settings ------------------------------------------------------------
    #
    # `config_schema` in the manifest documents these and warns on a wrong type,
    # but Hermes never merges its `default` into the config — so every default
    # that actually applies is the one written here.

    @property
    def enabled_types(self) -> tuple:
        raw = self.runtime.config("push.types", list(events.TYPES))
        wanted = {str(item) for item in raw} if isinstance(raw, list) else set(events.TYPES)
        return tuple(name for name in events.TYPES if name in wanted)

    @property
    def gateway_preview(self) -> str:
        value = str(self.runtime.config("push.preview", "device"))
        return value if value in ("never", "device") else "device"

    @property
    def attached_window(self) -> int:
        return int(self.runtime.config("push.attached_window_seconds", 90) or 90)

    @property
    def delay_seconds(self) -> int:
        return int(self.runtime.config("push.delay_seconds", 5) or 5)

    # -- capabilities --------------------------------------------------------

    def capabilities(self) -> List[str]:
        """What this gateway can actually do, not what the code could do.

        Web Push is claimed only when the signing library imports here, because
        an app that sees the capability will offer the browser a subscribe
        button, and a button that cannot work is worse than one that is absent.
        """
        # The mute list is honoured whether or not one exists yet: the string
        # says this gateway will obey a mute, which is what the app needs to
        # know before it offers the switch.
        found = [
            contract.CAP_PUSH_EXPO,
            contract.CAP_PUSH_MUTE,
            contract.CAP_PUSH_SEEN_PER_CHAT,
        ]
        if webpush.available():
            found.append(contract.CAP_PUSH_WEBPUSH)
        if self.gateway_preview == "device":
            found.append(contract.CAP_PUSH_PREVIEW)
        if "turn_done" in self.enabled_types:
            found.append(contract.CAP_PUSH_TURN_DONE)
        if "turn_failed" in self.enabled_types:
            found.append(contract.CAP_PUSH_TURN_FAILED)
        return found

    # -- the queue -----------------------------------------------------------

    def offer(self, notification: Optional[events.Notification], *, delay: bool = False) -> None:
        """Hand one decision to the worker. Never raises, never blocks."""
        if notification is None:
            return
        try:
            due = time.time() + (self.delay_seconds if delay else 0)
            self.queue.put_nowait((notification, due))
        except queue.Full:
            logger.warning("hermie: push queue is full; dropped a %s notification", notification.type)
            return
        self._ensure_worker()

    def _ensure_worker(self) -> None:
        with self.lock:
            if self.worker is not None and self.worker.is_alive():
                return
            self.worker = threading.Thread(target=self._run, name="hermie-push", daemon=True)
            self.worker.start()

    def stop(self) -> None:
        with self.lock:
            worker = self.worker
            self.worker = None
        if worker is not None and worker.is_alive():
            try:
                self.queue.put_nowait(None)
            except queue.Full:
                pass

    def _run(self) -> None:
        while True:
            item = self.queue.get()
            if item is None:
                return
            notification, due = item
            # The delay is ADR-0017's grace period: an app that is opening gets
            # a moment to write its `seen` heartbeat and claim the chat.
            remaining = due - time.time()
            if remaining > 0:
                time.sleep(min(remaining, 30))
            try:
                self.deliver(notification)
            except Exception as exc:
                logger.warning("hermie: push delivery failed: %s", exc)

    # -- delivery ------------------------------------------------------------

    def section(self) -> Section:
        return read_sections(self.runtime.app_sections())

    def deliver(self, notification: events.Notification) -> int:
        """Send one notification to everybody who asked for it. Returns the count."""
        state = self.runtime.state
        if not state.claim(notification.event_id):
            return 0

        targets = events.recipients(
            notification,
            self.section(),
            now=time.time(),
            attached_window_seconds=self.attached_window,
            enabled_types=self.enabled_types,
            gateway_preview=self.gateway_preview,
            retired=state.is_retired,
        )
        if not targets:
            return 0

        expo_batch: List[Dict[str, Any]] = []
        expo_owners: List[str] = []
        sent = 0

        for registration, preview in targets:
            title, body = notification.rendered(preview=preview)
            payload = notification.payload(preview=preview)
            if registration.transport == "expo":
                if not expo.is_expo_token(registration.token or ""):
                    logger.warning(
                        "hermie: registration %s is not a usable Expo token; skipping",
                        registration.installation_id,
                    )
                    continue
                expo_batch.append(expo.message_for(registration.token or "", payload, title=title, body=body))
                expo_owners.append(registration.installation_id)
            else:
                sent += self._send_webpush(registration, payload, title, body)

        if expo_batch:
            sent += self._send_expo(expo_batch, expo_owners)

        state.save()
        return sent

    def _send_expo(self, batch: List[Dict[str, Any]], owners: List[str]) -> int:
        sent = 0
        for index in range(0, len(batch), expo.MAX_BATCH):
            window = batch[index : index + expo.MAX_BATCH]
            tickets = expo.send(window)
            for installation_id, ticket in zip(owners[index : index + expo.MAX_BATCH], tickets):
                if ticket.status == "ok":
                    sent += 1
                elif ticket.device_gone:
                    logger.info("hermie: expo says %s is gone; retiring it", installation_id)
                    self.runtime.state.retire(installation_id, "DeviceNotRegistered")
                else:
                    logger.warning(
                        "hermie: expo refused a message for %s: %s %s",
                        installation_id, ticket.error or "?", ticket.message,
                    )
        return sent

    def vapid_key(self):
        if self._vapid_key is None:
            configured = str(self.runtime.config("push.vapid_key_path", "") or "")
            path = Path(configured) if configured else (self.runtime.data_dir / "vapid.pem")
            self._vapid_key = webpush.load_or_create_key(path)
        return self._vapid_key

    def _send_webpush(self, registration, payload: Dict[str, Any], title: str, body: str) -> int:
        if not webpush.available():
            return 0
        try:
            key = self.vapid_key()
        except Exception:
            return 0
        result = webpush.send(
            key,
            registration.endpoint or "",
            registration.keys.get("p256dh", ""),
            registration.keys.get("auth", ""),
            {**payload, "title": title, "body": body},
            contact=str(self.runtime.config("push.vapid_contact", "") or ""),
        )
        if result.device_gone:
            logger.info("hermie: %s says %s is gone; retiring it", result.status, registration.installation_id)
            self.runtime.state.retire(registration.installation_id, f"http-{result.status}")
            return 0
        if not result.ok:
            logger.warning("hermie: web push returned %s for %s", result.status, registration.installation_id)
            return 0
        return 1

    # -- hooks ---------------------------------------------------------------
    #
    # Every callback takes **kwargs. Hermes inspects a callback's signature and
    # passes only the fields it declares, so a narrow signature silently stops
    # receiving fields that are added later; **kwargs is the forward-compatible
    # shape, and `hermes plugins doctor` checks for it.

    def on_post_llm_call(self, **kwargs: Any) -> None:
        bot = self.runtime.bot_name()
        session_id = str(kwargs.get("session_id") or "")
        platform = str(kwargs.get("platform") or "")
        notification = events.from_assistant_message(
            bot=bot,
            session_id=session_id,
            turn_id=str(kwargs.get("turn_id") or ""),
            assistant_response=kwargs.get("assistant_response"),
            at=int(time.time()),
        )
        if notification is None:
            return
        # Hermes fires no cron-specific hook, so a cron delivery is recognised
        # only by the platform its session runs under. This is a heuristic and
        # DESIGN.md says so; when it misfires the message is notified as a
        # message, which is what it also is.
        if "cron" in platform.lower():
            notification = events.Notification(**{**notification.__dict__, "type": "cron", "body": "Cron delivered"})
        self.offer(notification, delay=True)

    def on_session_end(self, **kwargs: Any) -> None:
        self.offer(
            events.from_session_end(
                bot=self.runtime.bot_name(),
                session_id=str(kwargs.get("session_id") or ""),
                turn_id=kwargs.get("turn_id"),
                completed=kwargs.get("completed"),
                failed=kwargs.get("failed"),
                interrupted=kwargs.get("interrupted"),
                at=int(time.time()),
            ),
            delay=True,
        )

    def on_pre_approval_request(self, **kwargs: Any) -> None:
        if str(kwargs.get("surface") or "") == "smart":
            # The smart path answers itself; nobody is being asked.
            return
        self.offer(
            events.from_approval(
                bot=self.runtime.bot_name(),
                session_key=str(kwargs.get("session_key") or ""),
                description=kwargs.get("description"),
                request_id=kwargs.get("request_id"),
                turn_id=kwargs.get("turn_id"),
                at=int(time.time()),
            )
        )

    def on_pre_tool_call(self, **kwargs: Any) -> None:
        if str(kwargs.get("tool_name") or "") != "clarify":
            return
        args = kwargs.get("args") if isinstance(kwargs.get("args"), dict) else {}
        self.offer(
            events.from_clarify(
                bot=self.runtime.bot_name(),
                session_id=str(kwargs.get("session_id") or ""),
                tool_call_id=kwargs.get("tool_call_id"),
                question=args.get("question") or args.get("prompt"),
                at=int(time.time()),
            )
        )


def register(ctx, runtime) -> PushModule:
    module = PushModule(runtime)
    ctx.register_hook("post_llm_call", module.on_post_llm_call)
    ctx.register_hook("on_session_end", module.on_session_end)
    ctx.register_hook("pre_approval_request", module.on_pre_approval_request)
    ctx.register_hook("pre_tool_call", module.on_pre_tool_call)
    ctx.on_unload(module.stop)
    return module
