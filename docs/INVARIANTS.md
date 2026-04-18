# CARMA Box v2 — Invariants

Every rule below must hold on every control cycle. Violating any of them
is a deploy blocker. The 902 watchdog monitors them independently and
files a P1 Jira ticket when one breaks.

The list is intentionally short. If you add behaviour that would bend
one of these, write a new Jira and a regression test before merging.

## Data-plane invariants (inverter safety)

| ID | Rule | Enforced in | Regression |
|----|------|-------------|------------|
| **INV-1** | `ems_mode == "auto"` is never written. GoodWe firmware makes uncontrolled decisions in auto; we use explicit modes only. | `adapters/goodwe._VALID_EMS_MODES` excludes `auto`; `core/state_machine` never emits it. | `tests/regression/test_regressions.py::test_B10_auto_mode_forbidden` |
| **INV-2** | `ems_power_limit = 0` always writes `0`. GoodWe firmware has a truthy-trap where `0` can be skipped; G0 grid-charging guard depends on the write landing. | `adapters/goodwe.set_ems_power_limit` calls the service unconditionally. | `tests/regression/test_regressions.py::test_B9_ems_power_limit_zero_truthy_trap` |
| **INV-3** | `fast_charging == off` before any `discharge_pv` command. With fast-charging on the inverter pulls from grid during discharge_pv. | `core/mode_change` 5-step protocol verifies + enforces; `core/guards.G2` catches drift. | `tests/regression/test_regressions.py::test_B7_fast_charging_before_discharge` |
| **INV-4** | PV surplus → `charge_battery` (mode 11) with an `ems_power_limit` equal to the allocation. Never `charge_pv` in peak-shaving — it ignores the limit and pulls from grid. | `core/budget` emits `CHARGE_BATTERY` + `SET_EMS_POWER_LIMIT`. | `tests/unit/test_budget.py::test_plat1714_bat_charge_uses_charge_battery_not_charge_pv` |
| **INV-5** | SoC floor respected: never dispatch discharge when cell SoC < battery minimum (15% default, adjusted cold/SoH). | `core/guards.G1` / `core/balancer`. | `tests/regression/test_regressions.py` |

## Control-plane invariants (system behaviour)

| ID | Rule | Enforced in | Regression |
|----|------|-------------|------------|
| **INV-6** | Grid Guard VETO runs first. Emergency commands are executed before the decision engine and bypass the mode-change cooldown. | `core/engine.run_cycle` Phase 1. | `tests/unit/test_engine.py::test_guard_runs_first` |
| **INV-7** | Pure decision core. `decide()` / `allocate()` never perform I/O; adapters and the executor are the only side-effect layer. | `core/budget.allocate` is a pure function; `core/executor` is the single writer. | `tests/unit/test_code_quality.py` |
| **INV-8** | Single writer. Only `CommandExecutor` invokes adapter write methods. No other module may call HA services that touch EMS mode, limit, fast-charging, EV current, or consumer relays. | Protocol + code review. | grep-based guard in `tests/unit/test_code_quality.py` |
| **INV-9** | SoC-gate parity. `StateMachineConfig.surplus_entry_soc_pct` and `BudgetConfig.bat_charge_stop_soc_pct` must match; they come from one config field (`control.battery_gate.charge_stop_soc_pct`). A drift forms a dead zone where bat keeps charging while the scenario says PV_SURPLUS. | `config/schema.BatteryGateConfig` + `main.py` wires the same value to both. | `tests/unit/test_budget.py::test_plat1695_default_stop_matches_state_machine_s8_entry` |
| **INV-10** | Ellevio weighted hourly average is kept under the effective cap (`tak_kw × weight`). Night weight = 0.5 → 2 × tak during night; day weight = 1.0 → 1 × tak. Never exceeds the cap with more than 5 % margin (G3 breach). | `core/ellevio` + `core/guards.G3`. | `tests/regression/test_regressions.py::test_B13_effective_tak_night_weight` |
| **INV-11** | Grid target ±100 W. Budget drives grid power to zero; > 50 W deviation triggers aggressive correction. | `core/budget._available_surplus_w` closed-loop term. | `tests/unit/test_budget.py::test_plat1695_grid_w_variation` |

## Site / infrastructure invariants

| ID | Rule | Enforced in | Regression |
|----|------|-------------|------------|
| **INV-12** | Mining hardware is never controlled via a Shelly relay. ASIC boards are damaged by power-cycling; control must go through the vendor REST channel. | `config/schema.ConsumerConfig.validate_miner_safety`; `adapters/goldshell.GoldshellMinerAdapter` refuses `turn_on/off/set_power` until the REST path is wired. | `tests/unit/test_config.py::TestMinerSafetyValidator` + `tests/unit/test_goldshell.py` |
| **INV-13** | No hardcoded entity IDs in code. Every entity — inverter entities, Shelly switches, PV forecast sensors, peak-avg entity, EV charger id — comes from `site.yaml`. Moving the service to another customer requires editing YAML only. | `config/schema` (all entity fields typed); no literal HA entity names in core/adapters. | grep-based guard in `tests/unit/test_code_quality.py` |
| **INV-14** | `site.yaml` is validated by `config/schema.CarmaConfig` on startup. The service refuses to start with an invalid config (Pydantic ValidationError exits non-zero). | `main.py` load_config at startup. | `tests/unit/test_config.py` |
| **INV-15** | Watchdog is read-only. `carma-box-watchdog.service` observes HA state and files alerts; it never writes to inverters, chargers, or relays. | `carma-box-watchdog/main.py` — no write paths. | review + `carma-box-watchdog/tests/` (future) |

## Verification

- Local: `python3 -m pytest tests/regression/ tests/unit/ -q`
- Live (902 watchdog): `sudo journalctl -u carma-box-watchdog -f` — look for
  `BREACHES:` lines. Status snapshot lives in
  `/mnt/solutions/Root/platform/global/agent-comms/status/902-watchdog.json`.

## When a new rule is needed

1. File a Jira with the breach description (live incident or scenario).
2. Add a regression test that fails against current `main`.
3. Add the rule to this table (ID, enforcement, regression).
4. Implement the fix, land the test green, and merge.

Do not add rules without a regression — rules without tests rot.
