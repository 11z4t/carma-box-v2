# v1 Automations Disabled (PLAT-1673 / PLAT-1674)

**Status check:** 2026-04-16 22:50 (live HA query)

This file lists all v1-era HA automations that **must remain disabled** when
v2.10.0 (PLAT-1674) is the sole writer to GoodWe inverters and Easee charger.

## Why this matters

v2.10.0's NightEVController + BatSupportController **own** the night-window
control loop. If any v1 automation re-enables and writes to:
- `select.goodwe_*_ems_mode`
- `number.goodwe_*_ems_power_limit`
- `switch.goodwe_*_fast_charging`
- `switch.easee_home_*_is_enabled`
- `easee.set_charger_dynamic_limit` (service call)
- `easee.action_command` (service call)

…then **v2-watchdog will detect drift and re-write in a loop** → Modbus stress
on GoodWe + Easee API rate-limit risk.

## Verification command

```bash
ssh hassio@192.168.5.22 'sudo curl -s -H "Authorization: Bearer $(sudo cat /run/s6/container_environment/SUPERVISOR_TOKEN)" http://supervisor/core/api/states' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); [print(s['entity_id'], s['state']) for s in d if s['entity_id'].startswith('automation.') and s['state']=='on']"
```

Expected: only observability automations ON (`energilogg_*`, `slack_*`,
`energiplan_uppdatera_kl_06_12_18_22`). All EMS/Easee writers OFF.

## Forbidden — must stay OFF

### GoodWe EMS writers

| entity_id | What it writes | Status 22:50 |
|---|---|---|
| `automation.bat_standby_charge_pv_vid_pv_overskott` | `select.goodwe_*_ems_mode` (PV-overflow → charge_pv) | off ✓ |
| `automation.goodwe_export_limit_oppna_vid_pv` | `number.goodwe_*_export_limit` | off ✓ |
| `automation.ev_100_natt_stoppa_bat_standby` | `select.goodwe_*_ems_mode` (after EV 100%) | off ✓ |

### Easee writers

| entity_id | What it writes | Status 22:50 |
|---|---|---|
| `automation.easee_auto_fix_waiting_in_fully` | `switch.easee_home_*_is_enabled` (toggle) | off ✓ |
| `automation.ml_ev_100` | `easee.set_charger_dynamic_limit` (force 0A at 100%) | off ✓ |
| `automation.ml_ev_natt_stopp` | `easee.action_command` (stop at night-end) | off ✓ |
| `automation.ev_override_auto_avslut_vid_100` | `easee.set_charger_dynamic_limit` | off ✓ |
| `automation.ev_reset_max_strom_vid_frankoppling` | `easee.set_charger_max_limit` (BAD — FLASH wear) | off ✓ |

### Peak-shaving response (v1)

| entity_id | What it writes | Status 22:50 |
|---|---|---|
| `automation.fjv_peak_shaving_response_it_270` | FJV switch (heating shed) | off ✓ |
| `automation.gv_peak_shaving_response_it_270` | Ground heat switch | off ✓ |
| `automation.radiator_peak_shaving_response_it_270` | Radiator switch | off ✓ |
| `automation.guardian_peak_shaving_heating_conflict_it_279` | Heating conflict guard | off ✓ |
| `automation.kontor_ac_morgonuppvarmning` | Office AC pre-heat | off ✓ |

## Allowed (observability only — read-only writers, no EMS)

These are SAFE to leave ON — they only update HA sensors / send Slack:

- `automation.energilogg_*` (event logging — disk, EV, bat starts/stops)
- `automation.slack_energy_*` (Slack notifications)
- `automation.ellevio_*` (Ellevio peak shaving tracking — read-only metrics)
- `automation.energiplan_uppdatera_kl_06_12_18_22` (4×daily plan refresh)
- `automation.battery_soc_snapshot` (snapshot for UI)
- `automation.carma_box_decision_logger` (legacy decision sensor — read-only)

## How to disable a re-enabled automation

If a forbidden automation gets re-enabled:

```bash
# Via HA service call
ssh hassio@192.168.5.22 'sudo curl -s \
  -H "Authorization: Bearer $(sudo cat /run/s6/container_environment/SUPERVISOR_TOKEN)" \
  -H "Content-Type: application/json" \
  http://supervisor/core/api/services/automation/turn_off \
  -d "{\"entity_id\":\"automation.<name>\"}"'
```

Or via the HA UI: Settings → Automations & Scenes → toggle off.

## Audit cadence

Run the verification command before each v2 deploy and weekly afterwards.
Any deviation MUST be investigated — either disable the automation or
explicitly add it to the "Allowed" list with rationale.
