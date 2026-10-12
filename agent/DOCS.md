# HomeWatch Agent

Forwards this home's sensor changes to the HomeWatch cloud and sends a heartbeat every
minute, so the cloud can raise an alert if the home goes quiet (power cut, internet down,
hub failure) — something the home can't report about itself.

## Configuration

| Option | What to enter |
| --- | --- |
| `api_url` | The HomeWatch cloud API, e.g. `http://192.168.1.50:8000` (your PC running Docker Compose) during Phase 1. |
| `api_key` | The `hwk_...` key printed once by `python -m app.cli create-home`. Stored by Home Assistant, never in git. |
| `heartbeat_interval_s` | Seconds between heartbeats (default 60). The cloud alerts after the home's `offline_after` window (default 300 s). |
| `platforms` | Integrations whose sensors are eligible (default `zha`). Add `zwave_js` or `mqtt` if you use them. |
| `log_level` | `info` normally; `debug` when troubleshooting. |

On the Info tab, turn on **Start on boot** and **Watchdog**.

## What leaves the house

Only sensors from the listed integrations with a known sensor type (leak, temperature,
door, motion, battery, …), and only their entity ID, state, sensor type, unit, and
timestamp. Friendly names, other attributes, and anything from phones (location, Wi-Fi
name, battery) are never sent.

## When the cloud is unreachable

Events wait in `/data/outbox.db` (it survives restarts and updates) and drain
automatically when the connection returns. Events the cloud rejects as invalid are set
aside in a `dead_letter` table and counted in every heartbeat.
