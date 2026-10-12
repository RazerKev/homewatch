"""Agent configuration.

Two ways to run:

* As a Home Assistant app (formerly "add-on") — production. Options come from
  /data/options.json, which the app's Configuration tab writes. Home Assistant is reached
  through the Supervisor proxy with the SUPERVISOR_TOKEN it injects.
* Standalone — development on a laptop, against a real HA over the network or the fake
  HA in tools/. Settings come from environment variables, optionally loaded from a .env
  file next to where you run it (git ignores .env).

Secrets (the cloud API key, the HA token) never live in the repo.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

APP_OPTIONS = Path("/data/options.json")

# Only these device classes are ever forwarded. Anything else (signal strength, link
# quality, firmware, identify buttons...) stays in the house.
DEFAULT_DEVICE_CLASSES = frozenset(
    {
        # water, freeze, and safety
        "moisture", "cold", "heat", "smoke", "carbon_monoxide", "gas", "safety", "problem",
        # entry and activity (aging-in-place awareness)
        "door", "window", "garage_door", "opening", "motion", "occupancy", "presence",
        "tamper", "vibration",
        # measurements
        "temperature", "humidity", "illuminance", "power", "energy", "current", "voltage",
        "water", "volume_flow_rate",
        # upkeep
        "battery",
    }
)


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class AgentConfig:
    api_url: str
    api_key: str = field(repr=False)
    ha_ws_url: str
    ha_token: str = field(repr=False)
    mode: str  # "app" or "standalone"
    heartbeat_interval_s: float = 60.0
    # Only entities created by these integrations are eligible: the home's own sensor
    # network. Phone sensors from the Companion app (platform "mobile_app": location,
    # Wi-Fi name, battery...) never qualify.
    platforms: frozenset[str] = frozenset({"zha"})
    device_classes: frozenset[str] = DEFAULT_DEVICE_CLASSES
    batch_size: int = 100
    outbox_path: str = "homewatch_outbox.db"
    max_outbox_rows: int = 100_000
    log_level: str = "INFO"


def load_config() -> AgentConfig:
    if APP_OPTIONS.exists() and os.environ.get("SUPERVISOR_TOKEN"):
        return _from_app_options(json.loads(APP_OPTIONS.read_text()))
    _load_dotenv(Path(os.environ.get("HOMEWATCH_ENV_FILE", ".env")))
    return _from_environment()


def _from_app_options(opts: dict) -> AgentConfig:
    return _validated(
        AgentConfig(
            api_url=str(opts.get("api_url", "")).rstrip("/"),
            api_key=str(opts.get("api_key", "")).strip(),
            ha_ws_url="ws://supervisor/core/websocket",
            ha_token=os.environ["SUPERVISOR_TOKEN"],
            mode="app",
            heartbeat_interval_s=float(opts.get("heartbeat_interval_s", 60)),
            platforms=frozenset(opts.get("platforms") or ["zha"]),
            outbox_path="/data/outbox.db",  # /data persists across restarts and updates
            log_level=str(opts.get("log_level", "info")).upper(),
        )
    )


def _from_environment() -> AgentConfig:
    def required(name: str) -> str:
        value = os.environ.get(name, "").strip()
        if not value:
            raise ConfigError(f"{name} is not set (see agent/.env.example)")
        return value

    return _validated(
        AgentConfig(
            api_url=required("HOMEWATCH_API_URL").rstrip("/"),
            api_key=required("HOMEWATCH_API_KEY"),
            ha_ws_url=websocket_url(required("HA_URL")),
            ha_token=required("HA_TOKEN"),
            mode="standalone",
            heartbeat_interval_s=float(os.environ.get("HOMEWATCH_HEARTBEAT_S", "60")),
            platforms=frozenset(_csv(os.environ.get("HOMEWATCH_PLATFORMS", "zha"))),
            outbox_path=os.environ.get("HOMEWATCH_OUTBOX", "homewatch_outbox.db"),
            log_level=os.environ.get("HOMEWATCH_LOG_LEVEL", "INFO").upper(),
        )
    )


def _validated(cfg: AgentConfig) -> AgentConfig:
    if not cfg.api_url.startswith(("http://", "https://")):
        raise ConfigError("api_url must start with http:// or https://")
    if not cfg.api_key.startswith("hwk_"):
        raise ConfigError("api_key doesn't look like a HomeWatch key (hwk_...)")
    if not 2 <= cfg.heartbeat_interval_s <= 3600:
        raise ConfigError("heartbeat interval must be between 2 and 3600 seconds")
    return cfg


def websocket_url(ha_url: str) -> str:
    """http://homeassistant.local:8123 -> ws://homeassistant.local:8123/api/websocket"""
    base = ha_url.rstrip("/")
    for http, ws in (("https://", "wss://"), ("http://", "ws://")):
        if base.startswith(http):
            return ws + base[len(http):] + "/api/websocket"
    raise ConfigError("HA_URL must start with http:// or https://")


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _load_dotenv(path: Path) -> None:
    """Minimal KEY=VALUE reader; real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))
