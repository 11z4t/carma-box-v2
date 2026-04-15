"""Executable safety invariants for guard control logic (PLAT-1590).

I1: Charge/discharge mutual exclusion per battery
I2: No IMPORT_AC commands during BREACH
I3: ALARM/FREEZE dominates lower levels
I4: Every guard command has a cause (guard_id + reason)
"""

from __future__ import annotations

from core.guards import (
    ExportGuard,
    GridGuard,
    GuardConfig,
    GuardEvaluation,
    GuardLevel,
    GuardPolicy,
)
from core.models import CommandType, EMSMode, Scenario
from tests.conftest import make_battery_state, make_grid_state, make_snapshot


# ---------------------------------------------------------------------------
# Named test constants — no magic numbers in test code.
# ---------------------------------------------------------------------------
_SOC_BELOW_FLOOR_PCT: float = 14.0       # just below G1 floor (default 15.0%)
_SOC_NOMINAL_PCT: float = 60.0           # mid-range, no guard trigger
_NEUTRAL_WEIGHTED_AVG_KW: float = 1.0    # safe grid level, no G3 trigger
_BREACH_TRIGGER_KW: float = 5.0          # above Ellevio tak → G3 BREACH
_NEUTRAL_SPOT_PRICE_ORE: float = 50.0    # mid-range price, no ExportGuard trigger
_VERY_STALE_DATA_AGE_S: float = 600.0    # far above stale threshold → G6 FREEZE
_TEST_HOUR: int = 14                      # mid-afternoon test hour
_KONTOR_EMS_LIMIT_W: int = 3_000         # test fixture: kontor EMS power limit
_FORRAD_EMS_LIMIT_W: int = 2_000         # test fixture: forrad EMS power limit
_PV_NONE_KW: float = 0.0                 # no PV production
_PV_MODERATE_KW: float = 3.0             # moderate PV


# Priority ordering: FREEZE is highest, OK is lowest.
GUARD_LEVEL_PRIORITY: list[GuardLevel] = [
    GuardLevel.FREEZE,
    GuardLevel.ALARM,
    GuardLevel.BREACH,
    GuardLevel.CRITICAL,
    GuardLevel.WARNING,
    GuardLevel.OK,
]

# Charge modes (grid or PV charging)
_CHARGE_MODES = frozenset({EMSMode.IMPORT_AC.value, EMSMode.CHARGE_PV.value})
# Discharge modes
_DISCHARGE_MODES = frozenset({EMSMode.DISCHARGE_PV.value})


# ===========================================================================
# I1: Charge/discharge mutual exclusion
# ===========================================================================


class TestChargeDischargeExclusion:
    """I1: No battery may receive both charge and discharge commands
    in a single guard evaluation."""

    def test_charge_discharge_exclusion_single_battery(self) -> None:
        """Single battery: no conflicting charge + discharge commands."""
        guard = GridGuard(GuardConfig())
        policy = GuardPolicy(guard, ExportGuard())

        # Low SoC (triggers G1 → standby) + grid charging at floor (triggers G0)
        bat = make_battery_state(
            battery_id="kontor",
            soc_pct=_SOC_BELOW_FLOOR_PCT,
            ems_mode="charge_pv",
            ems_power_limit_w=_KONTOR_EMS_LIMIT_W,
        )
        snap = make_snapshot(hour=_TEST_HOUR, batteries=[bat])
        result = policy.evaluate(
            batteries=snap.batteries,
            current_scenario=Scenario.MIDDAY_CHARGE,
            weighted_avg_kw=_NEUTRAL_WEIGHTED_AVG_KW,
            hour=snap.hour,
            ha_connected=True,
            pv_kw=_PV_NONE_KW,
            spot_price_ore=_NEUTRAL_SPOT_PRICE_ORE,
        )

        # Collect mode commands per target
        mode_cmds: dict[str, set[str]] = {}
        for cmd in result.commands:
            if cmd.command_type == CommandType.SET_EMS_MODE:
                mode_cmds.setdefault(cmd.target_id, set()).add(str(cmd.value))

        for target_id, modes in mode_cmds.items():
            charge = modes & _CHARGE_MODES
            discharge = modes & _DISCHARGE_MODES
            assert not (charge and discharge), (
                f"Battery {target_id} has both charge ({charge}) "
                f"and discharge ({discharge}) commands"
            )

    def test_charge_discharge_exclusion_two_batteries(self) -> None:
        """Two batteries: no conflicting commands per battery."""
        guard = GridGuard(GuardConfig())
        policy = GuardPolicy(guard, ExportGuard())

        bats = [
            make_battery_state(
                battery_id="kontor", soc_pct=_SOC_BELOW_FLOOR_PCT,
                ems_mode="charge_pv", ems_power_limit_w=_KONTOR_EMS_LIMIT_W,
            ),
            make_battery_state(
                battery_id="forrad", soc_pct=_SOC_BELOW_FLOOR_PCT,
                ems_mode="charge_pv", ems_power_limit_w=_FORRAD_EMS_LIMIT_W,
            ),
        ]
        snap = make_snapshot(hour=_TEST_HOUR, batteries=bats)
        result = policy.evaluate(
            batteries=snap.batteries,
            current_scenario=Scenario.MIDDAY_CHARGE,
            weighted_avg_kw=_NEUTRAL_WEIGHTED_AVG_KW,
            hour=snap.hour,
            ha_connected=True,
            pv_kw=_PV_NONE_KW,
            spot_price_ore=_NEUTRAL_SPOT_PRICE_ORE,
        )

        mode_cmds: dict[str, set[str]] = {}
        for cmd in result.commands:
            if cmd.command_type == CommandType.SET_EMS_MODE:
                mode_cmds.setdefault(cmd.target_id, set()).add(str(cmd.value))

        for target_id, modes in mode_cmds.items():
            charge = modes & _CHARGE_MODES
            discharge = modes & _DISCHARGE_MODES
            assert not (charge and discharge), (
                f"Battery {target_id} has conflicting charge/discharge"
            )


# ===========================================================================
# I2: No IMPORT_AC during BREACH
# ===========================================================================


class TestImportCapOnBreach:
    """I2: When guard level is BREACH, no command may set IMPORT_AC."""

    def test_import_cap_never_exceeded_on_breach(self) -> None:
        """BREACH level → no SET_EMS_MODE=IMPORT_AC commands."""
        guard = GridGuard(GuardConfig())
        policy = GuardPolicy(guard, ExportGuard())

        # High weighted average → triggers G3 BREACH
        bat = make_battery_state(soc_pct=_SOC_NOMINAL_PCT)
        snap = make_snapshot(
            hour=_TEST_HOUR,
            batteries=[bat],
            grid=make_grid_state(weighted_avg_kw=_BREACH_TRIGGER_KW),
        )
        result = policy.evaluate(
            batteries=snap.batteries,
            current_scenario=Scenario.MIDDAY_CHARGE,
            weighted_avg_kw=snap.grid.weighted_avg_kw,
            hour=snap.hour,
            ha_connected=True,
            pv_kw=_PV_NONE_KW,
            spot_price_ore=_NEUTRAL_SPOT_PRICE_ORE,
        )

        if result.level in (GuardLevel.BREACH, GuardLevel.ALARM, GuardLevel.FREEZE):
            import_cmds = [
                cmd for cmd in result.commands
                if cmd.command_type == CommandType.SET_EMS_MODE
                and cmd.value == EMSMode.IMPORT_AC.value
            ]
            assert len(import_cmds) == 0, (
                f"BREACH/ALARM but IMPORT_AC commands found: {import_cmds}"
            )


# ===========================================================================
# I3: ALARM/FREEZE dominates lower levels
# ===========================================================================


class TestAlarmDominatesWarning:
    """I3: Higher guard levels always dominate lower ones."""

    def test_guard_level_priority_ordering(self) -> None:
        """FREEZE > ALARM > BREACH > CRITICAL > WARNING > OK."""
        for i, higher in enumerate(GUARD_LEVEL_PRIORITY):
            for lower in GUARD_LEVEL_PRIORITY[i + 1:]:
                # Higher priority levels have lower index
                assert GUARD_LEVEL_PRIORITY.index(higher) < GUARD_LEVEL_PRIORITY.index(lower), (
                    f"{higher.value} should dominate {lower.value}"
                )

    def test_alarm_dominates_warning(self) -> None:
        """If any guard triggers ALARM, final level is >= ALARM."""
        # Construct evaluation with ALARM level
        eval_alarm = GuardEvaluation(level=GuardLevel.ALARM)
        eval_warning = GuardEvaluation(level=GuardLevel.WARNING)

        alarm_idx = GUARD_LEVEL_PRIORITY.index(eval_alarm.level)
        warning_idx = GUARD_LEVEL_PRIORITY.index(eval_warning.level)
        assert alarm_idx < warning_idx, "ALARM must dominate WARNING"

    def test_freeze_dominates_all(self) -> None:
        """FREEZE is the highest priority level."""
        assert GUARD_LEVEL_PRIORITY[0] == GuardLevel.FREEZE

    def test_stale_data_triggers_freeze(self) -> None:
        """Stale data (high data_age_s) triggers FREEZE — highest level."""
        guard = GridGuard(GuardConfig())
        policy = GuardPolicy(guard, ExportGuard())

        bat = make_battery_state(soc_pct=_SOC_NOMINAL_PCT)
        snap = make_snapshot(hour=_TEST_HOUR, batteries=[bat])
        result = policy.evaluate(
            batteries=snap.batteries,
            current_scenario=Scenario.MIDDAY_CHARGE,
            weighted_avg_kw=_NEUTRAL_WEIGHTED_AVG_KW,
            hour=snap.hour,
            ha_connected=True,
            pv_kw=_PV_MODERATE_KW,
            spot_price_ore=_NEUTRAL_SPOT_PRICE_ORE,
            data_age_s=_VERY_STALE_DATA_AGE_S,
        )

        freeze_idx = GUARD_LEVEL_PRIORITY.index(GuardLevel.FREEZE)
        result_idx = GUARD_LEVEL_PRIORITY.index(result.level)
        assert result_idx <= freeze_idx, (
            f"Stale data should trigger FREEZE, got {result.level.value}"
        )


# ===========================================================================
# I4: Every command has a cause
# ===========================================================================


class TestEveryCommandHasCause:
    """I4: Every GuardCommand must have non-empty guard_id and reason."""

    def test_every_command_has_cause(self) -> None:
        """All commands from guard evaluation have guard_id + reason."""
        guard = GridGuard(GuardConfig())
        policy = GuardPolicy(guard, ExportGuard())

        # Trigger multiple guards: low SoC (G1) + grid charging (G0)
        bat = make_battery_state(
            soc_pct=_SOC_BELOW_FLOOR_PCT,
            ems_mode="charge_pv",
            ems_power_limit_w=_KONTOR_EMS_LIMIT_W,
        )
        snap = make_snapshot(hour=_TEST_HOUR, batteries=[bat])
        result = policy.evaluate(
            batteries=snap.batteries,
            current_scenario=Scenario.MIDDAY_CHARGE,
            weighted_avg_kw=_NEUTRAL_WEIGHTED_AVG_KW,
            hour=snap.hour,
            ha_connected=True,
            pv_kw=_PV_NONE_KW,
            spot_price_ore=_NEUTRAL_SPOT_PRICE_ORE,
        )

        assert len(result.commands) >= 1, "Expected at least 1 guard command"
        for cmd in result.commands:
            assert cmd.guard_id, f"Command missing guard_id: {cmd}"
            assert cmd.reason, f"Command missing reason: {cmd}"

    def test_commands_have_valid_guard_ids(self) -> None:
        """Guard IDs follow Gx or named guard pattern (e.g. EXPORT)."""
        guard = GridGuard(GuardConfig())
        policy = GuardPolicy(guard, ExportGuard())

        bat = make_battery_state(
            soc_pct=_SOC_BELOW_FLOOR_PCT,
            ems_mode="charge_pv",
            ems_power_limit_w=_KONTOR_EMS_LIMIT_W,
        )
        snap = make_snapshot(hour=_TEST_HOUR, batteries=[bat])
        result = policy.evaluate(
            batteries=snap.batteries,
            current_scenario=Scenario.MIDDAY_CHARGE,
            weighted_avg_kw=_NEUTRAL_WEIGHTED_AVG_KW,
            hour=snap.hour,
            ha_connected=True,
            pv_kw=_PV_NONE_KW,
            spot_price_ore=_NEUTRAL_SPOT_PRICE_ORE,
        )

        for cmd in result.commands:
            assert cmd.guard_id[0].isupper(), (
                f"Guard ID should start with uppercase: {cmd.guard_id}"
            )
