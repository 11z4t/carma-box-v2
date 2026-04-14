"""Tests for Safety Guards (G0-G7).

Each guard tested independently with trigger and non-trigger inputs.
Regression tests for B7, B9, B13, B15.

Test structure follows guard priority:
  G0 > G1 > G2 > G3 > G4 > G5 > G6 > G7
"""

from __future__ import annotations

import time

import pytest

from core.guards import (
    GridGuard,
    GuardConfig,
    GuardEvaluation,
    GuardLevel,
)
from core.models import BatteryState, CommandType, Scenario
from tests.conftest import make_battery_state


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def config() -> GuardConfig:
    return GuardConfig()


@pytest.fixture()
def guard(config: GuardConfig) -> GridGuard:
    return GridGuard(config)


def _eval(
    guard: GridGuard,
    batteries: list[BatteryState] | None = None,
    scenario: Scenario = Scenario.MIDDAY_CHARGE,
    weighted_avg_kw: float = 0.5,
    hour: int = 12,
    ha_connected: bool = True,
    data_age_s: float = 0.0,
    stale_entities: list[str] | None = None,
) -> GuardEvaluation:
    """Helper to call guard.evaluate with defaults."""
    return guard.evaluate(
        batteries=batteries or [make_battery_state()],
        current_scenario=scenario,
        weighted_avg_kw=weighted_avg_kw,
        hour=hour,
        ha_connected=ha_connected,
        data_age_s=data_age_s,
        stale_entities=stale_entities,
    )


# ===========================================================================
# G0: Grid Charging Detection
# ===========================================================================


class TestG0GridCharging:
    """G0 tests: detect unintentional grid charging."""

    def test_ems_power_limit_in_charge_pv_triggers(
        self, guard: GridGuard
    ) -> None:
        """B9: ems_power_limit > 0 in charge_pv = grid charging."""
        bat = make_battery_state(
            ems_mode="charge_pv", ems_power_limit_w=3000
        )
        result = _eval(guard, batteries=[bat])
        assert result.level == GuardLevel.CRITICAL
        assert any(c.guard_id == "G0" for c in result.commands)
        # Must command limit to 0
        g0_cmds = [c for c in result.commands if c.guard_id == "G0"]
        limit_cmd = [
            c for c in g0_cmds
            if c.command_type == CommandType.SET_EMS_POWER_LIMIT
        ]
        assert len(limit_cmd) >= 1
        assert limit_cmd[0].value == 0

    def test_normal_charge_pv_no_trigger(self, guard: GridGuard) -> None:
        """charge_pv with limit=0 is safe — no trigger."""
        bat = make_battery_state(
            ems_mode="charge_pv", ems_power_limit_w=0
        )
        result = _eval(guard, batteries=[bat])
        g0_cmds = [c for c in result.commands if c.guard_id == "G0"]
        assert len(g0_cmds) == 0

    def test_night_grid_charge_scenario_exempt(
        self, guard: GridGuard
    ) -> None:
        """During NIGHT_GRID_CHARGE, grid charging is intentional."""
        bat = make_battery_state(
            ems_mode="charge_pv", ems_power_limit_w=3000
        )
        result = _eval(
            guard, batteries=[bat],
            scenario=Scenario.NIGHT_GRID_CHARGE,
        )
        g0_cmds = [c for c in result.commands if c.guard_id == "G0"]
        assert len(g0_cmds) == 0

    def test_charging_at_soc_floor_triggers(
        self, guard: GridGuard
    ) -> None:
        """Autonomous grid charging at SoC floor."""
        bat = make_battery_state(
            soc_pct=15.5, ems_power_limit_w=1000
        )
        result = _eval(guard, batteries=[bat])
        g0_cmds = [c for c in result.commands if c.guard_id == "G0"]
        assert len(g0_cmds) >= 1

    def test_night_charging_without_pv_triggers(
        self, guard: GridGuard
    ) -> None:
        """Condition C: charging from grid at night with no PV."""
        bat = make_battery_state(
            power_w=-500,  # Charging
            ems_mode="battery_standby",
            pv_power_w=0,
        )
        result = _eval(guard, batteries=[bat], hour=2)
        g0_cmds = [c for c in result.commands if c.guard_id == "G0"]
        assert len(g0_cmds) >= 1
        # Should command standby (not just limit=0)
        mode_cmds = [
            c for c in g0_cmds
            if c.command_type == CommandType.SET_EMS_MODE
        ]
        assert len(mode_cmds) >= 1

    def test_pv_charging_does_not_trigger(self, guard: GridGuard) -> None:
        """Charging with PV production should NOT trigger G0 condition C."""
        bat = make_battery_state(
            power_w=-2000,  # Charging
            ems_mode="charge_pv",
            ems_power_limit_w=0,
            pv_power_w=3000,
        )
        result = _eval(guard, batteries=[bat])
        g0_cmds = [c for c in result.commands if c.guard_id == "G0"]
        assert len(g0_cmds) == 0

    def test_multiple_batteries_checked(self, guard: GridGuard) -> None:
        """Both batteries should be checked independently."""
        bat_ok = make_battery_state(
            battery_id="kontor", ems_mode="charge_pv", ems_power_limit_w=0
        )
        bat_bad = make_battery_state(
            battery_id="forrad", ems_mode="charge_pv", ems_power_limit_w=1500
        )
        result = _eval(guard, batteries=[bat_ok, bat_bad])
        g0_cmds = [c for c in result.commands if c.guard_id == "G0"]
        assert len(g0_cmds) >= 1
        assert all(c.target_id == "forrad" for c in g0_cmds)


# ===========================================================================
# G1: SoC Floor
# ===========================================================================


class TestG1SocFloor:
    """G1 tests: SoC floor enforcement with hysteresis."""

    def test_below_floor_triggers_standby(self, guard: GridGuard) -> None:
        bat = make_battery_state(soc_pct=14.0)
        result = _eval(guard, batteries=[bat])
        g1_cmds = [c for c in result.commands if c.guard_id == "G1"]
        assert len(g1_cmds) == 1
        assert g1_cmds[0].value == "battery_standby"

    def test_at_floor_triggers(self, guard: GridGuard) -> None:
        bat = make_battery_state(soc_pct=15.0)
        result = _eval(guard, batteries=[bat])
        g1_cmds = [c for c in result.commands if c.guard_id == "G1"]
        assert len(g1_cmds) == 1

    def test_above_floor_no_trigger(self, guard: GridGuard) -> None:
        bat = make_battery_state(soc_pct=50.0)
        result = _eval(guard, batteries=[bat])
        g1_cmds = [c for c in result.commands if c.guard_id == "G1"]
        assert len(g1_cmds) == 0

    def test_hysteresis_prevents_immediate_resume(
        self, guard: GridGuard
    ) -> None:
        """After hitting floor, must rise above floor+5% to resume."""
        bat_low = make_battery_state(soc_pct=14.0)
        _eval(guard, batteries=[bat_low])  # Trigger floor

        # SoC rises to 18% — still in hysteresis zone (< 15% + 5% = 20%)
        bat_mid = make_battery_state(soc_pct=18.0)
        result = _eval(guard, batteries=[bat_mid])
        g1_cmds = [c for c in result.commands if c.guard_id == "G1"]
        assert len(g1_cmds) == 1  # Still held at standby

    def test_hysteresis_clears_above_threshold(
        self, guard: GridGuard
    ) -> None:
        """After floor+5%, battery can resume."""
        bat_low = make_battery_state(soc_pct=14.0)
        _eval(guard, batteries=[bat_low])  # Trigger floor

        # SoC rises to 21% — above 15% + 5% = 20%
        bat_high = make_battery_state(soc_pct=21.0)
        result = _eval(guard, batteries=[bat_high])
        g1_cmds = [c for c in result.commands if c.guard_id == "G1"]
        assert len(g1_cmds) == 0  # Released

    def test_cold_raises_floor(self, guard: GridGuard) -> None:
        """Cold battery uses higher floor (20%)."""
        bat = make_battery_state(soc_pct=18.0, cell_temp_c=2.0)
        result = _eval(guard, batteries=[bat])
        g1_cmds = [c for c in result.commands if c.guard_id == "G1"]
        assert len(g1_cmds) == 1  # 18% < 20% cold floor

    def test_freeze_raises_floor_more(self, guard: GridGuard) -> None:
        """Freezing battery uses 25% floor."""
        bat = make_battery_state(soc_pct=23.0, cell_temp_c=-3.0)
        result = _eval(guard, batteries=[bat])
        g1_cmds = [c for c in result.commands if c.guard_id == "G1"]
        assert len(g1_cmds) == 1  # 23% < 25% freeze floor

    def test_low_soh_raises_floor(self, guard: GridGuard) -> None:
        """SoH < 80% adds +5% to floor."""
        bat = make_battery_state(soc_pct=18.0, soh_pct=75.0)
        result = _eval(guard, batteries=[bat])
        g1_cmds = [c for c in result.commands if c.guard_id == "G1"]
        assert len(g1_cmds) == 1  # 18% < 15% + 5% = 20%

    def test_very_low_soh_raises_floor_more(self, guard: GridGuard) -> None:
        """SoH < 70% adds +10% to floor."""
        bat = make_battery_state(soc_pct=23.0, soh_pct=65.0)
        result = _eval(guard, batteries=[bat])
        g1_cmds = [c for c in result.commands if c.guard_id == "G1"]
        assert len(g1_cmds) == 1  # 23% < 15% + 10% = 25%

    def test_one_battery_at_floor_other_free(
        self, guard: GridGuard
    ) -> None:
        """G1 is per-battery: one at floor shouldn't stop the other."""
        bat_low = make_battery_state(battery_id="kontor", soc_pct=14.0)
        bat_ok = make_battery_state(battery_id="forrad", soc_pct=60.0)
        result = _eval(guard, batteries=[bat_low, bat_ok])
        g1_cmds = [c for c in result.commands if c.guard_id == "G1"]
        assert len(g1_cmds) == 1
        assert g1_cmds[0].target_id == "kontor"


# ===========================================================================
# G2: INV-3 fast_charging Conflict
# ===========================================================================


class TestG2Inv3:
    """G2 tests: fast_charging + discharge_pv = firmware bug."""

    def test_fast_charging_plus_discharge_triggers(
        self, guard: GridGuard
    ) -> None:
        """B7 regression: fast_charging ON + discharge_pv must trigger."""
        bat = make_battery_state(
            fast_charging=True, ems_mode="discharge_pv"
        )
        result = _eval(guard, batteries=[bat])
        g2_cmds = [c for c in result.commands if c.guard_id == "G2"]
        assert len(g2_cmds) == 1
        assert g2_cmds[0].command_type == CommandType.SET_FAST_CHARGING
        assert g2_cmds[0].value is False

    def test_fast_charging_without_discharge_ok(
        self, guard: GridGuard
    ) -> None:
        bat = make_battery_state(
            fast_charging=True, ems_mode="charge_pv"
        )
        result = _eval(guard, batteries=[bat])
        g2_cmds = [c for c in result.commands if c.guard_id == "G2"]
        assert len(g2_cmds) == 0

    def test_discharge_without_fast_charging_ok(
        self, guard: GridGuard
    ) -> None:
        bat = make_battery_state(
            fast_charging=False, ems_mode="discharge_pv"
        )
        result = _eval(guard, batteries=[bat])
        g2_cmds = [c for c in result.commands if c.guard_id == "G2"]
        assert len(g2_cmds) == 0


# ===========================================================================
# G3: Ellevio Breach
# ===========================================================================


class TestG3Ellevio:
    """G3 tests: Ellevio weighted average check with effective tak."""

    def test_below_margin_no_trigger(self, guard: GridGuard) -> None:
        """Below 85% of tak (3.0*0.85=2.55) = OK."""
        result = _eval(guard, weighted_avg_kw=2.0, hour=12)
        assert result.level == GuardLevel.OK

    def test_warning_at_margin(self, guard: GridGuard) -> None:
        """Above 85% of 3.0kW = above 2.55kW → WARNING."""
        result = _eval(guard, weighted_avg_kw=2.7, hour=12)
        assert result.level == GuardLevel.WARNING

    def test_critical_at_emergency(self, guard: GridGuard) -> None:
        """Above 110% of 3.0kW = above 3.3kW → CRITICAL."""
        result = _eval(guard, weighted_avg_kw=3.5, hour=12)
        assert result.level == GuardLevel.CRITICAL

    def test_breach_above_tak(self, guard: GridGuard) -> None:
        """Between 3.0kW and 3.3kW → BREACH (above tak but below critical)."""
        result = _eval(guard, weighted_avg_kw=3.1, hour=12)
        assert result.level == GuardLevel.BREACH
        assert result.replan_needed

    def test_far_above_tak_is_critical(self, guard: GridGuard) -> None:
        """Above 3.3kW (110% of tak) → CRITICAL (highest severity)."""
        result = _eval(guard, weighted_avg_kw=3.5, hour=12)
        assert result.level == GuardLevel.CRITICAL
        assert result.replan_needed

    def test_effective_tak_night_is_6kw(self, guard: GridGuard) -> None:
        """B13 regression: night tak = 3.0 / 0.5 = 6.0 kW."""
        # 5.0 kW at night should be fine (< 6.0 kW)
        result = _eval(guard, weighted_avg_kw=5.0, hour=23)
        g3_violations = [v for v in result.violations if "G3" in v]
        assert len(g3_violations) == 0

    def test_effective_tak_day_is_3kw(self, guard: GridGuard) -> None:
        """Day tak = 3.0 / 1.0 = 3.0 kW. 3.1 kW exceeds tak → BREACH."""
        result = _eval(guard, weighted_avg_kw=3.1, hour=14)
        assert result.level == GuardLevel.BREACH

    def test_night_5_2kw_triggers_warning(self, guard: GridGuard) -> None:
        """Night: 5.2 kW > 6.0 * 0.85 = 5.1 kW → WARNING."""
        result = _eval(guard, weighted_avg_kw=5.2, hour=1)
        g3_violations = [v for v in result.violations if "G3" in v]
        assert len(g3_violations) >= 1

    def test_effective_tak_boundaries(self, guard: GridGuard) -> None:
        """Test all hour boundaries for night/day transition."""
        # Night hours: 22, 23, 0, 1, 2, 3, 4, 5
        for h in [22, 23, 0, 1, 2, 3, 4, 5]:
            assert guard._effective_tak_kw(h) == 6.0, f"Hour {h} should be night (6.0kW)"
        # Day hours: 6-21
        for h in range(6, 22):
            assert guard._effective_tak_kw(h) == 3.0, f"Hour {h} should be day (3.0kW)"


# ===========================================================================
# G4: Temperature Guard
# ===========================================================================


class TestG4Temperature:
    """G4 tests: cold weather protection."""

    def test_freeze_blocks_discharge(self, guard: GridGuard) -> None:
        bat = make_battery_state(cell_temp_c=-5.0)
        result = _eval(guard, batteries=[bat])
        g4_cmds = [c for c in result.commands if c.guard_id == "G4"]
        assert len(g4_cmds) == 1
        assert g4_cmds[0].value == "battery_standby"

    def test_warm_no_trigger(self, guard: GridGuard) -> None:
        bat = make_battery_state(cell_temp_c=20.0)
        result = _eval(guard, batteries=[bat])
        g4_cmds = [c for c in result.commands if c.guard_id == "G4"]
        assert len(g4_cmds) == 0


# ===========================================================================
# G5: Oscillation Detection
# ===========================================================================


class TestG5Oscillation:
    """G5 tests: rapid mode change detection."""

    def test_no_changes_no_trigger(self, guard: GridGuard) -> None:
        result = _eval(guard)
        g5_violations = [v for v in result.violations if "G5" in v]
        assert len(g5_violations) == 0

    def test_three_changes_triggers(self, guard: GridGuard) -> None:
        """3 changes in 5 min should trigger."""
        guard.record_mode_change()
        guard.record_mode_change()
        guard.record_mode_change()
        result = _eval(guard)
        g5_violations = [v for v in result.violations if "G5" in v]
        assert len(g5_violations) == 1

    def test_deadband_doubled_after_trigger(self, guard: GridGuard) -> None:
        guard.record_mode_change()
        guard.record_mode_change()
        guard.record_mode_change()
        _eval(guard)
        assert guard.is_deadband_doubled


# ===========================================================================
# G6: Stale Data
# ===========================================================================


class TestG6StaleData:
    """G6 tests: stale data → FREEZE (NOT standby)."""

    def test_fresh_data_no_trigger(self, guard: GridGuard) -> None:
        result = _eval(guard, data_age_s=10.0)
        g6_violations = [v for v in result.violations if "G6" in v]
        assert len(g6_violations) == 0

    def test_stale_data_triggers_freeze(self, guard: GridGuard) -> None:
        """Must FREEZE, not set standby (B15)."""
        result = _eval(guard, data_age_s=600.0)
        assert result.level == GuardLevel.FREEZE
        # Must NOT command standby — freeze means keep current state
        g6_mode_cmds = [
            c for c in result.commands
            if c.guard_id == "G6" and c.command_type == CommandType.SET_EMS_MODE
        ]
        assert len(g6_mode_cmds) == 0


# ===========================================================================
# G7: Communication Lost
# ===========================================================================


class TestG7CommLost:
    """G7 tests: HA connection loss → FREEZE."""

    def test_connected_no_trigger(self, guard: GridGuard) -> None:
        result = _eval(guard, ha_connected=True)
        g7_violations = [v for v in result.violations if "G7" in v]
        assert len(g7_violations) == 0

    def test_disconnected_beyond_timeout_triggers(
        self, guard: GridGuard
    ) -> None:
        """After ha_health_timeout_s, G7 fires."""
        # Simulate: last contact was long ago
        guard._last_ha_contact = time.monotonic() - 60
        result = _eval(guard, ha_connected=False)
        assert result.level == GuardLevel.FREEZE

    def test_brief_disconnect_no_trigger(self, guard: GridGuard) -> None:
        """Brief disconnect within timeout should not trigger."""
        # Just disconnected (within 30s timeout)
        guard._last_ha_contact = time.monotonic() - 5
        result = _eval(guard, ha_connected=False)
        g7_violations = [v for v in result.violations if "G7" in v]
        assert len(g7_violations) == 0


# ===========================================================================
# Guard Priority / Integration
# ===========================================================================


class TestGuardPriority:
    """Test that multiple guards can fire together."""

    def test_g0_and_g1_both_fire(self, guard: GridGuard) -> None:
        """G0 + G1 on same battery."""
        bat = make_battery_state(
            soc_pct=14.0,
            ems_mode="charge_pv",
            ems_power_limit_w=1000,
        )
        result = _eval(guard, batteries=[bat])
        guard_ids = {c.guard_id for c in result.commands}
        assert "G0" in guard_ids
        assert "G1" in guard_ids

    def test_headroom_calculated(self, guard: GridGuard) -> None:
        """Headroom = effective_tak - weighted_avg."""
        result = _eval(guard, weighted_avg_kw=2.0, hour=12)
        assert result.headroom_kw == pytest.approx(1.0, abs=0.01)  # 3.0 - 2.0

    def test_headroom_night(self, guard: GridGuard) -> None:
        result = _eval(guard, weighted_avg_kw=3.0, hour=23)
        assert result.headroom_kw == pytest.approx(3.0, abs=0.01)  # 6.0 - 3.0


# ===========================================================================
# Coverage: uncovered branches
# ===========================================================================


class TestCoverageBranches:
    """Tests targeting specific uncovered branches."""

    def test_old_mode_changes_purged_from_deque(self, guard: GridGuard) -> None:
        """Entries older than window_s are purged from deque (line 452)."""
        # Inject an old timestamp (600s ago — outside default window_s=300)
        guard._mode_changes.appendleft(time.monotonic() - 600)
        assert len(guard._mode_changes) == 1
        # Evaluate — purge loop should remove the old entry
        _eval(guard)
        assert len(guard._mode_changes) == 0

    def test_is_night_non_wrapping_window(self) -> None:
        """_is_night with non-wrapping window (start < end) covers line 536."""
        cfg = GuardConfig(night_start_hour=8, night_end_hour=20)
        g = GridGuard(cfg)
        assert g._is_night(10) is True   # inside window
        assert g._is_night(7) is False   # before start
        assert g._is_night(20) is False  # at end (exclusive)


# ===========================================================================
# PLAT-1357: G0 config-based thresholds, G3 BREACH emits STOP_EV
# ===========================================================================


class TestPlat1357G0ConfigThresholds:
    """PLAT-1357: G0 detection thresholds come from config, not hardcoded."""

    def test_g0_uses_custom_charging_power_threshold(self) -> None:
        """G0 Condition C respects g0_charging_power_threshold_w from config."""
        # Use a custom threshold: only trigger if power < -500W (not default -100W)
        cfg = GuardConfig(g0_charging_power_threshold_w=-500.0, g0_min_pv_power_w=50.0)
        guard = GridGuard(cfg)

        # Battery at -200W (would trigger with default -100W, but not with -500W)
        bat = make_battery_state(
            power_w=-200.0,         # Charging at 200W
            ems_mode="battery_standby",  # Not charge_pv
            pv_power_w=0.0,         # No PV
        )
        result = _eval(guard, batteries=[bat])
        g0_cmds = [c for c in result.commands if c.guard_id == "G0"]
        # Should NOT trigger G0 because -200 > -500 (above threshold)
        condition_c_cmds = [
            c for c in g0_cmds
            if c.command_type == CommandType.SET_EMS_MODE
        ]
        assert len(condition_c_cmds) == 0

    def test_g0_uses_custom_pv_threshold(self) -> None:
        """G0 Condition C respects g0_min_pv_power_w from config."""
        # High PV threshold: 200W of PV required before considering it 'PV charging'
        cfg = GuardConfig(g0_charging_power_threshold_w=-100.0, g0_min_pv_power_w=200.0)
        guard = GridGuard(cfg)

        # Battery charging at -200W with 100W PV (below 200W threshold → G0 triggers)
        bat = make_battery_state(
            power_w=-200.0,
            ems_mode="battery_standby",
            pv_power_w=100.0,  # 100W PV, but threshold is 200W
        )
        result = _eval(guard, batteries=[bat])
        g0_cmds = [c for c in result.commands if c.guard_id == "G0"]
        condition_c = [c for c in g0_cmds if c.command_type == CommandType.SET_EMS_MODE]
        assert len(condition_c) >= 1

    def test_g0_config_fields_exist(self) -> None:
        """GuardConfig must have g0 threshold fields."""
        cfg = GuardConfig()
        assert hasattr(cfg, "g0_charging_power_threshold_w")
        assert hasattr(cfg, "g0_min_pv_power_w")
        assert cfg.g0_charging_power_threshold_w == -100.0
        assert cfg.g0_min_pv_power_w == 50.0


class TestPlat1357G3BreachEmitsStopEv:
    """PLAT-1357: G3 BREACH level must emit STOP_EV_CHARGING command."""

    def test_g3_breach_emits_stop_ev(self, guard: GridGuard) -> None:
        """G3 BREACH (> tak but < emergency) must include STOP_EV_CHARGING."""
        # Default tak=3.0 kW, day_weight=1.0 → effective_tak=3.0 kW
        # BREACH when weighted_avg > 3.0 (but < 3.0*1.10=3.3 = CRITICAL)
        result = _eval(guard, weighted_avg_kw=3.1, hour=12)

        assert result.level == GuardLevel.BREACH
        stop_ev_cmds = [
            c for c in result.commands
            if c.command_type == CommandType.STOP_EV_CHARGING
        ]
        assert len(stop_ev_cmds) >= 1, "BREACH must emit STOP_EV_CHARGING"

    def test_g3_breach_also_cuts_ev_current(self, guard: GridGuard) -> None:
        """G3 BREACH emits both STOP_EV_CHARGING and SET_EV_CURRENT at 6A."""
        result = _eval(guard, weighted_avg_kw=3.1, hour=12)

        assert result.level == GuardLevel.BREACH
        set_current_cmds = [
            c for c in result.commands
            if c.command_type == CommandType.SET_EV_CURRENT
        ]
        assert len(set_current_cmds) >= 1
        assert set_current_cmds[0].value == 6

    def test_g3_breach_emits_turn_off_consumer(self, guard: GridGuard) -> None:
        """N3: G3 BREACH must shed all consumers."""
        result = _eval(guard, weighted_avg_kw=3.1, hour=12)
        off_cmds = [
            c for c in result.commands
            if c.command_type == CommandType.TURN_OFF_CONSUMER
        ]
        assert len(off_cmds) >= 1
        assert off_cmds[0].target_id == "all"

    def test_g3_critical_still_emits_stop_ev(self, guard: GridGuard) -> None:
        """G3 CRITICAL also emits STOP_EV_CHARGING (existing behaviour)."""
        # CRITICAL when weighted_avg > tak * 1.10 = 3.3 kW
        result = _eval(guard, weighted_avg_kw=3.5, hour=12)

        assert result.level == GuardLevel.CRITICAL
        stop_ev_cmds = [
            c for c in result.commands
            if c.command_type == CommandType.STOP_EV_CHARGING
        ]
        assert len(stop_ev_cmds) >= 1

    def test_g3_critical_emits_turn_off_consumer(self, guard: GridGuard) -> None:
        """N3: G3 CRITICAL must shed all consumers."""
        result = _eval(guard, weighted_avg_kw=3.5, hour=12)
        off_cmds = [
            c for c in result.commands
            if c.command_type == CommandType.TURN_OFF_CONSUMER
        ]
        assert len(off_cmds) >= 1
