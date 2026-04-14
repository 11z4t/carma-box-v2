"""Tests for PLAT-1540: ExportGuard — dynamic export limit based on PV and price."""

from __future__ import annotations

from core.guards import EXPORT_MIN_PV_KW, EXPORT_NEGATIVE_PRICE_ORE, ExportGuard
from core.models import CommandType

# ---------------------------------------------------------------------------
# Constants — no magic numbers in tests
# ---------------------------------------------------------------------------

# A PV level safely above the minimum — export must be allowed
_SAFE_PV_KW: float = EXPORT_MIN_PV_KW + 1.0

# A PV level below the minimum — export must be limited
_LOW_PV_KW: float = EXPORT_MIN_PV_KW - 0.1

# Positive spot price — no reason to limit on price alone
_POSITIVE_PRICE_ORE: float = 50.0

# Negative spot price — export must be limited
_NEGATIVE_PRICE_ORE: float = -10.0

# Zero price — exactly at threshold, must limit (≤ 0)
_ZERO_PRICE_ORE: float = 0.0


# ===========================================================================
# AC1: PV below threshold → export limited
# ===========================================================================


class TestExportLimitedByLowPV:
    """ExportGuard.evaluate emits EXPORT_LIMIT when pv_kw < EXPORT_MIN_PV_KW."""

    def test_low_pv_triggers_limit(self) -> None:
        """AC1: PV below EXPORT_MIN_PV_KW → limited=True + SET_EXPORT_LIMIT command."""
        guard = ExportGuard()
        result = guard.evaluate(pv_kw=_LOW_PV_KW, spot_price_ore=_POSITIVE_PRICE_ORE)

        assert result.limited is True, (
            f"PV {_LOW_PV_KW} kW below min {EXPORT_MIN_PV_KW} kW must trigger limit"
        )
        assert len(result.commands) == 1
        assert result.commands[0].command_type == CommandType.SET_EXPORT_LIMIT
        assert result.commands[0].guard_id == "EXPORT"
        assert result.commands[0].target_id == "all"
        assert result.commands[0].value == 0

    def test_pv_above_threshold_not_limited(self) -> None:
        """AC1 (negative): PV above threshold + positive price → no limit."""
        guard = ExportGuard()
        result = guard.evaluate(pv_kw=_SAFE_PV_KW, spot_price_ore=_POSITIVE_PRICE_ORE)

        assert result.limited is False
        assert result.commands == []


# ===========================================================================
# AC2: Negative spot price → export limited
# ===========================================================================


class TestExportLimitedByNegativePrice:
    """ExportGuard limits export when spot price is negative."""

    def test_negative_price_triggers_limit(self) -> None:
        """AC2: spot_price_ore < 0 → limited=True even when PV is sufficient."""
        guard = ExportGuard()
        result = guard.evaluate(pv_kw=_SAFE_PV_KW, spot_price_ore=_NEGATIVE_PRICE_ORE)

        assert result.limited is True, (
            f"Negative spot price {_NEGATIVE_PRICE_ORE} öre must trigger export limit"
        )
        assert len(result.commands) == 1
        assert result.commands[0].command_type == CommandType.SET_EXPORT_LIMIT

    def test_zero_price_triggers_limit(self) -> None:
        """AC2 (boundary): price = 0 öre → still limited (≤ EXPORT_NEGATIVE_PRICE_ORE)."""
        guard = ExportGuard()
        result = guard.evaluate(pv_kw=_SAFE_PV_KW, spot_price_ore=_ZERO_PRICE_ORE)

        assert result.limited is True, (
            f"Zero price ({_ZERO_PRICE_ORE} öre) must trigger limit"
            f" — value ≤ {EXPORT_NEGATIVE_PRICE_ORE}"
        )


# ===========================================================================
# AC3: Named constants — no magic numbers
# ===========================================================================


class TestExportGuardConstants:
    """EXPORT_MIN_PV_KW and EXPORT_NEGATIVE_PRICE_ORE must be named constants."""

    def test_export_min_pv_kw_is_named(self) -> None:
        """EXPORT_MIN_PV_KW must be importable as a named constant."""
        assert isinstance(EXPORT_MIN_PV_KW, float)
        assert EXPORT_MIN_PV_KW > 0.0

    def test_export_negative_price_ore_is_named(self) -> None:
        """EXPORT_NEGATIVE_PRICE_ORE must be importable and equal to 0.0."""
        assert isinstance(EXPORT_NEGATIVE_PRICE_ORE, float)
        assert EXPORT_NEGATIVE_PRICE_ORE == 0.0


# ===========================================================================
# AC4: Boundary / edge cases
# ===========================================================================


class TestExportGuardBoundary:
    """Boundary: exactly at EXPORT_MIN_PV_KW is NOT limited (< not ≤)."""

    def test_pv_exactly_at_threshold_not_limited(self) -> None:
        """pv_kw == EXPORT_MIN_PV_KW → NOT limited (threshold is strict <)."""
        guard = ExportGuard()
        result = guard.evaluate(pv_kw=EXPORT_MIN_PV_KW, spot_price_ore=_POSITIVE_PRICE_ORE)

        assert result.limited is False, (
            f"PV exactly at threshold {EXPORT_MIN_PV_KW} kW must NOT be limited"
        )

    def test_reason_populated_when_limited(self) -> None:
        """Result reason must be non-empty when export is limited."""
        guard = ExportGuard()
        result = guard.evaluate(pv_kw=_LOW_PV_KW, spot_price_ore=_POSITIVE_PRICE_ORE)
        assert result.reason != ""

    def test_reason_empty_when_not_limited(self) -> None:
        """Result reason must be empty when export is not limited."""
        guard = ExportGuard()
        result = guard.evaluate(pv_kw=_SAFE_PV_KW, spot_price_ore=_POSITIVE_PRICE_ORE)
        assert result.reason == ""
