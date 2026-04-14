"""Tests for PLAT-1560: GuardPolicy — composed guard pipeline."""

from __future__ import annotations

from core.guards import (
    EXPORT_MIN_PV_KW,
    ExportGuard,
    GridGuard,
    GuardConfig,
    GuardLevel,
    GuardPolicy,
)
from core.models import CommandType, Scenario
from tests.conftest import make_battery_state

# ---------------------------------------------------------------------------
# Constants — no magic numbers in tests
# ---------------------------------------------------------------------------

# PV safely above export minimum — ExportGuard must stay silent
_SAFE_PV_KW: float = EXPORT_MIN_PV_KW + 1.0

# PV below export minimum — ExportGuard must trigger
_LOW_PV_KW: float = EXPORT_MIN_PV_KW - 0.1

# Positive spot price — no price-based export limit
_POSITIVE_PRICE_ORE: float = 50.0

# Negative spot price — ExportGuard must trigger
_NEGATIVE_PRICE_ORE: float = -5.0

# Typical safe grid import (kW) — well below Ellevio tak
_SAFE_GRID_KW: float = 1.0

# Hour that is daytime (no night weight)
_DAY_HOUR: int = 12


def _make_policy() -> GuardPolicy:
    """Return a GuardPolicy with default config."""
    return GuardPolicy(
        grid_guard=GridGuard(GuardConfig()),
        export_guard=ExportGuard(),
    )


# ===========================================================================
# Basic composition — both guards run every cycle
# ===========================================================================


class TestGuardPolicyComposition:
    """GuardPolicy composes GridGuard and ExportGuard into one evaluation."""

    def test_returns_guard_evaluation(self) -> None:
        """evaluate() must return a GuardEvaluation object."""
        from core.guards import GuardEvaluation

        policy = _make_policy()
        batteries = [make_battery_state()]
        result = policy.evaluate(
            batteries=batteries,
            current_scenario=Scenario.NIGHT_LOW_PV,
            weighted_avg_kw=_SAFE_GRID_KW,
            hour=_DAY_HOUR,
            ha_connected=True,
            pv_kw=_SAFE_PV_KW,
            spot_price_ore=_POSITIVE_PRICE_ORE,
        )
        assert isinstance(result, GuardEvaluation)

    def test_no_triggers_gives_ok(self) -> None:
        """All guards silent → level must be OK."""
        policy = _make_policy()
        batteries = [make_battery_state()]
        result = policy.evaluate(
            batteries=batteries,
            current_scenario=Scenario.NIGHT_LOW_PV,
            weighted_avg_kw=_SAFE_GRID_KW,
            hour=_DAY_HOUR,
            ha_connected=True,
            pv_kw=_SAFE_PV_KW,
            spot_price_ore=_POSITIVE_PRICE_ORE,
        )
        assert result.level == GuardLevel.OK
        assert result.commands == []


# ===========================================================================
# ExportGuard commands merged when triggered
# ===========================================================================


class TestExportGuardIntegration:
    """GuardPolicy merges ExportGuard commands into the composed result."""

    def test_low_pv_adds_export_limit_command(self) -> None:
        """Low PV → SET_EXPORT_LIMIT command present in composed result."""
        policy = _make_policy()
        batteries = [make_battery_state()]
        result = policy.evaluate(
            batteries=batteries,
            current_scenario=Scenario.NIGHT_LOW_PV,
            weighted_avg_kw=_SAFE_GRID_KW,
            hour=_DAY_HOUR,
            ha_connected=True,
            pv_kw=_LOW_PV_KW,
            spot_price_ore=_POSITIVE_PRICE_ORE,
        )
        export_cmds = [
            c for c in result.commands if c.command_type == CommandType.SET_EXPORT_LIMIT
        ]
        assert len(export_cmds) == 1, "Export limit command must appear in composed result"
        assert export_cmds[0].target_id == "all"

    def test_negative_price_adds_export_limit_command(self) -> None:
        """Negative spot price → SET_EXPORT_LIMIT command in composed result."""
        policy = _make_policy()
        batteries = [make_battery_state()]
        result = policy.evaluate(
            batteries=batteries,
            current_scenario=Scenario.NIGHT_LOW_PV,
            weighted_avg_kw=_SAFE_GRID_KW,
            hour=_DAY_HOUR,
            ha_connected=True,
            pv_kw=_SAFE_PV_KW,
            spot_price_ore=_NEGATIVE_PRICE_ORE,
        )
        export_cmds = [
            c for c in result.commands if c.command_type == CommandType.SET_EXPORT_LIMIT
        ]
        assert len(export_cmds) == 1

    def test_export_trigger_escalates_ok_to_warning(self) -> None:
        """If GridGuard is OK but ExportGuard triggers → level escalated to WARNING."""
        policy = _make_policy()
        batteries = [make_battery_state()]
        result = policy.evaluate(
            batteries=batteries,
            current_scenario=Scenario.NIGHT_LOW_PV,
            weighted_avg_kw=_SAFE_GRID_KW,
            hour=_DAY_HOUR,
            ha_connected=True,
            pv_kw=_LOW_PV_KW,
            spot_price_ore=_POSITIVE_PRICE_ORE,
        )
        assert result.level == GuardLevel.WARNING

    def test_export_trigger_does_not_downgrade_higher_level(self) -> None:
        """ExportGuard must not downgrade a BREACH/CRITICAL from GridGuard."""
        policy = _make_policy()
        # Force G3 BREACH by setting grid import above tak
        _BREACH_KW: float = 10.0
        batteries = [make_battery_state()]
        result = policy.evaluate(
            batteries=batteries,
            current_scenario=Scenario.NIGHT_LOW_PV,
            weighted_avg_kw=_BREACH_KW,
            hour=_DAY_HOUR,
            ha_connected=True,
            pv_kw=_LOW_PV_KW,
            spot_price_ore=_POSITIVE_PRICE_ORE,
        )
        # Level must be at least BREACH — not downgraded to WARNING
        assert result.level not in (GuardLevel.OK, GuardLevel.WARNING)

    def test_export_violation_added_to_violations_list(self) -> None:
        """When ExportGuard triggers, its reason appears in violations."""
        policy = _make_policy()
        batteries = [make_battery_state()]
        result = policy.evaluate(
            batteries=batteries,
            current_scenario=Scenario.NIGHT_LOW_PV,
            weighted_avg_kw=_SAFE_GRID_KW,
            hour=_DAY_HOUR,
            ha_connected=True,
            pv_kw=_LOW_PV_KW,
            spot_price_ore=_POSITIVE_PRICE_ORE,
        )
        assert any("export" in v.lower() for v in result.violations), (
            "Export violation reason must appear in GuardEvaluation.violations"
        )


# ===========================================================================
# Priority: GridGuard runs before ExportGuard
# ===========================================================================


class TestGuardPolicyPriority:
    """Grid safety guards must not be suppressed by ExportGuard."""

    def test_grid_guard_commands_preserved_with_export_trigger(self) -> None:
        """When both guards trigger, all commands are present in result."""
        policy = _make_policy()
        # Trigger G3 (high grid import) AND ExportGuard (low PV)
        _HIGH_GRID_KW: float = 8.0
        batteries = [make_battery_state()]
        result = policy.evaluate(
            batteries=batteries,
            current_scenario=Scenario.NIGHT_LOW_PV,
            weighted_avg_kw=_HIGH_GRID_KW,
            hour=_DAY_HOUR,
            ha_connected=True,
            pv_kw=_LOW_PV_KW,
            spot_price_ore=_POSITIVE_PRICE_ORE,
        )
        # At least one G3 command and one EXPORT command
        g3_cmds = [c for c in result.commands if c.guard_id == "G3"]
        export_cmds = [
            c for c in result.commands if c.command_type == CommandType.SET_EXPORT_LIMIT
        ]
        assert len(g3_cmds) > 0, "G3 commands must be present"
        assert len(export_cmds) > 0, "Export limit commands must be present"
