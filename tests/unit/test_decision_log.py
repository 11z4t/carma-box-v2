"""Tests for DecisionLog — per-cycle audit trail."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from core.decision_log import DecisionLog, DecisionRecord, DecisionTrace
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


# ===========================================================================
# PLAT-1595: DecisionTrace tests
# ===========================================================================

_TEST_CYCLE_ID: str = "test-cycle-001"
_WARN_LEVEL: str = "WARNING"
_TEST_GUARD_REASON: str = "weighted_avg 2.3kW > tak 2.0kW"
_TEST_SCENARIO: str = "EVENING_DISCHARGE"
_TEST_PLAN_USED: str = "night_plan_active"
_TEST_SUPPRESSED_CMD: str = "ev_start: guard_level=CRITICAL"
_TEST_SENT_CMD: str = "set_mode:discharge_pv:kontor"
_TEST_DEGRADED_MODE: str = "stale_soc"
_EXPECTED_SCHEMA_VERSION: str = "1.0"
_TEST_TRACE_TIMESTAMP: datetime = datetime(2026, 4, 16, 3, 0, 0, tzinfo=timezone.utc)


def _make_trace(**overrides: object) -> DecisionTrace:
    defaults: dict[str, object] = {
        "cycle_id": _TEST_CYCLE_ID,
        "timestamp": _TEST_TRACE_TIMESTAMP,
        "scenario": _TEST_SCENARIO,
        "active_guard_level": _WARN_LEVEL,
        "guard_reason": _TEST_GUARD_REASON,
        "plan_used": _TEST_PLAN_USED,
        "commands_sent": [_TEST_SENT_CMD],
        "commands_suppressed": [_TEST_SUPPRESSED_CMD],
        "degraded_modes_active": [_TEST_DEGRADED_MODE],
    }
    defaults.update(overrides)
    return DecisionTrace(**defaults)  # type: ignore[arg-type]


class TestTraceCapturesGuardReason:
    """T1: Guard reason preserved in trace."""

    def test_trace_captures_guard_reason(self) -> None:
        trace = _make_trace()
        assert trace.active_guard_level == _WARN_LEVEL
        assert "weighted_avg" in trace.guard_reason


class TestTraceCapturesSuppressedCommands:
    """T2: Suppressed commands list preserved."""

    def test_trace_captures_suppressed_commands(self) -> None:
        trace = _make_trace()
        assert len(trace.commands_suppressed) == 1
        assert "ev_start" in trace.commands_suppressed[0]


class TestTraceSerializableToJson:
    """T3: to_dict() → json roundtrip."""

    def test_trace_serializable_to_json(self) -> None:
        trace = _make_trace()
        d = trace.to_dict()
        json_str = json.dumps(d)
        roundtrip = json.loads(json_str)
        assert roundtrip == d
        assert "schema_version" in d


class TestTraceSchemaVersioned:
    """T4: SCHEMA_VERSION class attribute in to_dict()."""

    def test_trace_schema_versioned(self) -> None:
        assert isinstance(DecisionTrace.SCHEMA_VERSION, str)
        trace = _make_trace()
        d = trace.to_dict()
        assert d["schema_version"] == _EXPECTED_SCHEMA_VERSION
        assert d["schema_version"] == DecisionTrace.SCHEMA_VERSION
