# CLAUDE.md — CARMA Box v2.0

## Project Overview

CARMA Box is a standalone Python 3.12+ energy optimization service that manages
solar PV, battery inverters (GoodWe ET), EV chargers (Easee), and dispatchable
loads for residential sites. It replaces ~6,800 lines of HA YAML automations.

## Architecture

- **Pure function core:** `decide(state, config, plan) -> Decision` — no side effects
- **Single writer:** Only CommandExecutor writes to hardware via adapters
- **Grid Guard = VETO:** Runs first every cycle, constraints are absolute
- **30-second control loop:** COLLECT -> GUARD -> DECIDE -> EXECUTE -> PERSIST
- **Plugin adapters:** InverterAdapter, EVChargerAdapter, LoadAdapter ABCs

## Project Structure

```
carma-box-v2/
  config/           # site.yaml + Pydantic schema
  core/             # Domain models, decision engine, guards
  adapters/         # Hardware adapter interfaces + implementations
  storage/          # SQLite local + PostgreSQL hub sync
  tests/            # pytest suite (unit, integration, regression, e2e)
  main.py           # Entry point with argparse + asyncio loop
  carma-box.service # systemd unit file
```

## Critical Rules

1. **NEVER use EMS mode `auto`** — GoodWe firmware makes uncontrolled decisions (B10)
2. **ALWAYS set `fast_charging=OFF` before `discharge_pv`** — INV-3 / B7
3. **`ems_power_limit=0` must actually write 0** — not be skipped by truthy-trap (B9)
4. **Ellevio weighted target = 2.0 kW** — NEVER exceed, use `effective_tak_kw` with night weight (B13)
5. **ALL discharge paths use `discharge_pv`** — NEVER `auto` (B14)
6. **SoC absolute minimum = 15%** — GoodWe cuts AC output below this (B8)
7. **Easee max_charger_current >= 10A** — below 6A causes `waiting_in_fully` block (B3)
8. **CT placement awareness:** Kontor=local_load, Forrad=house_grid — different control strategies

## Commands

```bash
# Run tests
python3 -m pytest tests/ -v

# Run with mypy
mypy --strict config/ core/ adapters/ storage/ main.py

# Load config validation
python3 -c "from config.schema import load_config; c = load_config('config/site.yaml'); print(f'Site: {c.site.name}')"

# Start service
python3 main.py --config config/site.yaml

# Dry run (validate config only)
python3 main.py --config config/site.yaml --dry-run
```

## Design Spec

Full specification: `/mnt/solutions/Root/solutions/HA-Malmgren/solution/docs/energy-automation/CARMA-BOX-DESIGN-SPEC-v2.md`

## Dependencies

- pydantic >= 2.6 (config validation)
- aiohttp >= 3.9 (HA REST API client)
- pyyaml >= 6.0 (config loading)
- aiosqlite >= 0.19 (local storage)
- asyncpg >= 0.29 (PostgreSQL hub sync)
- structlog >= 24.1 (structured logging)
- prometheus-client >= 0.20 (metrics)

## Testing

- Unit tests: `tests/unit/` — pure function tests, no I/O
- Integration tests: `tests/integration/` — mock HA server
- Regression tests: `tests/regression/` — one test per known bug (B1-B15)
- E2E tests: `tests/e2e/` — full cycle with mock adapters
