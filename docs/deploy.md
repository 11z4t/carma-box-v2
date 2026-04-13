# CARMA Box v2 — Deploy Guide

## Prerequisites

- Python 3.12+ on target host
- SSH access to target host (as root or sudo user)
- HA long-lived access token (Settings > Security > Long-Lived Access Tokens)
- Network access to HA at `http://192.168.5.22:8123`

## HA Token

Get from 1Password vault `HA` or create in HA:
1. HA > Profile > Long-Lived Access Tokens > Create Token
2. Save in `/etc/carma-box/env`:
   ```
   HA_TOKEN=eyJ0...
   HA_URL=http://192.168.5.22:8123
   ```

## Cutover Procedure (v6 to v2)

### Step 1: Stop v6 safely

```bash
export HA_TOKEN="your-token-here"
./scripts/stop-v6.sh
```

This will:
- Set both batteries to `battery_standby`
- Zero EMS power limits
- **Verify readback** — aborts if values don't match
- Disable v6 automations

### Step 2: Deploy v2

```bash
./scripts/deploy.sh
```

This will:
- Create `carma-box` system user
- Create `/opt/carma-box`, `/etc/carma-box`, `/var/lib/carma-box`, `/var/log/carma-box`
- Sync code, create venv, install dependencies
- Copy initial `site.yaml` (won't overwrite existing)
- Create `/etc/carma-box/env` template (won't overwrite existing)
- Install systemd unit

### Step 3: Configure

Edit `/etc/carma-box/env`:
```
HA_TOKEN=your-long-lived-token
HA_URL=http://192.168.5.22:8123
```

Edit `/etc/carma-box/site.yaml` if needed (entity IDs, thresholds).

### Step 4: Start v2

```bash
sudo systemctl start carma-box
```

### Step 5: Smoke test

```bash
# Check service status
sudo systemctl status carma-box

# Watch logs
sudo journalctl -u carma-box -f

# Verify dashboard sensors updated
curl -s -H "Authorization: Bearer $HA_TOKEN" \
  http://192.168.5.22:8123/api/states/sensor.carma_box_scenario | python3 -m json.tool
```

Expected: scenario sensor updates every 30 seconds.

## Rollback (v2 to v6)

```bash
./scripts/rollback.sh
```

This will:
- Stop carma-box v2 service
- Set batteries to `battery_standby`
- Zero EMS power limits
- Re-enable v6 automations

## Restart v2

```bash
./scripts/deploy.sh --restart
```

## File locations

| Path | Purpose |
|------|---------|
| `/opt/carma-box/` | Application code |
| `/opt/carma-box/venv/` | Python virtual environment |
| `/etc/carma-box/site.yaml` | Site configuration |
| `/etc/carma-box/env` | Environment (HA_TOKEN, HA_URL) |
| `/var/lib/carma-box/` | SQLite database |
| `/var/log/carma-box/` | Log files |
| `/etc/systemd/system/carma-box.service` | systemd unit |
