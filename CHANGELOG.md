# Changelog

All notable changes to CARMA Box v2 will be documented in this file.

## [2.0.0] — 2026-04-14

### Added
- Complete rewrite from HA YAML automations to standalone Python service
- 8-scenario state machine with transition matrix (S1-S8)
- Safety guards G0-G7 as VETO layer (guard before decision engine)
- K/F battery balancer with proportional allocation, cold derating
- 5-step mode change protocol (PREPARE→CLEAR→STANDBY→SET→VERIFY)
- EV controller with ramp logic 6→8→10A
- Surplus dispatch engine with knapsack allocation
- Night + evening planner with price optimization
- ConsumptionProfile EMA learning (weekday/weekend)
- Savings tracker (peak reduction, price optimization, what-if)
- Monthly report data collector
- Dashboard write-back (scenario, rules, decision, plan sensors)
- Manual override via HA helpers
- Climate commands (CLIMATE_SET_TEMP, CLIMATE_SET_MODE)
- SQLite local storage with WAL mode
- PostgreSQL hub sync (dry_run default)
- Slack notifications with persistent aiohttp session
- Health endpoint with Prometheus metrics
- Excel energy plan report (xlsxwriter)
- Explicit FallbackPolicy (7 triggers, 7 actions)
- Per-cycle DecisionLog audit trail
- Deploy scripts (deploy.sh, stop-v6.sh, rollback.sh)
- 668+ tests (unit, regression B1-B15, edge cases, e2e)
- mypy --strict, ruff, pre-commit quality gate

### Security
- ems_power_limit=0 truthy-trap defense
- fast_charging OFF before discharge_pv (INV-3)
- EMS mode "auto" forbidden (B10)
- SQL table name allowlist (M5)
- No retry on 401/403 auth errors
- EnvironmentFile with chmod 600 for HA_TOKEN

### Architecture
- Pure function core: decide(state, config) → Decision
- Single writer: only CommandExecutor writes to hardware
- Grid Guard = VETO: runs first every cycle
- 30-second control loop: COLLECT→GUARD→DECIDE→EXECUTE→PERSIST
- Plugin adapters: InverterAdapter, EVChargerAdapter, LoadAdapter ABCs
- EMSMode + CTPlacement enums throughout (no raw strings)
