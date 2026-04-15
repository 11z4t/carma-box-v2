"""Deterministic closed-loop simulation harness (PLAT-1597).

Drives ControlEngine.run_cycle() with pre-built SystemSnapshot traces.
No I/O, no HA imports — pure Python simulation.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.engine import ControlEngine, CycleResult
from core.models import SystemSnapshot

_DEFAULT_TIME_STEP_S: float = 30.0


@dataclass(frozen=True)
class SimCycleOutput:
    """Result of one simulated cycle."""

    snapshot: SystemSnapshot
    result: CycleResult
    elapsed_s: float


class SimulationHarness:
    """Drive ControlEngine deterministically with snapshot traces.

    Each run() call iterates a list of pre-built snapshots, feeding
    each to engine.run_cycle() and collecting results.
    """

    def __init__(
        self,
        engine: ControlEngine,
        time_step_s: float = _DEFAULT_TIME_STEP_S,
    ) -> None:
        self._engine = engine
        self._time_step_s = time_step_s

    async def run(
        self,
        trace: list[SystemSnapshot],
        *,
        initial_ha_connected: bool = True,
        data_age_s: float = 0.0,
    ) -> list[SimCycleOutput]:
        """Run simulation over a trace of snapshots.

        Args:
            trace: Ordered list of SystemSnapshots to process.
            initial_ha_connected: HA connection state for all cycles.
            data_age_s: Data staleness for all cycles.

        Returns:
            List of SimCycleOutput — one per snapshot.
        """
        outputs: list[SimCycleOutput] = []
        for snap in trace:
            result = await self._engine.run_cycle(
                snapshot=snap,
                ha_connected=initial_ha_connected,
                data_age_s=data_age_s,
            )
            outputs.append(SimCycleOutput(
                snapshot=snap,
                result=result,
                elapsed_s=result.elapsed_s,
            ))
        return outputs
