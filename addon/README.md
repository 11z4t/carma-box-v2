# CARMA Box — Home Assistant Addon

Energy optimization daemon for residential solar + battery systems.

**Version:** 2.1.0 (includes PLAT-1828 H6 stale-SoC guard fix)

## Overview

CARMA Box manages:
- **GoodWe ET** battery inverters — charge/discharge scheduling
- **Easee** EV chargers — surplus-based and night charging
- **Dispatchable loads** — Shelly-switched appliances, Goldshell miners
- **Peak shaving** — Ellevio tariff optimization

Control loop runs every 30 seconds: COLLECT → GUARD → DECIDE → EXECUTE → PERSIST.

## Requirements

- Home Assistant OS or Supervised installation
- GoodWe inverter with HA HACS integration (entity IDs in site.yaml)
- Easee EV charger (optional)
- Nordpool integration for spot-price data (optional)
- Solcast account for PV forecast (optional)

## Installation

1. Add this repository as a custom addon repository in HA Supervisor:
   `https://github.com/11z4t/carma-box-v2`

2. Install **CARMA Box** from the addon store.

3. Create your site configuration at `/homeassistant/carmabox/site.yaml`
   (see `site.yaml.example` for a full template — copied to
   `/homeassistant/carmabox/site.yaml.example` on first start).

4. Configure addon secrets in the addon options (HA UI):
   - `ha_token` — optional; Supervisor token is used automatically in addon mode
   - `solcast_api_key` — your Solcast API key
   - `slack_webhook_url` — Slack webhook for notifications (optional)

5. Start the addon.

## Configuration

### Addon options (HA UI)

| Option | Default | Description |
|--------|---------|-------------|
| `ha_token` | `` | Leave empty — Supervisor token used automatically |
| `solcast_api_key` | `` | Solcast API key (leave empty to disable) |
| `nordpool_api_key` | `` | Nordpool API key (empty = use HA entity) |
| `slack_webhook_url` | `` | Slack notifications webhook (empty = disabled) |
| `pg_host` | `` | PostgreSQL host for hub sync (empty = disabled) |
| `pg_port` | `5432` | PostgreSQL port |
| `pg_database` | `energy` | PostgreSQL database name |
| `pg_user` | `` | PostgreSQL user |
| `pg_password` | `` | PostgreSQL password |
| `log_level` | `INFO` | Log level: DEBUG / INFO / WARNING / ERROR |

### site.yaml (full configuration)

The full site configuration lives at `/homeassistant/carmabox/site.yaml`.
This file is never managed by the addon — you edit it via the HA File Editor
or SSH addon.

**Minimum required fields:**

```yaml
site:
  id: "my-site"
  name: "My Home"
  latitude: 59.33
  longitude: 18.07

homeassistant:
  url: "http://supervisor/core"   # Use this URL when running as addon

batteries:
  - id: "bat1"
    name: "GoodWe Indoor"
    cap_kwh: 13.0
    min_soc_pct: 15.0
    ct_placement: "house_grid"
    entities:
      soc: "sensor.battery_soc"
      power: "sensor.battery_power"
      ems_mode: "select.ems_mode"
      ems_power_limit: "number.ems_power_limit"
      fast_charging: "switch.fast_charging"

ev_charger:
  id: "easee1"
  name: "Easee Home"
  charger_id: "EH000000"
  entities:
    status: "sensor.easee_status"
    power: "sensor.easee_power"
    current: "sensor.easee_current"
    enabled: "switch.easee_enabled"

ev:
  id: "car1"
  name: "My EV"
  battery_kwh: 75.0
  entities:
    soc: "sensor.car_battery_level"
```

See `site.yaml.example` for all available options.

## Persistent data

| Path in container | Description |
|-------------------|-------------|
| `/data/carma.db` | SQLite state database — cycle logs, EV sessions |
| `/data/logs/carma.log` | Application log (rotated, 10 MB × 5 files) |

Both paths survive addon restarts and updates.

## Health check

The addon exposes a health endpoint on port **8412**:

```
GET http://<ha-host>:8412/health
→ {"status": "ok", "version": "2.1.0", "uptime_s": 3600}
```

## PLAT-1828 H6 stale-SoC guard

This version includes a critical safety fix: if the GoodWe SoC sensor has not
been updated for more than 120 seconds (e.g. during a UDP blackout), carma-box
treats the reading as stale (`soc = -1`) and switches to safe mode
(`battery_standby`) instead of acting on incorrect data.

Additionally, when the battery is at or near the SoC floor with PV surplus
available (> 500 W), carma-box always selects `charge_pv` rather than
`battery_standby` — preventing unnecessary grid export.

## Logs

View addon logs in HA:
- **Supervisor → CARMA Box → Log**

Or via SSH:
```bash
cat /homeassistant/carmabox/../../../data/addon_local/carmabox/logs/carma.log
```
