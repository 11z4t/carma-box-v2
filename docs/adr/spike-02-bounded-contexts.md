# SPIKE-02: Kartlägg bounded contexts

**Status:** Decided  
**Date:** 2026-04-14  
**Decision:** 4 bounded contexts identified, current module boundaries are correct

## Context

CARMA Box v2 has 15+ Python modules. This spike identifies logical boundaries (bounded contexts) to guide future refactoring.

## Identified Bounded Contexts

### 1. Energy Control (core domain)
**Modules:** engine.py, state_machine.py, guards.py, balancer.py, planner.py
**Responsibility:** Decide what to do with batteries, grid, EV each cycle.
**Invariant:** Guard ALWAYS runs before decisions. No decision bypasses guards.
**External deps:** SystemSnapshot (read-only input), Command list (output)

### 2. Hardware Adapters (integration layer)
**Modules:** adapters/goodwe.py, adapters/easee.py, adapters/ha_api.py, adapters/base.py
**Responsibility:** Translate commands to hardware API calls, read sensor states.
**Invariant:** Adapters never make decisions. They execute or read.
**External deps:** HA REST API, GoodWe Modbus (via HA), Easee Cloud (via HA)

### 3. Surplus & Consumers (sub-domain)
**Modules:** surplus_dispatch.py, ev_controller.py
**Responsibility:** Manage dispatchable loads (VP, miner, pool, EV ramp).
**Invariant:** Surplus dispatch respects grid guard limits.
**External deps:** ConsumerState list, grid headroom

### 4. Persistence & Reporting (infrastructure)
**Modules:** storage/local_db.py, storage/hub_sync.py, notifications/slack.py, reports/energy_plan.py, decision_log.py, consumption.py, savings.py
**Responsibility:** Store data, sync to hub, notify, generate reports.
**Invariant:** Persistence failures never crash the control loop.
**External deps:** SQLite, PostgreSQL, Slack webhook, xlsxwriter

## Current Module Boundaries Assessment

The current file structure maps cleanly to these 4 contexts:
- `core/` = Context 1 + 3 (Energy Control + Surplus)
- `adapters/` = Context 2 (Hardware)
- `storage/` + `notifications/` + `reports/` = Context 4 (Persistence)

**Recommendation:** Current boundaries are correct. No restructuring needed now. If engine.py grows beyond ~400 lines, extract GuardPolicy and ScenarioPolicy into separate files within core/.

## Consequences

- Future refactoring (PLAT-1376-1379) should respect these context boundaries
- Cross-context communication via well-defined interfaces (Protocol classes)
- No circular imports between contexts
