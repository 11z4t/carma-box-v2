"""Tiered grid-sensor controller (PLAT-1737).

Pure function module translating raw grid-sensor readings to a signed
power-delta that Budget applies to its bat-target — giving fast response
to micro-fluctuations (moln-dippar) without moving mode-change decisions
on every transient.

Two interfaces:

  * ``tune_grid_delta(raw_grid_w, cfg)`` → signed int W
      Graderad respons vid ±50/75/100 W (default) med ±100/300/500 W
      korrektion. Inga mode-byten — bara power-limit-justering.

  * ``GridRollingState`` + ``should_block_mode_change`` → anti-flap guard
      Håller 5-min rullande medelvärde. Om |avg| < stability_w blockeras
      mode-change (charge ↔ discharge) så noise inte driver oscillation.

Signs:
  raw_grid_w > 0  → grid import. Delta positiv = bat ska lyfta net
                    (discharge mer eller charge mindre) → grid mot 0.
  raw_grid_w < 0  → grid export. Delta negativ = bat ska sänka net
                    (charge mer eller discharge mindre).

Konfiguration ligger helt i ``GridTunerConfig`` (site.yaml) — inga
magic numbers i koden.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass(frozen=True)
class GridTunerConfig:
    """Tunables — kund-agnostisk, all from site.yaml."""

    enabled: bool = False
    # Thresholds (ascending) where each tier starts to apply. |grid| below
    # the first tier is dead-band (no action).
    tiers_w: tuple[float, ...] = (50.0, 75.0, 100.0)
    # Correction magnitude per tier, same length as tiers_w. Applied signed
    # by the grid direction (import → positive, export → negative).
    corrections_w: tuple[int, ...] = (100, 300, 500)
    # Rolling window size for anti-flap mode-change guard.
    rolling_window_s: int = 300
    # If rolling grid avg is within ±this many W, mode-change is blocked.
    mode_change_stability_w: float = 50.0


def tune_grid_delta(raw_grid_w: float, cfg: GridTunerConfig) -> int:
    """Return signed bat-net delta (W) for the current raw grid reading.

    Positive delta → bat should raise net power (more discharge / less
    charge). Negative → bat should lower net power (more charge / less
    discharge). Zero → grid inside dead-band, hold steady.

    No physical clamping here — the caller combines this delta with
    zero_grid output and clamps against ``BatLimits.max_{charge,discharge}_w``.
    """
    if not cfg.enabled:
        return 0
    abs_g = abs(raw_grid_w)
    if abs_g < cfg.tiers_w[0]:
        return 0
    correction = 0
    for tier, corr in zip(cfg.tiers_w, cfg.corrections_w):
        if abs_g >= tier:
            correction = corr
    sign = 1 if raw_grid_w > 0 else -1
    return sign * correction


@dataclass
class GridRollingState:
    """Mutable 5-min sliding window of grid-power samples.

    Caller owns the instance and calls ``add()`` once per control cycle
    (mutable by design — Budget holds the single copy in BudgetState).
    ``avg()`` is a plain arithmetic mean of surviving samples.
    """

    history: deque[tuple[float, float]] = field(default_factory=deque)

    def add(self, ts: float, grid_w: float, window_s: int) -> None:
        """Append a sample and prune anything older than ``window_s``."""
        self.history.append((ts, grid_w))
        cutoff = ts - window_s
        while self.history and self.history[0][0] < cutoff:
            self.history.popleft()

    def avg(self) -> float:
        """Arithmetic mean of samples still in the window, or 0.0 if empty."""
        if not self.history:
            return 0.0
        return sum(g for _, g in self.history) / len(self.history)


def should_block_mode_change(
    rolling_avg_w: float, cfg: GridTunerConfig,
) -> bool:
    """True if the rolling grid avg is close enough to zero that changing
    bat mode (charge ↔ discharge) would likely be a false positive —
    caller should keep the current mode until the trend is real.

    Strict inequality at the boundary: a sample EXACTLY at the stability
    width is treated as "trend is real" so the guard cannot trap at the
    exact edge indefinitely.
    """
    return abs(rolling_avg_w) < cfg.mode_change_stability_w
