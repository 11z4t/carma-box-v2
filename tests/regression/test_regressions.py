"""Regression Test Suite (B1-B15).

Each test reproduces the exact failure condition of a known bug
and verifies the fix is in place. These tests MUST NEVER fail.

Based on spec Section 1.5 known bugs.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from adapters.goodwe import GoodWeAdapter
from core.balancer import BatteryBalancer, BatteryInfo
from core.ev_controller import EVAction, EVController, EVControllerConfig
from core.guards import GridGuard, GuardConfig, GuardLevel
from core.mode_change import ModeChangeConfig, ModeChangeManager
from core.models import CommandType, Scenario
from tests.conftest import make_battery_state


# B1: Mode change through 5-min standby
class TestB1FirmwareLatencyStandby:
    """B1: Mode change must go through battery_standby intermediate."""

    @pytest.mark.asyncio
    async def test_standby_intermediate_exists(self) -> None:
        mgr = ModeChangeManager(ModeChangeConfig(
            clear_wait_s=0, standby_wait_s=0, set_wait_s=0, verify_wait_s=0,
        ))
        executor = AsyncMock()
        executor.set_ems_mode = AsyncMock(return_value=True)
        executor.set_ems_power_limit = AsyncMock(return_value=True)
        executor.set_fast_charging = AsyncMock(return_value=True)
        executor.get_ems_mode = AsyncMock(return_value="discharge_pv")
        executor.get_fast_charging = AsyncMock(return_value=False)

        mgr.request_change("kontor", "discharge_pv")
        # Run through to STANDBY
        await mgr.process(executor)  # IDLE → CLEARING
        await mgr.process(executor)  # CLEARING → STANDBY_WAIT

        calls = executor.set_ems_mode.call_args_list
        standby_calls = [c for c in calls if c[0] == ("kontor", "battery_standby")]
        assert len(standby_calls) >= 1, "B1: battery_standby MUST be intermediate"


# B3: Easee waiting_in_fully auto-fix
class TestB3WaitingInFully:
    """B3: waiting_in_fully triggers 3-step fix sequence."""

    def test_ev_controller_detects_waiting_in_fully(self) -> None:
        ctrl = EVController(EVControllerConfig(
            step_interval_s=0, cooldown_after_start_s=0, cooldown_after_stop_s=0,
        ))
        result = ctrl.evaluate(
            ev_connected=True, ev_soc_pct=50.0, charging=False,
            current_amps=0, grid_import_w=0, ellevio_headroom_w=5000,
            reason_for_no_current="waiting_in_fully",
        )
        assert result.action == EVAction.FIX_WAITING_IN_FULLY


# B5: K/F SoC divergence balanced
class TestB5KFDivergence:
    """B5: Balancer corrects diverged K/F SoC."""

    def test_diverged_soc_correction(self) -> None:
        balancer = BatteryBalancer()
        k = BatteryInfo(battery_id="kontor", soc_pct=80.0, cap_kwh=15.0,
                        cell_temp_c=20.0, soh_pct=100.0, max_discharge_w=5000.0,
                        max_charge_w=5000.0, ct_placement="local_load")
        f = BatteryInfo(battery_id="forrad", soc_pct=40.0, cap_kwh=5.0,
                        cell_temp_c=20.0, soh_pct=100.0, max_discharge_w=5000.0,
                        max_charge_w=5000.0, ct_placement="house_grid")
        result = balancer.allocate([k, f], 4000.0)
        alloc = result.allocation_map
        assert alloc["kontor"].correction_factor > 1.0, "B5: higher SoC should discharge more"


# B6: EV never jumps above max, always starts at 6A
class TestB6EVMaxAndStart:
    """B6: EV never jumps to 16A, always starts at 6A."""

    def test_start_at_6a(self) -> None:
        ctrl = EVController(EVControllerConfig(
            step_interval_s=0, cooldown_after_start_s=0, cooldown_after_stop_s=0,
        ))
        result = ctrl.evaluate(
            ev_connected=True, ev_soc_pct=50.0, charging=False,
            current_amps=0, grid_import_w=0, ellevio_headroom_w=5000,
        )
        assert result.action == EVAction.START
        assert result.target_amps == 6, "B6: ALWAYS start at 6A"

    def test_never_above_max(self) -> None:
        ctrl = EVController(EVControllerConfig(
            step_interval_s=0, cooldown_after_start_s=0, cooldown_after_stop_s=0,
        ))
        result = ctrl.evaluate(
            ev_connected=True, ev_soc_pct=50.0, charging=True,
            current_amps=10, grid_import_w=0, ellevio_headroom_w=10000,
        )
        if result.action == EVAction.SET_CURRENT:
            assert result.target_amps <= 10, "B6: never above max_amps"


# B7: fast_charging OFF before discharge_pv (INV-3)
class TestB7Inv3FastCharging:
    """B7: fast_charging must be OFF before any discharge_pv."""

    def test_goodwe_set_ems_mode_does_not_touch_fast_charging(self) -> None:
        mock_api = AsyncMock()
        mock_api.call_service = AsyncMock(return_value=True)
        mock_api.get_state = AsyncMock(return_value="off")
        from config.schema import BatteryConfig, BatteryEntities
        config = BatteryConfig(
            id="test", name="Test", cap_kwh=10.0, ct_placement="house_grid",
            entities=BatteryEntities(
                soc="s.soc", power="s.power",
                ems_mode="select.mode", ems_power_limit="number.limit",
                fast_charging="switch.fc",
            ),
        )
        import asyncio
        adapter = GoodWeAdapter(mock_api, config)
        asyncio.get_event_loop().run_until_complete(adapter.set_ems_mode("discharge_pv"))
        for call in mock_api.call_service.call_args_list:
            assert call[0][0] != "switch", "B7: set_ems_mode must NOT call switch services"


# B8: 15% SoC floor enforced
class TestB8SocFloor:
    """B8: Absolute SoC floor of 15% enforced by G1."""

    def test_floor_enforced(self) -> None:
        guard = GridGuard(GuardConfig())
        bat = make_battery_state(soc_pct=14.0)
        result = guard.evaluate(
            batteries=[bat], current_scenario=Scenario.MIDDAY_CHARGE,
            weighted_avg_kw=1.0, hour=12, ha_connected=True,
        )
        g1 = [c for c in result.commands if c.guard_id == "G1"]
        assert len(g1) >= 1, "B8: G1 must trigger at SoC < 15%"


# B9: ems_power_limit=0 in charge_pv causes grid charging
class TestB9EmsLimitChargePv:
    """B9: ems_power_limit > 0 in charge_pv detected by G0."""

    def test_grid_charging_detected(self) -> None:
        guard = GridGuard(GuardConfig())
        bat = make_battery_state(ems_mode="charge_pv", ems_power_limit_w=3000)
        result = guard.evaluate(
            batteries=[bat], current_scenario=Scenario.MIDDAY_CHARGE,
            weighted_avg_kw=1.0, hour=12, ha_connected=True,
        )
        assert result.level == GuardLevel.CRITICAL, "B9: G0 must fire"


# B10: auto mode forbidden
class TestB10AutoForbidden:
    """B10: EMS mode 'auto' is never used."""

    def test_auto_rejected(self) -> None:
        mock_api = AsyncMock()
        mock_api.call_service = AsyncMock(return_value=True)
        from config.schema import BatteryConfig, BatteryEntities
        config = BatteryConfig(
            id="test", name="Test", cap_kwh=10.0, ct_placement="house_grid",
            entities=BatteryEntities(
                soc="s.soc", power="s.power",
                ems_mode="select.mode", ems_power_limit="number.limit",
                fast_charging="switch.fc",
            ),
        )
        import asyncio
        adapter = GoodWeAdapter(mock_api, config)
        result = asyncio.get_event_loop().run_until_complete(adapter.set_ems_mode("auto"))
        assert result is False, "B10: auto mode must be rejected"


# B13: effective_tak, not raw tak
class TestB13EffectiveTak:
    """B13: Night uses effective_tak (6.0 kW), not raw tak (3.0 kW)."""

    def test_night_tak_is_6kw(self) -> None:
        guard = GridGuard(GuardConfig())
        assert guard._effective_tak_kw(23) == 6.0, "B13: night tak = 3.0/0.5 = 6.0 kW"
        assert guard._effective_tak_kw(12) == 3.0, "B13: day tak = 3.0/1.0 = 3.0 kW"


# B14: All discharge paths use discharge_pv, never auto
class TestB14DischargePathsOnly:
    """B14: All discharge uses discharge_pv mode."""

    def test_auto_not_in_valid_modes(self) -> None:
        from adapters.goodwe import _VALID_EMS_MODES
        assert "auto" not in _VALID_EMS_MODES, "B14: auto must not be in valid modes"
        assert "discharge_pv" in _VALID_EMS_MODES


# B15: ems_power_limit cleared on mode transition
class TestB15LimitClearedOnTransition:
    """B15: ems_power_limit zeroed in mode change step 2 (CLEARING)."""

    @pytest.mark.asyncio
    async def test_limit_zeroed_in_clearing(self) -> None:
        mgr = ModeChangeManager(ModeChangeConfig(
            clear_wait_s=0, standby_wait_s=0, set_wait_s=0, verify_wait_s=0,
        ))
        executor = AsyncMock()
        executor.set_ems_mode = AsyncMock(return_value=True)
        executor.set_ems_power_limit = AsyncMock(return_value=True)
        executor.set_fast_charging = AsyncMock(return_value=True)

        mgr.request_change("kontor", "discharge_pv")
        await mgr.process(executor)  # IDLE → CLEARING

        executor.set_ems_power_limit.assert_awaited_with("kontor", 0)


# B2: No direct charge→discharge (must go through standby)
class TestB2StandbyBetweenChargeDischarge:
    """B2: No direct charge→discharge. Must go through standby intermediate."""

    @pytest.mark.asyncio
    async def test_charge_to_discharge_goes_through_standby(self) -> None:
        from core.mode_change import ModeChangeConfig, ModeChangeManager

        mgr = ModeChangeManager(ModeChangeConfig(
            clear_wait_s=0, standby_wait_s=0, set_wait_s=0, verify_wait_s=0,
        ))
        executor = AsyncMock()
        executor.set_ems_mode = AsyncMock(return_value=True)
        executor.set_ems_power_limit = AsyncMock(return_value=True)
        executor.set_fast_charging = AsyncMock(return_value=True)
        executor.get_ems_mode = AsyncMock(return_value="discharge_pv")
        executor.get_fast_charging = AsyncMock(return_value=False)

        mgr.request_change("kontor", "discharge_pv")
        # Process through all steps
        for _ in range(6):
            await mgr.process(executor)

        calls = executor.set_ems_mode.call_args_list
        modes = [c[0][1] for c in calls]
        # Must include battery_standby before discharge_pv
        assert "battery_standby" in modes, "B2: standby must appear before discharge"
        standby_idx = modes.index("battery_standby")
        discharge_idx = modes.index("discharge_pv")
        assert standby_idx < discharge_idx, "B2: standby must come BEFORE discharge"


# B4: Easee charger_id, NOT device_id
class TestB4ChargerIdNotDeviceId:
    """B4: All Easee service calls use charger_id, never device_id."""

    @pytest.mark.asyncio
    async def test_set_current_uses_charger_id(self) -> None:
        from adapters.easee import EaseeAdapter
        from adapters.ha_api import HAApiClient
        from config.schema import EVChargerConfig, EVChargerEntities

        mock_api = AsyncMock(spec=HAApiClient)
        mock_api.call_service = AsyncMock(return_value=True)
        config = EVChargerConfig(
            id="ev", name="Easee", charger_id="EH128405",
            entities=EVChargerEntities(
                status="s.status", power="s.power",
                current="s.current", enabled="sw.enabled",
            ),
        )
        adapter = EaseeAdapter(mock_api, config)
        await adapter.set_current(8)
        call_data = mock_api.call_service.call_args[0][2]
        assert "charger_id" in call_data, "B4: must use charger_id"
        assert call_data["charger_id"] == "EH128405"
        assert "device_id" not in call_data, "B4: must NOT use device_id"


# B11: Night EV not killed by export watchdog during discharge ramp-up
class TestB11NightEvNotKilledByWatchdog:
    """B11: During night EV charging, transient discharge ramp-up
    should NOT trigger export watchdog to kill EV."""

    def test_g3_does_not_stop_ev_at_warning_level(self) -> None:
        """G3 WARNING should NOT stop EV — only CRITICAL/BREACH."""
        guard = GridGuard(GuardConfig())
        bat = make_battery_state(soc_pct=60.0)
        # Weighted avg just above warning but below breach
        result = guard.evaluate(
            batteries=[bat],
            current_scenario=Scenario.NIGHT_HIGH_PV,
            weighted_avg_kw=2.7,  # > 3.0*0.85=2.55 (WARNING) but < 3.0 (BREACH)
            hour=23,
            ha_connected=True,
        )
        # WARNING should NOT emit STOP_EV commands
        stop_ev = [
            c for c in result.commands
            if c.command_type == CommandType.STOP_EV_CHARGING
        ]
        assert len(stop_ev) == 0, "B11: WARNING must NOT stop EV"


# B12: No ensure_initialized(force=True) during active charging
class TestB12NoForceInitDuringCharging:
    """B12: The system must not reset/reinitialize components
    while EV is actively charging — could cause current spike."""

    def test_ev_controller_no_reset_while_charging(self) -> None:
        """EVController should not reset state while charging is active."""
        from core.ev_controller import EVAction, EVController, EVControllerConfig

        ctrl = EVController(EVControllerConfig(
            step_interval_s=0,
            cooldown_after_start_s=0,
            cooldown_after_stop_s=0,
        ))
        # Simulate active charging at 8A
        result = ctrl.evaluate(
            ev_connected=True, ev_soc_pct=50.0, charging=True,
            current_amps=8, grid_import_w=2000,
            ellevio_headroom_w=1000,
        )
        # Should NOT produce START action (which would reset current to 6A)
        assert result.action != EVAction.START, (
            "B12: must not START (reset to 6A) while already charging"
        )
