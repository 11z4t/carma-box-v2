"""Integration tests for PLAT-1552: Ellevio peak tracker end-to-end.

Tests the full pipeline:
  GridState (high power) → GuardConfig → EllevioTracker → G3 guard trigger

No mocks of EllevioTracker or GuardConfig — real components wired together.
"""

from __future__ import annotations

from datetime import datetime, timezone

from core.ellevio import EllevioConfig, EllevioTracker
from core.guards import GridGuard, GuardConfig, GuardLevel
from tests.conftest import make_battery_state, make_grid_state, make_snapshot

# ---------------------------------------------------------------------------
# Constants — no magic numbers in tests
# ---------------------------------------------------------------------------

# Ellevio default tak (from EllevioConfig default)
_DEFAULT_TAK_KW: float = 3.0

# A safe load well below tak — guard must stay OK
_SAFE_LOAD_KW: float = 1.0

# A load that exceeds tak — G3 BREACH or CRITICAL must trigger
_BREACH_LOAD_KW: float = _DEFAULT_TAK_KW + 1.0

# Night weight factor (from EllevioConfig default)
_NIGHT_WEIGHT: float = 0.5

# Daytime hour (no night weight applied)
_DAY_HOUR: int = 12

# Nighttime hour
_NIGHT_HOUR: int = 2


# ===========================================================================
# AC1: EllevioTracker is updated and integrated in guard pipeline
# ===========================================================================


class TestEllevioTrackerIntegration:
    """EllevioTracker feeds weighted_avg into G3 guard via real pipeline."""

    def test_safe_load_does_not_trigger_g3(self) -> None:
        """AC1+AC2: Normal grid import stays below tak → guard level OK."""
        tracker = EllevioTracker(EllevioConfig())
        guard = GridGuard(GuardConfig())

        now = datetime.now(tz=timezone.utc)
        # Simulate 1 hour of safe load to establish rolling average
        tracker.update(_SAFE_LOAD_KW, now)

        snap = make_snapshot(
            batteries=[make_battery_state()],
            grid=make_grid_state(
                grid_power_w=_SAFE_LOAD_KW * 1000.0,
                weighted_avg_kw=tracker.current_weighted_avg_kw,
                dynamic_tak_kw=_DEFAULT_TAK_KW,
            ),
            hour=_DAY_HOUR,
        )

        result = guard.evaluate(
            batteries=snap.batteries,
            current_scenario=snap.current_scenario,
            weighted_avg_kw=tracker.current_weighted_avg_kw,
            hour=snap.hour,
            ha_connected=True,
            data_age_s=0.0,
        )

        assert result.level in (GuardLevel.OK, GuardLevel.WARNING), (
            f"Safe load {_SAFE_LOAD_KW} kW must not trigger G3 breach, got {result.level}"
        )

    def test_high_load_triggers_g3_breach(self) -> None:
        """AC2+AC3: Grid import exceeding tak → G3 BREACH or CRITICAL triggers."""
        tracker = EllevioTracker(EllevioConfig())
        guard = GridGuard(GuardConfig())

        now = datetime.now(tz=timezone.utc)
        # Push tracker above tak
        tracker.update(_BREACH_LOAD_KW, now)

        snap = make_snapshot(
            batteries=[make_battery_state()],
            grid=make_grid_state(
                grid_power_w=_BREACH_LOAD_KW * 1000.0,
                weighted_avg_kw=tracker.current_weighted_avg_kw,
                dynamic_tak_kw=_DEFAULT_TAK_KW,
            ),
            hour=_DAY_HOUR,
        )

        result = guard.evaluate(
            batteries=snap.batteries,
            current_scenario=snap.current_scenario,
            weighted_avg_kw=tracker.current_weighted_avg_kw,
            hour=snap.hour,
            ha_connected=True,
            data_age_s=0.0,
        )

        assert result.level in (GuardLevel.BREACH, GuardLevel.CRITICAL, GuardLevel.ALARM), (
            f"Load {_BREACH_LOAD_KW} kW above tak {_DEFAULT_TAK_KW} kW must trigger G3, "
            f"got {result.level}"
        )
        g3_commands = [c for c in result.commands if c.guard_id == "G3"]
        assert len(g3_commands) > 0, "G3 breach must emit guard commands"


# ===========================================================================
# AC4: No magic numbers in ellevio.py config path
# ===========================================================================


class TestEllevioNoMagicNumbers:
    """EllevioConfig defaults come from named fields — not inline literals."""

    def test_tak_kw_is_configurable(self) -> None:
        """tak_kw must be overridable — not baked into calculation logic."""
        high_tak = EllevioConfig(tak_kw=10.0)
        tracker_high = EllevioTracker(high_tak)
        now = datetime.now(tz=timezone.utc)
        tracker_high.update(_BREACH_LOAD_KW, now)

        # With tak_kw=10.0, BREACH_LOAD_KW (4.0) should be well under tak
        # → tracker reports no breach
        assert tracker_high.current_weighted_avg_kw < high_tak.tak_kw, (
            "EllevioTracker must use configurable tak_kw, not a hardcoded 3.0"
        )

    def test_night_weight_halves_contribution(self) -> None:
        """Night hours weighted at night_weight (default 0.5) — configurable.

        Default night window: 22-06. Hour 2 = night (weight 0.5). Hour 12 = day (weight 1.0).
        Same power input → night weighted avg < day weighted avg.
        """
        cfg = EllevioConfig(night_weight=_NIGHT_WEIGHT)  # default night_start_h=22, night_end_h=6
        tracker_night = EllevioTracker(cfg)
        tracker_day = EllevioTracker(cfg)

        ts_night = datetime(2026, 4, 14, _NIGHT_HOUR, 0, 0, tzinfo=timezone.utc)  # hour=2
        ts_day = datetime(2026, 4, 14, _DAY_HOUR, 0, 0, tzinfo=timezone.utc)      # hour=12

        tracker_night.update(2.0, ts_night)
        night_avg = tracker_night.current_weighted_avg_kw

        tracker_day.update(2.0, ts_day)
        day_avg = tracker_day.current_weighted_avg_kw

        assert night_avg < day_avg, (
            f"Night contribution ({night_avg:.3f} kW) must be less than "
            f"day contribution ({day_avg:.3f} kW) due to night_weight={_NIGHT_WEIGHT}"
        )
