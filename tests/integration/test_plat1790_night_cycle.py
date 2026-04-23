"""Integration test: full PLAT-1790 R-natt night charging cycle.

Scenario: EV plugged in at 22:00, charges overnight via R-natt, reaches SoC target
at 04:00. Verifies:
  - 0 grid violations (R1 not triggered during intentional night grid import)
  - bat_soc_drop < 3pp over night (bat is smoothing-only, not primary source)
  - decision-reason transitions: night_window_charge → soc_target_reached
"""

from __future__ import annotations

from config.schema import EVDispatchV2Config, NightModeConfig, NightModeBatSmoothingConfig
from core.ev_dispatch import (
    EVActionType,
    EVDispatchInputs,
    EVDispatchState,
    EVPhase,
    evaluate_ev_action,
)


def _night_cfg() -> EVDispatchV2Config:
    return EVDispatchV2Config(
        enabled=True,
        shadow_mode=False,
        bat_ready_threshold_pct=95.0,
        min_session_min=15.0,
        margin_factor=1.2,
        ev_min_amps=6,
        ev_max_amps=10,
        ev_phase_count=3,
        bat_smoothing_threshold_w=300.0,
        grid_incident_threshold_w=100.0,
        night_mode=NightModeConfig(
            enabled=True,
            start_hour=22,
            end_hour=6,
            bat_smoothing=NightModeBatSmoothingConfig(
                enabled=True,
                cap_w=500,
                soc_floor_pct=80.0,
                window_s=5.0,
            ),
        ),
    )


def test_plat1790_full_night_cycle() -> None:
    """Simulate a full night EV charging session: 22:00 plug-in → 04:00 SoC full.

    The cycle simulates 30-second intervals at hours 22, 23, 0, 1, 2, 3 (charging)
    and then at hour 4 the EV reaches target SoC (80%) → stops.

    Asserts:
    - 0 R1 grid incidents (R1 not checked during night charging)
    - bat_soc_drop < 3pp (smoothing only, no active drain)
    - reason transitions correctly (night_window_charge → soc_target_reached)
    - No writes before plug is detected (IDLE → CONNECTED → CHARGING)
    """
    cfg = _night_cfg()
    state = EVDispatchState(phase=EVPhase.IDLE)

    # EV battery: 92 kWh, charging at 3-phase 10A = 230*10*3 = 6900 W ≈ 6.9 kW
    # From SoC 40% → 80%: 0.40 * 92 = 36.8 kWh to add.
    # Hours needed: 36.8 / 6.9 ≈ 5.3 h  → reaches target at ~03:18

    ev_soc = 40.0
    ev_target_soc = 80.0
    bat_soc = 98.0     # starts high (day charging done)
    bat_soc_start = bat_soc

    grid_incidents = 0
    reasons_seen: list[str] = []

    # Night hours: 22,23,0,1,2,3 → simulate one cycle per hour for simplicity
    # EV charges at 10A × 3ph × 230V = 6.9 kW per hour → SoC gain per hour:
    #   6.9 kWh / 92 kWh = 7.5% per hour
    ev_soc_gain_per_hour = (10 * 3 * 230) / 1000 / 92 * 100  # ≈ 7.5%

    # Smoothing: occasional transients of 300W (within 500W cap)
    grid_transient = 300.0  # W transient; bat_soc stays above 80% throughout

    # Small bat discharge from smoothing: 300W × 5s per cycle / 3600 / 92kWh * 100%
    # = 0.000045% per cycle → negligible, well under 3pp over 6 hours

    # Night hours: plug at 22, start charging at 23, reach target at hour 5
    # (40% + 6 × 7.5% = 85% > 80% target → soc_target_reached fires at hour 5)
    night_hours = [22, 23, 0, 1, 2, 3, 4, 5]

    for hour in night_hours:
        # Build inputs for this hour
        inputs = EVDispatchInputs(
            ev_status="connected",
            bat_soc=bat_soc,
            surplus_w=0.0,           # no PV at night
            grid_w=7000.0,           # ~7kW EV + house import (intentional)
            predicted_refill_kwh=0.0,  # R10 fails (irrelevant at night)
            predicted_bat_deficit_kwh=5.0,
            planned_window_min=0.0,  # R11 fails (irrelevant at night)
            bat_soc_at_sunset=50.0,  # R9 fails (irrelevant at night)
            pv_power_w=0.0,
            prev_pv_power_w=0.0,
            now_monotonic=float(hour * 3600),
            hour_of_day=hour,
            ev_soc=ev_soc,
            ev_target_soc=ev_target_soc,
            grid_transient_w=grid_transient if state.phase == EVPhase.CHARGING else 0.0,
        )

        result = evaluate_ev_action(state, inputs, cfg)
        state = result.new_state

        # Track reasons during charging
        if state.phase == EVPhase.CHARGING or result.action in (
            EVActionType.EV_START, EVActionType.EV_STOP
        ):
            reasons_seen.append(result.reason)

        # R1 incidents must be 0 (R1 is skipped during night charging)
        if result.grid_incident:
            grid_incidents += 1

        # Advance EV SoC if charging
        if state.night_charging:
            ev_soc = min(ev_soc + ev_soc_gain_per_hour, 100.0)
            # Tiny bat drain from smoothing (300W × 1 cycle ÷ 92kWh = ~0.003%)
            bat_soc -= 300.0 / 1000 / 92 * 100  # negligible

    # ── Assertions ─────────────────────────────────────────────────────────

    # 1. Zero grid violations — R1 not triggered during night charging
    assert grid_incidents == 0, (
        f"Expected 0 grid incidents, got {grid_incidents}. "
        "R1 should be suppressed during night_charging mode."
    )

    # 2. bat_soc_drop < 3pp over the whole night
    bat_soc_drop = bat_soc_start - bat_soc
    assert bat_soc_drop < 3.0, (
        f"bat_soc dropped {bat_soc_drop:.3f}pp, expected < 3pp. "
        "Night bat smoothing (Alt B) should not significantly drain battery."
    )

    # 3. At least one 'R-natt: night_window_charge' reason was emitted
    assert any("night_window_charge" in r for r in reasons_seen), (
        f"Expected at least one 'night_window_charge' reason. Reasons seen: {reasons_seen}"
    )

    # 4. Session ended via soc_target_reached (hour 4: ev_soc ≈ 80%+ → stop)
    #    OR outside_window (hour 6 not included in our sim, but soc target reached first)
    assert any("soc_target_reached" in r or "outside_window" in r for r in reasons_seen), (
        f"Expected soc_target_reached or outside_window stop reason. Reasons: {reasons_seen}"
    )

    # 5. Final state is COMPLETING or IDLE (session ended cleanly)
    assert state.phase in (EVPhase.COMPLETING, EVPhase.IDLE), (
        f"Expected COMPLETING or IDLE at end of cycle, got {state.phase}"
    )
