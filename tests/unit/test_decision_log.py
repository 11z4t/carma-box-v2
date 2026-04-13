"""Tests for DecisionLog — per-cycle audit trail."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from core.decision_log import DecisionLog, DecisionRecord
from core.models import Scenario


class TestDecisionRecord:
    def test_to_json(self) -> None:
        record = DecisionRecord(
            cycle_id="abc123",
            timestamp="2026-04-14T01:00:00+00:00",
            elapsed_ms=42,
            scenario="MIDDAY_CHARGE",
            guard_level="ok",
            guard_commands=[],
            balance_total_w=3000,
            commands_succeeded=2,
            commands_failed=0,
            error=None,
        )
        j = record.to_json()
        d = json.loads(j)
        assert d["cycle_id"] == "abc123"
        assert d["elapsed_ms"] == 42
        assert d["scenario"] == "MIDDAY_CHARGE"


class TestDecisionLog:
    def test_record_increments_count(self) -> None:
        log = DecisionLog()
        assert log.cycle_count == 0
        log.record(
            cycle_id="c1",
            timestamp=datetime.now(tz=timezone.utc),
            elapsed_s=0.03,
            scenario=Scenario.MIDDAY_CHARGE,
        )
        assert log.cycle_count == 1

    def test_record_returns_record(self) -> None:
        log = DecisionLog()
        r = log.record(
            cycle_id="c2",
            timestamp=datetime.now(tz=timezone.utc),
            elapsed_s=0.05,
            scenario=Scenario.EVENING_DISCHARGE,
            guard_level="breach",
            guard_commands=["SET_EV_CURRENT"],
            commands_succeeded=1,
            commands_failed=0,
        )
        assert r.scenario == "EVENING_DISCHARGE"
        assert r.guard_level == "breach"
        assert r.guard_commands == ["SET_EV_CURRENT"]

    def test_persist_callback_called(self) -> None:
        records: list[DecisionRecord] = []
        log = DecisionLog(persist_callback=records.append)
        log.record(
            cycle_id="c3",
            timestamp=datetime.now(tz=timezone.utc),
            elapsed_s=0.01,
            scenario=Scenario.NIGHT_HIGH_PV,
        )
        assert len(records) == 1
        assert records[0].cycle_id == "c3"

    def test_persist_error_does_not_crash(self) -> None:
        def bad_persist(_: DecisionRecord) -> None:
            raise RuntimeError("DB down")

        log = DecisionLog(persist_callback=bad_persist)
        # Must not raise
        r = log.record(
            cycle_id="c4",
            timestamp=datetime.now(tz=timezone.utc),
            elapsed_s=0.01,
            scenario=Scenario.MIDDAY_CHARGE,
        )
        assert r.cycle_id == "c4"

    def test_error_field_preserved(self) -> None:
        log = DecisionLog()
        r = log.record(
            cycle_id="c5",
            timestamp=datetime.now(tz=timezone.utc),
            elapsed_s=0.1,
            scenario=Scenario.MIDDAY_CHARGE,
            error="inverter timeout",
        )
        assert r.error == "inverter timeout"
