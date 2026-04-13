# CARMA Box v2 — Senior Code Review Findings

**Date:** 2026-04-13
**Reviewer:** VM-900 (Opus senior review)
**Status:** 31 findings, 5 CRITICAL

---

## CRITICAL (must fix before production)

### C1: Main loop is a stub — system does nothing
- **File:** main.py:130-155
- **Impact:** No COLLECT, GUARD, DECIDE, EXECUTE, PERSIST wired up. Service spins empty.
- **Fix:** Wire all components in CarmaBoxService.__init__(), implement 6-phase pipeline in _run_cycle()

### C2: G0 Condition C incomplete — missing fast_charging OFF + limit=0
- **File:** core/guards.py:246-268
- **Impact:** INV-3 grid charging could persist after G0 trigger
- **Fix:** Emit SET_FAST_CHARGING=OFF + SET_EMS_POWER_LIMIT=0 alongside SET_EMS_MODE=standby

### C3: G3 Ellevio breach emits NO commands
- **File:** core/guards.py:360-414
- **Impact:** Ellevio breach logged but no corrective action (no load shed, no EV cut, no discharge)
- **Fix:** CRITICAL/BREACH: emit STOP_EV, TURN_OFF_CONSUMER (reverse shed), SET_EMS_MODE=discharge_pv

### C4: Emergency mode change delayed 60-90s
- **File:** core/executor.py:185-192
- **Impact:** Guard corrections (G0, G1) wait for ModeChangeManager cycle instead of executing immediately
- **Fix:** Emergency path must call inverter directly, bypassing 5-step protocol

### C5: Scenario transitions skip standby intermediate
- **File:** core/engine.py:121-124
- **Impact:** Direct charge→discharge transitions cause B1/B2 firmware hangs
- **Fix:** Route transitions through ModeChangeManager.request_change()

---

## HIGH (production reliability risk)

### H1: Balance result computed but never used (engine.py:127-149)
### H2: Hardcoded 5000W max discharge/charge (engine.py:136-137)
### H3: _effective_min_soc duplicated (guards.py vs balancer.py)
### H4: No effective_tak headroom fed to EV controller
### H5: get_states_batch fetches ALL HA entities every cycle (ha_api.py)
### H6: Audit trail grows unboundedly in memory (executor.py)
### H7: ScenarioState.dwell_s timezone fragility (models.py)
### H8: Slack creates new HTTP session per notification (slack.py)

---

## MEDIUM (code quality)

### M1: Transition matrix incomplete — no recovery from stuck states
### M2: _exit_s4 doesn't check SoC floor
### M3: Hardcoded 15.0 in _exit_s1 and _entry_s4
### M4: Nordpool price unit assumption (SEK vs öre)
### M5: SQL injection pattern in local_db.py
### M6: fix_waiting_in_fully blocks control loop 18s
### M7: Hub sync doesn't write to PostgreSQL
### M8: No health HTTP server implementation
### M9: Solcast confidence calculation inverted
### M10: is_charging determination incomplete in engine

---

## LOW (nice-to-have)

### L1: ModelEncoder doesn't handle set/frozenset
### L2: EVChargerRampConfig.steps list vs tuple mismatch
### L3: No cross-field validation for night_start/end_hour
### L4: SurplusDispatch ignores active_dependencies
### L5: EveningPlan floor can exceed 100%
### L6: ModeChangeManager._requests grows unboundedly
### L7: ExcelReportGenerator uses object instead of Workbook type
### L8: Logging hierarchy mismatch (core.guards vs carma_box)
