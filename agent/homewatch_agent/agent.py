"""The agent: Home Assistant -> durable outbox -> cloud, plus a heartbeat.

Four concurrent tasks:

    ha         keeps the WebSocket to Home Assistant alive; on every (re)connect it
               loads the entity registry, subscribes to state changes, then snapshots
               all current states (subscribe first, then snapshot, so nothing that
               changes in between is missed; duplicates are harmless)
    process    turns queued HA states into events, filters them, and commits them to
               the SQLite outbox
    upload     drains the outbox to the cloud in batches with backoff; quarantines
               events the cloud rejects
    heartbeat  every interval: ping HA, then tell the cloud "alive, HA ok/not ok"

HA's reader only puts raw states on an in-memory queue; the processor does the rest.
That split matters: the processor sometimes has to ask HA for the entity registry,
and the reader must keep reading to deliver that answer.
"""

import asyncio
import logging
import math
import random
import time
from datetime import UTC, datetime
from typing import Any

import aiohttp

from . import __version__
from .cloud import AuthError, BatchRejected, CloudClient, CloudError, RetryableError
from .config import AgentConfig
from .events import build_event, is_candidate
from .ha import HAClient, HACommandError, HANotConnected
from .outbox import Outbox

log = logging.getLogger("homewatch.agent")

CLOCK_SKEW_WARN_S = 120


class Backoff:
    """Exponential backoff with jitter: ~2s, 4s, 8s ... capped at 5 minutes."""

    def __init__(self, base: float = 2.0, cap: float = 300.0) -> None:
        self.base, self.cap, self.attempt = base, cap, 0

    def next_delay(self) -> float:
        delay = min(self.cap, self.base * 2**self.attempt)
        self.attempt += 1
        return random.uniform(delay / 2, delay)  # jitter: many homes don't retry in lockstep

    def reset(self) -> None:
        self.attempt = 0


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Agent:
    def __init__(self, cfg: AgentConfig, session: aiohttp.ClientSession) -> None:
        self.cfg = cfg
        self.outbox = Outbox(cfg.outbox_path, cfg.max_outbox_rows)
        self.ha = HAClient(session, cfg.ha_ws_url, cfg.ha_token)
        self.cloud = CloudClient(session, cfg.api_url, cfg.api_key)
        self._states: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._platforms: dict[str, str] = {}  # entity_id -> integration, from HA's entity registry
        self._unregistered: set[str] = set()
        self._last_event_id: dict[str, str] = {}  # per entity; skips attribute-only updates
        self._wakeup = asyncio.Event()  # set when the outbox gets something new
        self._ha_ready = asyncio.Event()
        self._last_skew_warning = -math.inf

    async def run(self) -> None:
        tasks = [
            asyncio.create_task(self.ha.run(self._on_ha_connect), name="ha"),
            asyncio.create_task(self._process_states(), name="process"),
            asyncio.create_task(self._upload_loop(), name="upload"),
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                task.result()  # surface the crash; the app's watchdog restarts us
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.outbox.close()

    # ---- Home Assistant -> queue ------------------------------------------------

    async def _on_ha_connect(self, ha: HAClient) -> None:
        self._unregistered.clear()
        await self._refresh_registry()
        await ha.subscribe_events("state_changed", self._on_state_changed)
        states = await ha.call({"type": "get_states"}, timeout=60)
        for state in states:
            if is_candidate(state.get("entity_id", "")):
                self._states.put_nowait(state)
        self._ha_ready.set()
        log.info("home assistant snapshot queued", extra={"entities": len(states)})

    def _on_state_changed(self, event: dict[str, Any]) -> None:
        new_state = (event.get("data") or {}).get("new_state")
        if new_state and is_candidate(new_state.get("entity_id", "")):
            self._states.put_nowait(new_state)

    async def _refresh_registry(self) -> None:
        try:
            result = await self.ha.call({"type": "config/entity_registry/list_for_display"}, timeout=60)
            platforms = {e["ei"]: e.get("pl") for e in result.get("entities", []) if "ei" in e}
        except HACommandError:
            # Older Home Assistant without list_for_display.
            result = await self.ha.call({"type": "config/entity_registry/list"}, timeout=60)
            platforms = {e["entity_id"]: e.get("platform") for e in result if "entity_id" in e}
        self._platforms = platforms
        log.info("entity registry loaded", extra={"entities": len(platforms)})

    # ---- queue -> outbox ---------------------------------------------------------

    async def _process_states(self) -> None:
        while True:
            state = await self._states.get()
            try:
                await self._process_one(state)
            except Exception:  # noqa: BLE001 — never let one odd state stop processing
                log.exception("failed to process a state change")

    async def _process_one(self, state: dict[str, Any]) -> None:
        entity_id = state.get("entity_id", "")
        if entity_id not in self._platforms and entity_id not in self._unregistered:
            try:
                await self._refresh_registry()  # most likely a sensor paired since we last looked
            except (TimeoutError, HANotConnected, HACommandError):
                return
            if entity_id not in self._platforms:
                # No registry entry at all (e.g. a YAML-defined sensor): never eligible,
                # and don't reload the registry for it again until the next reconnect.
                self._unregistered.add(entity_id)
        event = build_event(
            state,
            platform=self._platforms.get(entity_id),
            platforms=self.cfg.platforms,
            device_classes=self.cfg.device_classes,
        )
        if event is None or self._last_event_id.get(entity_id) == event["event_id"]:
            return
        self._last_event_id[entity_id] = event["event_id"]
        if self.outbox.add(event):
            self._wakeup.set()

    # ---- outbox -> cloud ---------------------------------------------------------

    async def _upload_loop(self) -> None:
        backoff = Backoff()
        batch_size = self.cfg.batch_size
        while True:
            try:
                dropped = self.outbox.trim()
                if dropped:
                    log.warning("outbox full; dropped oldest events", extra={"dropped": dropped})

                self._wakeup.clear()  # clear *before* peeking so no wake-up is lost
                batch = self.outbox.peek(batch_size)
                if not batch:
                    try:
                        await asyncio.wait_for(self._wakeup.wait(), timeout=5)
                    except TimeoutError:
                        pass
                    continue

                try:
                    result = await self.cloud.send_events([event for _, event in batch])
                except RetryableError as exc:
                    delay = backoff.next_delay()
                    log.warning(
                        "upload failed; retrying",
                        extra={"error": str(exc), "retry_in_s": round(delay, 1), "outbox_depth": self.outbox.depth()},
                    )
                    await asyncio.sleep(delay)
                    continue
                except AuthError:
                    log.error("cloud rejected the API key; check the agent configuration")
                    await asyncio.sleep(300)
                    continue
                except BatchRejected as exc:
                    if len(batch) > 1:
                        batch_size = max(1, len(batch) // 2)  # narrow down to the bad event
                        log.warning("batch refused; retrying smaller", extra={"batch_size": batch_size})
                    else:
                        self.outbox.dead_letter(batch[0][0], f"request refused: {exc}")
                        log.error("event quarantined", extra={"reason": str(exc)})
                    continue

                backoff.reset()
                batch_size = min(self.cfg.batch_size, batch_size * 2)
                rejected = {item["index"]: item for item in result.get("rejected", [])}
                for index, (row_id, _) in enumerate(batch):
                    if index in rejected:
                        self.outbox.dead_letter(row_id, str(rejected[index].get("errors")))
                self.outbox.ack(row_id for index, (row_id, _) in enumerate(batch) if index not in rejected)
                if rejected:
                    log.error("events quarantined as invalid", extra={"count": len(rejected)})
                log.info(
                    "uploaded events",
                    extra={
                        "accepted": result.get("accepted"),
                        "duplicates": result.get("duplicates"),
                        "rejected": len(rejected),
                        "outbox_depth": self.outbox.depth(),
                    },
                )
            except Exception:  # noqa: BLE001 — keep draining whatever happens
                log.exception("upload loop error")
                await asyncio.sleep(5)

    # ---- heartbeat ---------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        interval = self.cfg.heartbeat_interval_s
        # Give HA a moment on startup so a fresh install doesn't report "HA down" first.
        try:
            await asyncio.wait_for(self._ha_ready.wait(), timeout=min(30.0, interval))
        except TimeoutError:
            pass

        healthy = None
        next_at = time.monotonic()
        while True:
            try:
                ha_ok = await self.ha.ping(timeout=min(10.0, interval / 2))
                response = await self.cloud.heartbeat(
                    {
                        "sent_at": _utcnow().isoformat(),
                        "agent_version": __version__,
                        "ha_ok": ha_ok,
                        "outbox_depth": self.outbox.depth(),
                        "dead_letters": self.outbox.dead_count(),
                    }
                )
                self._check_clock(response.get("server_time"))
                if healthy is not True:
                    log.info("heartbeat ok", extra={"ha_ok": ha_ok})
                healthy = True
            except AuthError:
                log.error("cloud rejected the API key; check the agent configuration")
                healthy = False
            except CloudError as exc:
                if healthy is not False:
                    log.warning("heartbeat failed", extra={"error": str(exc)})
                healthy = False
            except Exception:  # noqa: BLE001
                log.exception("heartbeat loop error")
                healthy = False

            next_at = max(next_at + interval, time.monotonic())
            await asyncio.sleep(next_at - time.monotonic())

    def _check_clock(self, server_time: str | None) -> None:
        """Warn (at most hourly) when the Pi's clock disagrees with the server's."""
        if not server_time:
            return
        try:
            skew = (_utcnow() - datetime.fromisoformat(server_time.replace("Z", "+00:00"))).total_seconds()
        except ValueError:
            return
        if abs(skew) > CLOCK_SKEW_WARN_S and time.monotonic() - self._last_skew_warning > 3600:
            self._last_skew_warning = time.monotonic()
            log.warning("system clock is off; event times will be wrong", extra={"skew_s": round(skew)})
