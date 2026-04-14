"""Ellevio Peak Tracking for CARMA Box.

Replaces 16 HA YAML automations with in-process Python calculations.

Tracks:
- Weighted hourly average (rolling within each clock hour)
- Top-3 monthly peaks (weighted)
- Monthly average of top-3
- Hit rate (% of hours under target)

Ellevio billing: mean(top-3 weighted hourly peaks) * 81.25 kr/kW/month.
Night hours (22-06) weighted at 0.5x.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class EllevioConfig:
    """Ellevio tracking configuration."""

    tak_kw: float = 3.0
    night_weight: float = 0.5
    day_weight: float = 1.0
    night_start_h: int = 22
    night_end_h: int = 6
    top_n: int = 3
    cost_per_kw: float = 81.25


@dataclass
class HourSample:
    """One hour's accumulated data."""

    hour: int
    date: str  # YYYY-MM-DD
    sum_kw: float = 0.0
    count: int = 0
    weight: float = 1.0

    @property
    def weighted_avg_kw(self) -> float:
        if self.count == 0:
            return 0.0
        return (self.sum_kw / self.count) * self.weight


@dataclass
class EllevioState:
    """Monthly Ellevio tracking state."""

    month: int = 0
    year: int = 0
    top_peaks: list[float] = field(default_factory=list)
    hours_total: int = 0
    hours_under_target: int = 0
    current_hour_sample: HourSample | None = None
    last_hourly_kw: float = 0.0

    @property
    def top_n_avg(self) -> float:
        if not self.top_peaks:
            return 0.0
        return sum(self.top_peaks) / len(self.top_peaks)

    @property
    def hit_rate_pct(self) -> float:
        if self.hours_total == 0:
            return 100.0
        return (self.hours_under_target / self.hours_total) * 100.0



class EllevioTracker:
    """Tracks Ellevio weighted hourly peaks.

    Call update() every 30s cycle with current grid import.
    Automatically detects hour boundaries and records peaks.
    """

    def __init__(self, config: EllevioConfig | None = None) -> None:
        self._config = config or EllevioConfig()
        self.state = EllevioState()

    @property
    def monthly_cost_kr(self) -> float:
        """Monthly peak cost using configured cost_per_kw."""
        return self.state.top_n_avg * self._config.cost_per_kw

    def update(
        self,
        grid_import_kw: float,
        now: datetime,
    ) -> float:
        """Update with current grid import reading.

        Args:
            grid_import_kw: Current grid import in kW (positive=import).
            now: Current timestamp.

        Returns:
            Current weighted hourly average (kW).
        """
        # Reset on new month
        if (
            self.state.month != now.month
            or self.state.year != now.year
        ):
            logger.info(
                "Ellevio: new month %d/%d — reset",
                now.month, now.year,
            )
            self.state = EllevioState(
                month=now.month, year=now.year,
            )

        # Determine weight
        is_night = (
            now.hour >= self._config.night_start_h
            or now.hour < self._config.night_end_h
        )
        weight = (
            self._config.night_weight if is_night
            else self._config.day_weight
        )

        date_str = now.strftime("%Y-%m-%d")

        # New hour? Close previous and start new
        current = self.state.current_hour_sample
        if current is None or current.hour != now.hour or current.date != date_str:
            if current is not None and current.count > 0:
                self._close_hour(current)
            self.state.current_hour_sample = HourSample(
                hour=now.hour,
                date=date_str,
                weight=weight,
            )

        # Accumulate sample (only positive import)
        sample = self.state.current_hour_sample
        if sample is not None:
            sample.sum_kw += max(0.0, grid_import_kw)
            sample.count += 1

        return self.current_weighted_avg_kw

    @property
    def current_weighted_avg_kw(self) -> float:
        """Current hour's weighted average so far."""
        sample = self.state.current_hour_sample
        if sample is None or sample.count == 0:
            return 0.0
        return sample.weighted_avg_kw

    def _close_hour(self, sample: HourSample) -> None:
        """Close an hour — record peak if applicable."""
        weighted = sample.weighted_avg_kw
        self.state.last_hourly_kw = weighted
        self.state.hours_total += 1

        if weighted <= self._config.tak_kw:
            self.state.hours_under_target += 1

        # Update top-N peaks
        self.state.top_peaks.append(weighted)
        self.state.top_peaks.sort(reverse=True)
        self.state.top_peaks = self.state.top_peaks[
            : self._config.top_n
        ]

        logger.info(
            "Ellevio: hour %02d closed — weighted=%.2f kW, "
            "top3=[%s], avg=%.2f kW",
            sample.hour,
            weighted,
            ", ".join(f"{p:.2f}" for p in self.state.top_peaks),
            self.state.top_n_avg,
        )

    def to_dict(self) -> dict[str, object]:
        """Serialize state for persistence."""
        return {
            "month": self.state.month,
            "year": self.state.year,
            "top_peaks": list(self.state.top_peaks),
            "hours_total": self.state.hours_total,
            "hours_under_target": self.state.hours_under_target,
            "last_hourly_kw": self.state.last_hourly_kw,
        }

    def from_dict(self, data: dict[str, object]) -> None:
        """Restore state from persistence."""
        month_val = data.get("month", 0)
        year_val = data.get("year", 0)
        peaks_val = data.get("top_peaks", [])
        hours_val = data.get("hours_total", 0)
        under_val = data.get("hours_under_target", 0)
        last_val = data.get("last_hourly_kw", 0.0)
        self.state = EllevioState(
            month=int(str(month_val)),
            year=int(str(year_val)),
            top_peaks=[float(str(x)) for x in (peaks_val if isinstance(peaks_val, list) else [])],
            hours_total=int(str(hours_val)),
            hours_under_target=int(str(under_val)),
            last_hourly_kw=float(str(last_val)),
        )
