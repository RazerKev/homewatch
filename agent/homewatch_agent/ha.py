"""Minimal Home Assistant WebSocket API client.

Protocol (https://developers.home-assistant.io/docs/api/websocket):
    server: auth_required -> client: auth -> server: auth_ok | auth_invalid
    then JSON commands, each with an increasing "id"; results and events echo that id.

One background reader task receives every message and routes it: command results
resolve the waiting call's future, subscription events go to the registered callback.
Reconnects forever with exponential backoff and jitter.
"""

import asyncio
import itertools
import json
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

log = logging.getLogger("homewatch.ha")

EventCallback = Callable[[dict[str, Any]], None]


class HAAuthError(Exception):
    """The token was rejected. Retrying quickly won't help."""


class HACommandError(Exception):
    """Home Assistant answered a command with success: false."""


class HANotConnected(Exception):
    """No live connection to Home Assistant right now."""


class HAClient:
    def __init__(self, session: aiohttp.ClientSession, ws_url: str, token: str) -> None:
        self._session = session
        self._url = ws_url
        self._token = token
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ids = itertools.count(1)  # must keep increasing, even across reconnects
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._subscriptions: dict[int, EventCallback] = {}

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    async def run(self, on_connect: Callable[["HAClient"], Awaitable[None]]) -> None:
        """Stay connected forever. `on_connect` runs after each successful (re)connect."""
        backoff = 1.0
        while True:
            established = False
            try:
                established = await self._connect_and_serve(on_connect)
                log.warning("home assistant connection closed")
            except HAAuthError:
                log.error("home assistant rejected the access token; check the configuration")
                backoff = 300.0
            # Network trouble, timeouts, HA restarting, or garbage during the handshake (ValueError).
            except (TimeoutError, aiohttp.ClientError, OSError, ValueError, HACommandError, HANotConnected) as exc:
                log.warning("home assistant unreachable", extra={"error": type(exc).__name__})
            except Exception:  # noqa: BLE001 — anything unexpected: log it and keep reconnecting
                log.exception("unexpected error on the home assistant connection")
            if established:
                backoff = 1.0
            await asyncio.sleep(backoff * random.uniform(0.5, 1.0))
            backoff = min(backoff * 2, 60.0) if backoff < 300 else backoff

    async def _connect_and_serve(self, on_connect: Callable[["HAClient"], Awaitable[None]]) -> bool:
        async with self._session.ws_connect(
            self._url,
            heartbeat=30,  # protocol-level pings; a dead link is noticed within ~a minute
            max_msg_size=64 * 1024 * 1024,  # get_states on a big install is several MB
            timeout=aiohttp.ClientWSTimeout(ws_close=10),
        ) as ws:
            await self._authenticate(ws)
            self._ws = ws
            reader = asyncio.create_task(self._read_loop(ws), name="ha-reader")
            try:
                await on_connect(self)
                log.info("home assistant connected")
                await reader
            finally:
                reader.cancel()
                self._ws = None
                self._subscriptions.clear()
                self._fail_pending()
        return True

    async def _authenticate(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        hello = await self._receive_json(ws)
        if hello.get("type") != "auth_required":
            raise HACommandError("unexpected handshake")
        await ws.send_json({"type": "auth", "access_token": self._token})
        reply = await self._receive_json(ws)
        if reply.get("type") == "auth_invalid":
            raise HAAuthError()
        if reply.get("type") != "auth_ok":
            raise HACommandError("unexpected handshake")

    @staticmethod
    async def _receive_json(ws: aiohttp.ClientWebSocketResponse) -> dict[str, Any]:
        msg = await ws.receive(timeout=10)
        if msg.type != aiohttp.WSMsgType.TEXT:
            raise HANotConnected()
        return json.loads(msg.data)

    async def _read_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.ERROR:
                    break
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    log.warning("ignoring a malformed message from home assistant")
                    continue
                # HA may coalesce several messages into one JSON array.
                for item in data if isinstance(data, list) else [data]:
                    self._dispatch(item)
        finally:
            self._fail_pending()

    def _dispatch(self, message: dict[str, Any]) -> None:
        msg_id = message.get("id")
        kind = message.get("type")
        if kind == "event":
            callback = self._subscriptions.get(msg_id)  # type: ignore[arg-type]
            if callback is not None:
                try:
                    callback(message.get("event") or {})
                except Exception:  # noqa: BLE001 — one bad event must not kill the connection
                    log.exception("event callback failed")
            return
        if kind in ("result", "pong"):
            future = self._pending.pop(msg_id, None)  # type: ignore[arg-type]
            if future is None or future.done():
                return
            if kind == "pong" or message.get("success"):
                future.set_result(message.get("result"))
            else:
                code = (message.get("error") or {}).get("code", "unknown_error")
                future.set_exception(HACommandError(code))

    def _fail_pending(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(HANotConnected())
        self._pending.clear()

    async def call(self, message: dict[str, Any], timeout: float = 30, on_event: EventCallback | None = None) -> Any:
        """Send a command and wait for its result."""
        ws = self._ws
        if ws is None or ws.closed:
            raise HANotConnected()
        msg_id = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        if on_event is not None:
            # Registered before sending, so no event can arrive before we're listening.
            self._subscriptions[msg_id] = on_event
        try:
            await ws.send_json({**message, "id": msg_id})
            return await asyncio.wait_for(future, timeout)
        except BaseException:
            self._subscriptions.pop(msg_id, None)
            raise
        finally:
            self._pending.pop(msg_id, None)

    async def subscribe_events(self, event_type: str, callback: EventCallback) -> None:
        await self.call({"type": "subscribe_events", "event_type": event_type}, on_event=callback)

    async def ping(self, timeout: float = 10) -> bool:
        """True if Home Assistant answers a ping — i.e. it's actually processing messages."""
        try:
            await self.call({"type": "ping"}, timeout=timeout)
            return True
        except (TimeoutError, HANotConnected, HACommandError, aiohttp.ClientError, OSError):
            return False
