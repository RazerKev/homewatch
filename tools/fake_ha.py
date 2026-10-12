"""A fake Home Assistant for developing and testing the agent without a Pi.

Speaks the parts of Home Assistant's WebSocket API the agent uses (auth,
subscribe_events, get_states, config/entity_registry/list_for_display, ping) and adds a
small control API to change sensor states and to simulate Home Assistant going down.

    python tools/fake_ha.py serve                          # ws://127.0.0.1:8123, token "fake-token"
    python tools/fake_ha.py set binary_sensor.water_heater_leak on
    python tools/fake_ha.py set sensor.basement_temperature 38.5
    python tools/fake_ha.py set sensor.basement_temperature 38.5 --attr friendly_name=Basement
    python tools/fake_ha.py down                           # drop connections, refuse new ones
    python tools/fake_ha.py up
    python tools/fake_ha.py list

Needs only aiohttp (already installed with the agent).
"""

import argparse
import json
import sys
import urllib.request
import uuid
from datetime import UTC, datetime
from typing import Any

HA_VERSION = "2026.9.0"

# entity_id: (platform, initial state, attributes)
SEED: dict[str, tuple[str, str, dict[str, Any]]] = {
    "binary_sensor.water_heater_leak": ("zha", "off", {"device_class": "moisture", "friendly_name": "Water heater leak"}),
    "binary_sensor.washer_leak": ("zha", "off", {"device_class": "moisture", "friendly_name": "Washer leak"}),
    "binary_sensor.front_door": ("zha", "off", {"device_class": "door", "friendly_name": "Front door"}),
    "binary_sensor.kitchen_motion": ("zha", "off", {"device_class": "motion", "friendly_name": "Kitchen motion"}),
    "sensor.basement_temperature": ("zha", "64.2", {"device_class": "temperature", "unit_of_measurement": "°F", "friendly_name": "Basement temperature"}),
    "sensor.water_heater_leak_battery": ("zha", "100", {"device_class": "battery", "unit_of_measurement": "%", "friendly_name": "Water heater leak battery"}),
    "sensor.water_heater_leak_lqi": ("zha", "180", {"friendly_name": "Water heater leak LQI"}),
    # Phone sensors from the Companion app — these must never leave the house.
    "sensor.phone_battery_level": ("mobile_app", "84", {"device_class": "battery", "unit_of_measurement": "%", "friendly_name": "Phone battery"}),
    "sensor.phone_geocoded_location": ("mobile_app", "123 Example Street", {"friendly_name": "Phone location"}),
    "sun.sun": ("sun", "above_horizon", {"friendly_name": "Sun"}),
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _context() -> dict[str, Any]:
    return {"id": uuid.uuid4().hex, "parent_id": None, "user_id": None}


class FakeHA:
    def __init__(self, token: str) -> None:
        self.token = token
        self.up = True
        self.platforms = {entity_id: platform for entity_id, (platform, _, _) in SEED.items()}
        stamp = _now()
        self.states: dict[str, dict[str, Any]] = {
            entity_id: {
                "entity_id": entity_id,
                "state": state,
                "attributes": dict(attributes),
                "last_changed": stamp,
                "last_updated": stamp,
                "context": _context(),
            }
            for entity_id, (_, state, attributes) in SEED.items()
        }
        self.clients: dict[Any, dict[int, str | None]] = {}  # websocket -> {sub id: event_type}

    # ---- WebSocket API -----------------------------------------------------------

    async def websocket(self, request: Any) -> Any:
        from aiohttp import WSMsgType, web

        if not self.up:
            return web.Response(status=503, text="Home Assistant is down (fake)")
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required", "ha_version": HA_VERSION})
        try:
            auth = await ws.receive_json(timeout=10)
        except (TimeoutError, TypeError, ValueError):
            await ws.close()
            return ws
        if auth.get("type") != "auth" or auth.get("access_token") != self.token:
            await ws.send_json({"type": "auth_invalid", "message": "Invalid access token or password"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok", "ha_version": HA_VERSION})

        self.clients[ws] = {}
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._command(ws, json.loads(msg.data))
        finally:
            self.clients.pop(ws, None)
        return ws

    async def _command(self, ws: Any, msg: dict[str, Any]) -> None:
        msg_id, kind = msg.get("id"), msg.get("type")

        def result(value: Any) -> dict[str, Any]:
            return {"id": msg_id, "type": "result", "success": True, "result": value}

        if kind == "ping":
            await ws.send_json({"id": msg_id, "type": "pong"})
        elif kind == "subscribe_events":
            self.clients[ws][msg_id] = msg.get("event_type")
            await ws.send_json(result(None))
        elif kind == "get_states":
            await ws.send_json(result(list(self.states.values())))
        elif kind == "config/entity_registry/list_for_display":
            entities = [{"ei": entity_id, "pl": platform} for entity_id, platform in self.platforms.items()]
            await ws.send_json(result({"entity_categories": {"0": "config", "1": "diagnostic"}, "entities": entities}))
        else:
            await ws.send_json(
                {"id": msg_id, "type": "result", "success": False,
                 "error": {"code": "unknown_command", "message": "Unknown command."}}
            )

    # ---- control API ---------------------------------------------------------------

    async def set_state(self, request: Any) -> Any:
        from aiohttp import web

        body = await request.json()
        entity_id, state = body["entity_id"], str(body["state"])
        attributes = body.get("attributes")
        old = self.states.get(entity_id)
        stamp = _now()
        new_attributes = dict(old["attributes"]) if old else {}
        if attributes:
            new_attributes.update(attributes)
        if old and old["state"] == state and old["attributes"] == new_attributes:
            return web.json_response({"changed": False})
        new = {
            "entity_id": entity_id,
            "state": state,
            "attributes": new_attributes,
            # Like HA: last_changed only moves when the state itself changes.
            "last_changed": stamp if not old or old["state"] != state else old["last_changed"],
            "last_updated": stamp,
            "context": _context(),
        }
        self.states[entity_id] = new
        self.platforms.setdefault(entity_id, body.get("platform", "zha"))
        event = {
            "event_type": "state_changed",
            "data": {"entity_id": entity_id, "old_state": old, "new_state": new},
            "origin": "LOCAL",
            "time_fired": stamp,
            "context": new["context"],
        }
        for ws, subscriptions in list(self.clients.items()):
            for sub_id, event_type in subscriptions.items():
                if event_type in (None, "state_changed"):
                    await ws.send_json({"id": sub_id, "type": "event", "event": event})
        return web.json_response({"changed": True, "broadcast_to": len(self.clients)})

    async def go_down(self, request: Any) -> Any:
        from aiohttp import WSCloseCode, web

        self.up = False
        for ws in list(self.clients):
            await ws.close(code=WSCloseCode.SERVICE_RESTART, message=b"fake HA going down")
        return web.json_response({"up": False})

    async def go_up(self, request: Any) -> Any:
        from aiohttp import web

        self.up = True
        return web.json_response({"up": True})

    async def list_states(self, request: Any) -> Any:
        from aiohttp import web

        return web.json_response({"up": self.up, "states": self.states})


def serve(host: str, port: int, token: str) -> None:
    from aiohttp import web

    fake = FakeHA(token)
    app = web.Application()
    app.router.add_get("/api/websocket", fake.websocket)
    app.router.add_post("/fake/state", fake.set_state)
    app.router.add_post("/fake/down", fake.go_down)
    app.router.add_post("/fake/up", fake.go_up)
    app.router.add_get("/fake/states", fake.list_states)
    print(f"fake Home Assistant on ws://{host}:{port}/api/websocket (token: {token})", flush=True)
    web.run_app(app, host=host, port=port, print=None)


def _control(url: str, path: str, body: dict[str, Any] | None = None) -> None:
    data = json.dumps(body).encode() if body is not None else b"{}"
    method = "GET" if path == "/fake/states" else "POST"
    request = urllib.request.Request(
        url + path, data=None if method == "GET" else data, method=method,
        headers={"Content-Type": "application/json"},
    )
    # Talking to localhost: bypass any HTTP(S)_PROXY in the environment.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=10) as response:
        print(response.read().decode())


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake Home Assistant for agent development")
    parser.add_argument("--url", default="http://127.0.0.1:8123", help="control URL (for set/down/up/list)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8123)
    p.add_argument("--token", default="fake-token")
    p = sub.add_parser("set")
    p.add_argument("entity_id")
    p.add_argument("state")
    p.add_argument("--attr", action="append", default=[], metavar="KEY=VALUE")
    sub.add_parser("down")
    sub.add_parser("up")
    sub.add_parser("list")
    args = parser.parse_args()

    if args.command == "serve":
        serve(args.host, args.port, args.token)
    elif args.command == "set":
        attributes = dict(item.split("=", 1) for item in args.attr)
        _control(args.url, "/fake/state", {"entity_id": args.entity_id, "state": args.state, "attributes": attributes})
    elif args.command in ("down", "up"):
        _control(args.url, f"/fake/{args.command}")
    elif args.command == "list":
        _control(args.url, "/fake/states")
    else:
        sys.exit(2)


if __name__ == "__main__":
    main()
