# CARMA Box v2.0 — Smart Energy Optimization Platform

Standalone Python service managing solar PV, battery inverters (GoodWe ET), EV chargers (Easee), and dispatchable loads for residential sites. Replaces ~6,800 lines of HA YAML automations.

## Quick Start

```bash
# Install
./scripts/deploy.sh

# Configure
sudo vi /etc/carma-box/env       # Set HA_TOKEN
sudo vi /etc/carma-box/site.yaml  # Verify entity IDs

# Start
sudo systemctl start carma-box

# Monitor
sudo journalctl -u carma-box -f
curl http://localhost:8412/health
```

## Architecture

```
30s cycle: COLLECT → GUARD → SCENARIO → BALANCE → EXECUTE → PERSIST → SURPLUS → DASHBOARD
```

- **Guards G0-G7**: Safety VETO layer (always runs first)
- **State Machine**: 8 scenarios (S1-S8) with transition matrix
- **Balancer**: K/F battery proportional allocation
- **Surplus Dispatch**: Knapsack allocation to consumers

## Key Files

| File | Purpose |
|------|---------|
| `main.py` | Entry point, control loop |
| `core/engine.py` | 6-phase pipeline |
| `core/guards.py` | Safety guards G0-G7 |
| `core/state_machine.py` | Scenario transitions |
| `core/balancer.py` | K/F battery allocation |
| `config/site.yaml` | All configuration |
| `scripts/deploy.sh` | Deploy to production |
| `scripts/stop-v6.sh` | Safe v6 shutdown |
| `scripts/rollback.sh` | Rollback to v6 |

## Testing

```bash
python3 -m pytest tests/ -v          # 668+ tests
python3 -m mypy --strict core/ main.py  # Type checking
python3 -m ruff check .               # Linting
```

## Deploy / Cutover

See [docs/deploy.md](docs/deploy.md) for full procedure.

## Rollback

```bash
./scripts/rollback.sh
```

Stops v2, sets batteries to standby, re-enables v6 automations.

## Debug

```bash
# Health endpoint
curl http://localhost:8412/health | python3 -m json.tool

# Prometheus metrics
curl http://localhost:8412/metrics

# Last 100 log lines
sudo journalctl -u carma-box -n 100

# Config validation
python3 main.py --config /etc/carma-box/site.yaml --dry-run
```
