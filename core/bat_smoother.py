"""CARMA Box — Battery night smoother (PLAT-1790 R-natt-bat).

Pure function module for Alt B bat smoothing during night EV charging.

Design:
- Grid is the primary source for EV + house during night window (22-06).
- Battery acts as transient buffer only — it does NOT actively drain to support EV.
- Smoothing activates when a grid transient (spike) exceeds 0 W (pre-filtered by caller).
- Hard cap: cfg.cap_w (default 500 W) limits bat discharge per transient.
- SoC floor: cfg.soc_floor_pct (default 80%) protects morning-peak arbitrage capacity.

Rationale for Alt B over Alt C (full bat-assist):
  bat cycle cost (~2-3 öre/kWh) + opportunity cost (~70 öre/kWh)
  vs grid night price (~30 öre/kWh)
  → net: -40 öre/kWh for bat-assist → Alt C is economically suboptimal.
  Alt B (smoothing only) costs < 0.6 kWh per night, preserving bat for 06-09 peak.

No HA imports — pure Python, fully unit-testable without mocking.
"""

from __future__ import annotations


def compute_night_smoothing(
    grid_transient_w: float,
    bat_soc_pct: float,
    cap_w: int,
    soc_floor_pct: float,
    enabled: bool = True,
) -> float:
    """Compute battery discharge for night EV charging grid transient smoothing.

    Called each cycle when ev_dispatch is in night_charging=True mode.
    The coordinator pre-computes grid_transient_w (e.g. max(0, Δgrid_w))
    and passes it here. This function is stateless.

    Args:
        grid_transient_w: Positive grid spike in watts (0 = grid steady).
                          Computed by coordinator as max(0, grid_w - prev_grid_w)
                          or similar delta. Must be >= 0.
        bat_soc_pct: Current battery SoC in percent.
        cap_w: Maximum bat discharge for smoothing (W). Typically 500 W.
        soc_floor_pct: Bat SoC floor during night EV. Smoothing stops at or below this.
                       Typically 80% — preserves bat for 06-09 morning peak arbitrage.
        enabled: Master switch. False = always return 0.0.

    Returns:
        Watts the battery should discharge to smooth the transient.
        0.0 if smoothing is not needed or not permitted.
    """
    if not enabled:
        return 0.0
    if bat_soc_pct <= soc_floor_pct:
        return 0.0
    if grid_transient_w <= 0:
        return 0.0
    return min(grid_transient_w, float(cap_w))
