"""PLAT-1757: Integration tests — atomär HA sensor-batch-read.

Verifies that battery sensor reads are atomic (single get_states_batch call)
even when HA API has 50-100 ms latency.

Without the fix: sequential per-battery get_states_batch() → N calls →
algorithm sees data from different HA snapshots (race condition).

With the fix: single get_states_batch(all_bat_entities) → 1 call →
all batteries guaranteed to read from the same HA snapshot.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from config.schema import load_config
from main import CarmaBoxService

# ---------------------------------------------------------------------------
# Test constants (no magic numbers)
# ---------------------------------------------------------------------------

_CONFIG_PATH = str(Path(__file__).resolve().parents[2] / "config" / "site.yaml")
_HA_LATENCY_S: float = 0.050  # 50 ms simulated HA API latency
_MAX_SKEW_S: float = 0.010  # 10 ms — AC3: p99 sensor-skew < 10 ms
_SOC_BAT_A_PCT: float = 55.0
_SOC_BAT_B_PCT: float = 70.0
_MIN_BATTERIES_FOR_RACE: int = 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_cfg() -> Any:
    """Load the production site.yaml config."""
    return load_config(_CONFIG_PATH)


def _build_batch_response(cfg: Any) -> dict[str, dict[str, str]]:
    """Build a minimal batch response dict for all battery entities."""
    response: dict[str, dict[str, str]] = {}
    soc_values = [_SOC_BAT_A_PCT, _SOC_BAT_B_PCT]
    for i, bat_cfg in enumerate(cfg.batteries):
        ents = bat_cfg.entities
        soc = soc_values[i % len(soc_values)]
        response[ents.soc] = {"entity_id": ents.soc, "state": str(soc)}
        response[ents.power] = {"entity_id": ents.power, "state": "0"}
        response[ents.ems_mode] = {
            "entity_id": ents.ems_mode,
            "state": "battery_standby",
        }
        response[ents.ems_power_limit] = {
            "entity_id": ents.ems_power_limit,
            "state": "0",
        }
        response[ents.fast_charging] = {
            "entity_id": ents.fast_charging,
            "state": "off",
        }
    return response


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
class TestSensorRacePlat1757:
    """Integration tests: atomic bat sensor read under simulated HA latency."""

    async def test_single_batch_call_with_50ms_latency(self) -> None:
        """PLAT-1757 AC2: 50 ms HA latency must not cause N separate batch calls.

        Simulates realistic HA API latency.  With the atomic fix, ALL battery
        entities are fetched in exactly 1 get_states_batch() call so sensor
        skew is structurally impossible — not dependent on cache TTL alignment.
        """
        cfg = _load_cfg()
        assert (
            len(cfg.batteries) >= _MIN_BATTERIES_FOR_RACE
        ), f"Need ≥{_MIN_BATTERIES_FOR_RACE} batteries for sensor-race test"

        batch_response = _build_batch_response(cfg)
        call_entity_sets: list[frozenset[str]] = []

        async def _slow_batch(entity_ids: list[str]) -> dict[str, dict[str, str]]:
            """Simulate 50 ms HA latency on each batch call."""
            call_entity_sets.append(frozenset(entity_ids))
            await asyncio.sleep(_HA_LATENCY_S)
            return {k: v for k, v in batch_response.items() if k in entity_ids}

        mock_api = AsyncMock()
        mock_api.get_states_batch = _slow_batch  # type: ignore[method-assign]
        mock_api.get_state = AsyncMock(return_value=None)

        service = CarmaBoxService(cfg, ha_api=mock_api)
        await service._collect_snapshot(ha_connected=True)

        # Count how many batch calls included battery entities
        bat_soc_entities = frozenset(b.entities.soc for b in cfg.batteries)
        bat_calls = [s for s in call_entity_sets if s & bat_soc_entities]

        assert len(bat_calls) == 1, (
            f"PLAT-1757: expected 1 atomic batch call covering all batteries, "
            f"got {len(bat_calls)} calls. "
            "This means batteries are read from different HA snapshots."
        )

        # All bat soc entities must be in the single call
        single_call_entities = bat_calls[0]
        for bat_cfg in cfg.batteries:
            assert (
                bat_cfg.entities.soc in single_call_entities
            ), f"Battery {bat_cfg.id} soc not in single atomic batch call"

    async def test_sensor_skew_is_zero_with_atomic_read(self) -> None:
        """PLAT-1757 AC3: sensor skew between batteries must be 0 ms with atomic read.

        When all batteries are read in a single get_states_batch() call, their
        data all comes from the same HTTP response — structural skew = 0 ms.
        (Live target: p99 < 10 ms via network jitter; here we verify structural 0.)
        """
        cfg = _load_cfg()
        assert len(cfg.batteries) >= _MIN_BATTERIES_FOR_RACE

        batch_response = _build_batch_response(cfg)

        bat_soc_entities = {b.entities.soc for b in cfg.batteries}
        read_times_per_entity: dict[str, float] = {}
        call_count_covering_bats = 0

        async def _tracking_batch(entity_ids: list[str]) -> dict[str, dict[str, str]]:
            nonlocal call_count_covering_bats
            now = time.monotonic()
            await asyncio.sleep(_HA_LATENCY_S)
            response = {k: v for k, v in batch_response.items() if k in entity_ids}
            for eid in entity_ids:
                if eid in bat_soc_entities:
                    read_times_per_entity[eid] = now
            if any(eid in bat_soc_entities for eid in entity_ids):
                call_count_covering_bats += 1
            return response

        mock_api = AsyncMock()
        mock_api.get_states_batch = _tracking_batch  # type: ignore[method-assign]
        mock_api.get_state = AsyncMock(return_value=None)

        service = CarmaBoxService(cfg, ha_api=mock_api)
        await service._collect_snapshot(ha_connected=True)

        # Structural check: only 1 call covered battery entities → skew = 0
        assert call_count_covering_bats == 1, (
            f"Expected 1 batch call covering all batteries (skew=0), "
            f"got {call_count_covering_bats}."
        )

        # All bat soc entities must have been read in the same call (same timestamp)
        if len(read_times_per_entity) >= _MIN_BATTERIES_FOR_RACE:
            times = list(read_times_per_entity.values())
            max_skew = max(times) - min(times)
            assert max_skew < _MAX_SKEW_S, (
                f"Sensor skew {max_skew * 1000:.1f} ms exceeds "
                f"{_MAX_SKEW_S * 1000:.0f} ms limit. "
                "Batteries were read from different HTTP responses."
            )

    async def test_soc_values_correct_under_latency(self) -> None:
        """PLAT-1757: correct SoC values extracted even with 50 ms simulated latency."""
        cfg = _load_cfg()
        assert len(cfg.batteries) >= _MIN_BATTERIES_FOR_RACE

        batch_response = _build_batch_response(cfg)
        expected_socs = {
            bat_cfg.id: (_SOC_BAT_A_PCT if i == 0 else _SOC_BAT_B_PCT)
            for i, bat_cfg in enumerate(cfg.batteries)
        }

        async def _slow_batch(entity_ids: list[str]) -> dict[str, dict[str, str]]:
            await asyncio.sleep(_HA_LATENCY_S)
            return {k: v for k, v in batch_response.items() if k in entity_ids}

        mock_api = AsyncMock()
        mock_api.get_states_batch = _slow_batch  # type: ignore[method-assign]
        mock_api.get_state = AsyncMock(return_value=None)

        service = CarmaBoxService(cfg, ha_api=mock_api)
        snapshot = await service._collect_snapshot(ha_connected=True)

        assert snapshot is not None
        assert len(snapshot.batteries) == len(cfg.batteries)
        for bat in snapshot.batteries:
            expected = expected_socs.get(bat.battery_id)
            assert bat.soc_pct == pytest.approx(
                expected
            ), f"Battery {bat.battery_id}: expected SoC {expected}, got {bat.soc_pct}"
