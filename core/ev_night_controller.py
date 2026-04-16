"""Night EV Controller — pure decision logic for night-window EV charging.

PLAT-1673 Case 1.

Inputs:  snapshot (EV state, grid weighted, time), config (thresholds), state (last action time)
Outputs: list[Command] (START/STOP/SET_CURRENT) + decision_reason for logging

Pure function — no I/O. Caller (engine) executes commands and persists state.

Strategy locked by user 2026-04-16:
  - Start kl 22:00 ALLTID @ 6A om EV plugged + SoC < target
  - Var 60s: ramp +1A om grid < tak*0.9, -1A om > tak*0.95
  - Stop: SoC >= target, klockan >= night_end, disconnected, G3
  - ALDRIG > target SoC (target läses från HA)
  - ALDRIG > Ellevio tak (mjuk ramp-ner, sedan G3 hard-stop som sista skydd)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from core.models import Command, CommandType, EVState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — internal calculation only
# ---------------------------------------------------------------------------

_RULE_ID: str = "NIGHT_EV"
_SOC_FULL_MARGIN_PCT: float = 0.5     # treat >= target-0.5 as "at target"


# ---------------------------------------------------------------------------
# Config + State
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NightEVConfig:
    """Configuration for night EV controller. All thresholds from site.yaml."""

    night_start_hour: int = 22
    night_end_hour: int = 6
    start_amps: int = 6
    max_amps: int = 10
    min_amps: int = 6
    ramp_step_amps: int = 1
    ramp_interval_s: int = 60
    tak_weighted_kw: float = 3.0
    grid_safety_margin_up: float = 0.9    # ramp up if weighted < tak * this
    grid_safety_margin_down: float = 0.95  # ramp down if weighted > tak * this


@dataclass
class NightEVState:
    """Mutable state of the controller (caller persists between cycles)."""

    current_amps: int = 0
    last_ramp_ts: float = 0.0     # epoch seconds of last ramp action
    last_decision_reason: str = ""


@dataclass(frozen=True)
class NightEVDecision:
    """Result of one evaluate() call."""

    commands: list[Command]
    new_amps: int
    reason: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _in_night_window(now: datetime, cfg: NightEVConfig) -> bool:
    """Return True if `now.hour` is inside the night window."""
    h = now.hour
    if cfg.night_start_hour < cfg.night_end_hour:
        return cfg.night_start_hour <= h < cfg.night_end_hour
    # wraps midnight (e.g. 22 → 6)
    return h >= cfg.night_start_hour or h < cfg.night_end_hour


def _at_or_above_target(ev_soc: float, target: float) -> bool:
    """Return True if EV SoC has effectively reached target (with margin)."""
    return ev_soc >= target - _SOC_FULL_MARGIN_PCT


def _ev_ready_to_start(ev: EVState, target_soc_pct: float) -> bool:
    """Return True if EV is plugged + below target."""
    return ev.connected and not _at_or_above_target(ev.soc_pct, target_soc_pct)


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def evaluate(
    now: datetime,
    ev: EVState,
    grid_weighted_kw: float,
    target_soc_pct: float,
    state: NightEVState,
    cfg: NightEVConfig,
) -> NightEVDecision:
    """Evaluate one cycle. Return commands + new amp setting + reason.

    Caller responsibilities:
      - Read EV state + grid weighted from snapshot
      - Read target_soc_pct from input_number.car_target_soc
      - Persist NightEVState between cycles
      - Execute returned commands via CommandExecutor
    """
    cmds: list[Command] = []

    # Outside night window — STOP if was charging
    if not _in_night_window(now, cfg):
        if ev.charging or state.current_amps > 0:
            cmds.append(_stop_cmd("Outside night window"))
            return NightEVDecision(
                commands=cmds, new_amps=0,
                reason=f"OUTSIDE_NIGHT_WINDOW hour={now.hour}",
            )
        return NightEVDecision(commands=cmds, new_amps=0, reason="OUTSIDE_NIGHT_WINDOW idle")

    # EV not ready to start
    if not ev.connected:
        if ev.charging:
            cmds.append(_stop_cmd("EV disconnected"))
        return NightEVDecision(
            commands=cmds, new_amps=0,
            reason="EV_DISCONNECTED",
        )

    # At or above target — STOP
    if _at_or_above_target(ev.soc_pct, target_soc_pct):
        if ev.charging or state.current_amps > 0:
            cmds.append(_stop_cmd(f"SoC {ev.soc_pct:.1f}% >= target {target_soc_pct:.0f}%"))
        return NightEVDecision(
            commands=cmds, new_amps=0,
            reason=f"AT_TARGET soc={ev.soc_pct:.1f} target={target_soc_pct:.0f}",
        )

    now_ts = now.timestamp()

    # Not yet started — START with start_amps
    if not ev.charging and state.current_amps == 0:
        cmds.append(_start_cmd("Night window opened"))
        cmds.append(_set_current_cmd(cfg.start_amps, "Initial start_amps"))
        return NightEVDecision(
            commands=cmds, new_amps=cfg.start_amps,
            reason=f"START soc={ev.soc_pct:.1f} weighted={grid_weighted_kw:.2f}",
        )

    # Charging — apply ramp logic if interval elapsed
    if now_ts - state.last_ramp_ts < cfg.ramp_interval_s:
        return NightEVDecision(
            commands=cmds, new_amps=state.current_amps,
            reason=f"HOLD {state.current_amps}A (ramp interval {cfg.ramp_interval_s}s not elapsed)",
        )

    threshold_up = cfg.tak_weighted_kw * cfg.grid_safety_margin_up
    threshold_down = cfg.tak_weighted_kw * cfg.grid_safety_margin_down

    if grid_weighted_kw > threshold_down and state.current_amps > cfg.min_amps:
        new_amps = state.current_amps - cfg.ramp_step_amps
        cmds.append(_set_current_cmd(
            new_amps,
            f"RAMP_DOWN weighted={grid_weighted_kw:.2f} > {threshold_down:.2f}",
        ))
        return NightEVDecision(
            commands=cmds, new_amps=new_amps,
            reason=f"RAMP_DOWN to {new_amps}A (weighted={grid_weighted_kw:.2f})",
        )

    if grid_weighted_kw < threshold_up and state.current_amps < cfg.max_amps:
        new_amps = state.current_amps + cfg.ramp_step_amps
        cmds.append(_set_current_cmd(
            new_amps,
            f"RAMP_UP weighted={grid_weighted_kw:.2f} < {threshold_up:.2f}",
        ))
        return NightEVDecision(
            commands=cmds, new_amps=new_amps,
            reason=f"RAMP_UP to {new_amps}A (weighted={grid_weighted_kw:.2f})",
        )

    return NightEVDecision(
        commands=cmds, new_amps=state.current_amps,
        reason=f"HOLD {state.current_amps}A (weighted={grid_weighted_kw:.2f} in band)",
    )


# ---------------------------------------------------------------------------
# Internal command builders
# ---------------------------------------------------------------------------


def _start_cmd(reason: str) -> Command:
    return Command(
        command_type=CommandType.START_EV_CHARGING,
        target_id="ev",
        value=None,
        rule_id=_RULE_ID,
        reason=reason,
    )


def _stop_cmd(reason: str) -> Command:
    return Command(
        command_type=CommandType.STOP_EV_CHARGING,
        target_id="ev",
        value=None,
        rule_id=_RULE_ID,
        reason=reason,
    )


def _set_current_cmd(amps: int, reason: str) -> Command:
    return Command(
        command_type=CommandType.SET_EV_CURRENT,
        target_id="ev",
        value=amps,
        rule_id=_RULE_ID,
        reason=reason,
    )
