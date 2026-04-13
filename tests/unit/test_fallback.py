"""Tests for FallbackPolicy — explicit triggers and actions."""

from __future__ import annotations

from core.fallback import (
    FALLBACK_POLICY,
    FallbackAction,
    FallbackTrigger,
    resolve_fallback,
    resolve_soc_fallback,
)


class TestFallbackPolicy:
    def test_all_triggers_have_actions(self) -> None:
        for trigger in FallbackTrigger:
            assert trigger in FALLBACK_POLICY

    def test_ha_disconnected_standby(self) -> None:
        event = resolve_fallback(FallbackTrigger.HA_DISCONNECTED)
        assert event.action == FallbackAction.STANDBY_ALL

    def test_guard_error_freeze(self) -> None:
        event = resolve_fallback(FallbackTrigger.GUARD_ERROR)
        assert event.action == FallbackAction.FREEZE

    def test_config_error_refuse(self) -> None:
        event = resolve_fallback(FallbackTrigger.CONFIG_ERROR)
        assert event.action == FallbackAction.REFUSE_START

    def test_executor_error_retry(self) -> None:
        event = resolve_fallback(FallbackTrigger.EXECUTOR_ERROR)
        assert event.action == FallbackAction.RETRY_NEXT

    def test_detail_preserved(self) -> None:
        event = resolve_fallback(
            FallbackTrigger.STALE_DATA, "sensor lag 600s",
        )
        assert "600s" in event.detail


class TestSoCFallback:
    def test_valid_soc_no_fallback(self) -> None:
        soc, event = resolve_soc_fallback(85.0, 80.0, 3600.0, 10.0)
        assert soc == 85.0
        assert event is None

    def test_negative_soc_uses_last_known(self) -> None:
        soc, event = resolve_soc_fallback(-1.0, 70.0, 3600.0, 30.0)
        assert soc == 70.0
        assert event is not None
        assert event.trigger == FallbackTrigger.INVALID_SOC

    def test_stale_last_known_uses_50(self) -> None:
        soc, event = resolve_soc_fallback(-1.0, 70.0, 3600.0, 7200.0)
        assert soc == 50.0
        assert event is not None

    def test_no_last_known_uses_50(self) -> None:
        soc, event = resolve_soc_fallback(-1.0, -1.0, 3600.0, 0.0)
        assert soc == 50.0
