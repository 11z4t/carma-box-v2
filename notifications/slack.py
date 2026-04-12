"""Slack Notifications for CARMA Box.

Direct HTTPS webhook — works even when HA is down.
Webhook URL from environment variable (never hardcoded).
Errors in sending do not crash the service.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SlackConfig:
    """Slack notification config — from site.yaml."""

    webhook_env: str = "CARMA_SLACK_WEBHOOK"
    channel: str = "#energy"
    notify_on: tuple[str, ...] = (
        "scenario_transition",
        "ev_start_stop",
        "guard_trigger",
        "ellevio_breach",
        "communication_lost",
        "daily_summary",
    )


@dataclass(frozen=True)
class DailySummary:
    """Daily KPI summary for Slack report."""

    date: str
    total_pv_kwh: float = 0.0
    total_grid_import_kwh: float = 0.0
    total_grid_export_kwh: float = 0.0
    self_consumption_pct: float = 0.0
    max_grid_import_kw: float = 0.0
    ellevio_weighted_avg_kw: float = 0.0
    ev_charged_kwh: float = 0.0
    cycles_total: int = 0
    guard_triggers: int = 0


class SlackNotifier:
    """Sends notifications to Slack via webhook.

    Direct HTTPS — no HA dependency. All errors caught and logged.
    """

    def __init__(self, config: SlackConfig | None = None) -> None:
        self._config = config or SlackConfig()
        self._webhook_url = os.environ.get(self._config.webhook_env, "")
        if not self._webhook_url:
            logger.warning(
                "Slack webhook env %s is empty — notifications disabled",
                self._config.webhook_env,
            )

    def _should_notify(self, event_type: str) -> bool:
        """Check if this event type is configured for notification."""
        return event_type in self._config.notify_on

    async def notify(
        self, event_type: str, message: str, severity: str = "info"
    ) -> bool:
        """Send a notification to Slack.

        Returns True if sent successfully, False on error or filtered.
        """
        if not self._should_notify(event_type):
            return False

        if not self._webhook_url:
            logger.debug("Slack disabled — skipping %s", event_type)
            return False

        payload = self._format_message(event_type, message, severity)
        return await self._send(payload)

    async def send_daily_summary(self, summary: DailySummary) -> bool:
        """Send daily KPI summary at 22:00."""
        if not self._webhook_url:
            return False

        text = (
            f"*CARMA Box Daily Summary — {summary.date}*\n"
            f"☀️ PV: {summary.total_pv_kwh:.1f} kWh\n"
            f"⬇️ Import: {summary.total_grid_import_kwh:.1f} kWh\n"
            f"⬆️ Export: {summary.total_grid_export_kwh:.1f} kWh\n"
            f"🔋 Self-consumption: {summary.self_consumption_pct:.0f}%\n"
            f"⚡ Max import: {summary.max_grid_import_kw:.1f} kW\n"
            f"📊 Ellevio avg: {summary.ellevio_weighted_avg_kw:.2f} kW\n"
            f"🚗 EV charged: {summary.ev_charged_kwh:.1f} kWh\n"
            f"🔄 Cycles: {summary.cycles_total} | Guards: {summary.guard_triggers}"
        )
        payload = {"text": text}
        return await self._send(payload)

    def _format_message(
        self, event_type: str, message: str, severity: str
    ) -> dict[str, Any]:
        """Format a Slack message payload."""
        emoji = {
            "info": "ℹ️",
            "warning": "⚠️",
            "critical": "🚨",
            "breach": "🔴",
        }.get(severity, "ℹ️")

        return {
            "text": f"{emoji} *CARMA Box — {event_type}*\n{message}",
        }

    async def _send(self, payload: dict[str, Any]) -> bool:
        """Send payload to Slack webhook. Never raises."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._webhook_url,
                    data=json.dumps(payload),
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        return True
                    logger.warning(
                        "Slack webhook returned %d", resp.status,
                    )
                    return False
        except Exception as exc:
            logger.error("Slack send failed: %s", exc)
            return False
