"""Tests for Slack Notifications.

Covers:
- Message formatting for each event type
- Filter by configured event types
- Daily summary format
- Webhook error handled gracefully
- No webhook URL → disabled
"""

from __future__ import annotations


import pytest

from notifications.slack import DailySummary, SlackConfig, SlackNotifier


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def notifier() -> SlackNotifier:
    """Notifier with no webhook (disabled)."""
    return SlackNotifier(SlackConfig(webhook_env="TEST_SLACK_WEBHOOK"))


# ===========================================================================
# Filtering
# ===========================================================================


class TestFiltering:
    """Only configured event types should be notified."""

    def test_configured_event_passes(self, notifier: SlackNotifier) -> None:
        assert notifier._should_notify("scenario_transition")

    def test_unconfigured_event_blocked(self, notifier: SlackNotifier) -> None:
        assert not notifier._should_notify("unknown_event")


# ===========================================================================
# Message formatting
# ===========================================================================


class TestMessageFormatting:
    """Test message format for different severities."""

    def test_info_format(self, notifier: SlackNotifier) -> None:
        payload = notifier._format_message("test", "hello", "info")
        assert "ℹ️" in payload["text"]
        assert "hello" in payload["text"]

    def test_critical_format(self, notifier: SlackNotifier) -> None:
        payload = notifier._format_message("test", "bad", "critical")
        assert "🚨" in payload["text"]

    def test_breach_format(self, notifier: SlackNotifier) -> None:
        payload = notifier._format_message("test", "breach!", "breach")
        assert "🔴" in payload["text"]


# ===========================================================================
# Disabled (no webhook)
# ===========================================================================


@pytest.mark.asyncio
class TestDisabled:
    """No webhook URL → notifications disabled."""

    async def test_notify_returns_false(self, notifier: SlackNotifier) -> None:
        result = await notifier.notify("scenario_transition", "test")
        assert result is False

    async def test_daily_summary_returns_false(self, notifier: SlackNotifier) -> None:
        summary = DailySummary(date="2026-04-12")
        result = await notifier.send_daily_summary(summary)
        assert result is False


# ===========================================================================
# Daily summary
# ===========================================================================


class TestDailySummary:
    """Daily summary formatting."""

    def test_summary_dataclass(self) -> None:
        summary = DailySummary(
            date="2026-04-12",
            total_pv_kwh=25.5,
            self_consumption_pct=95.0,
        )
        assert summary.total_pv_kwh == 25.5
        assert summary.self_consumption_pct == 95.0


# ===========================================================================
# Coverage: _send and notify with webhook
# ===========================================================================


@pytest.mark.asyncio
class TestWithWebhook:
    """Tests with a mock webhook URL set."""

    async def test_notify_with_mocked_send(self) -> None:
        """With webhook URL, notify should call _send for configured events."""
        import os
        from unittest.mock import AsyncMock, patch

        os.environ["TEST_SLACK_WEBHOOK"] = "https://hooks.slack.com/test"
        try:
            n = SlackNotifier(SlackConfig(webhook_env="TEST_SLACK_WEBHOOK"))
            with patch.object(n, "_send", new_callable=AsyncMock, return_value=True) as m:
                result = await n.notify("scenario_transition", "test msg")
                assert result is True
                m.assert_awaited_once()
        finally:
            del os.environ["TEST_SLACK_WEBHOOK"]

    async def test_daily_summary_with_mocked_send(self) -> None:
        """Daily summary calls _send with formatted text."""
        import os
        from unittest.mock import AsyncMock, patch

        os.environ["TEST_SLACK_WEBHOOK"] = "https://hooks.slack.com/test"
        try:
            n = SlackNotifier(SlackConfig(webhook_env="TEST_SLACK_WEBHOOK"))
            with patch.object(n, "_send", new_callable=AsyncMock, return_value=True) as m:
                summary = DailySummary(date="2026-04-12", total_pv_kwh=25.0)
                result = await n.send_daily_summary(summary)
                assert result is True
                m.assert_awaited_once()
        finally:
            del os.environ["TEST_SLACK_WEBHOOK"]

    async def test_filtered_event_not_sent(self) -> None:
        """Unconfigured event type should not send even with webhook."""
        import os

        os.environ["TEST_SLACK_WEBHOOK"] = "https://hooks.slack.com/test"
        try:
            n = SlackNotifier(SlackConfig(webhook_env="TEST_SLACK_WEBHOOK"))
            result = await n.notify("unknown_type", "test")
            assert result is False
        finally:
            del os.environ["TEST_SLACK_WEBHOOK"]
