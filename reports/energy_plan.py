"""Energy Plan Excel Report Generator for CARMA Box.

Generates 48h energy plan Excel report with xlsxwriter (ALDRIG openpyxl).
3 tabs: Energiplan | Utfall | Prognos.

Runs at 22:00 (after night plan) and 06:00 (morning summary).
Graceful failure: errors do not crash service.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlanRow:
    """One hour in the energy plan."""

    hour: int
    scenario: str = ""
    bat_target_soc: float = 0.0
    ev_target_soc: float = 0.0
    grid_limit_kw: float = 0.0
    pv_forecast_kwh: float = 0.0
    price_ore: float = 0.0


@dataclass(frozen=True)
class ActualRow:
    """One hour of actual data."""

    hour: int
    grid_kw: float = 0.0
    pv_kw: float = 0.0
    bat_soc: float = 0.0
    ev_soc: float = 0.0
    scenario: str = ""


@dataclass(frozen=True)
class ReportData:
    """Input data for report generation."""

    plan: list[PlanRow] = field(default_factory=list)
    actuals: list[ActualRow] = field(default_factory=list)
    pv_forecast: list[float] = field(default_factory=list)
    prices: list[float] = field(default_factory=list)
    date: str = ""
    site_name: str = "Sanduddsvagen 60"


class ExcelReportGenerator:
    """Generates energy plan Excel report using xlsxwriter.

    NEVER uses openpyxl (produces broken files in this system).
    """

    def generate(self, data: ReportData, output_dir: Path) -> Path | None:
        """Generate Excel report. Returns path or None on failure.

        Never raises — errors caught and logged.
        """
        try:
            import xlsxwriter
        except ImportError:
            logger.error("xlsxwriter not installed — cannot generate report")
            return None

        output_path = output_dir / f"energy-plan-{data.date}.xlsx"

        try:
            wb = xlsxwriter.Workbook(str(output_path))
            self._write_plan_sheet(wb, data)
            self._write_utfall_sheet(wb, data)
            self._write_forecast_sheet(wb, data)
            wb.close()
            logger.info("Report generated: %s", output_path)
            return output_path
        except Exception as exc:
            logger.error("Report generation failed: %s", exc)
            return None

    def _write_plan_sheet(self, wb: object, data: ReportData) -> None:
        """Write Energiplan tab."""
        ws = wb.add_worksheet("Energiplan")  # type: ignore[attr-defined]
        headers = ["Timme", "Scenario", "Bat SoC %", "EV SoC %", "Grid kW", "PV kWh", "Pris öre"]
        for col, h in enumerate(headers):
            ws.write(0, col, h)
        for i, row in enumerate(data.plan, 1):
            ws.write(i, 0, row.hour)
            ws.write(i, 1, row.scenario)
            ws.write(i, 2, row.bat_target_soc)
            ws.write(i, 3, row.ev_target_soc)
            ws.write(i, 4, row.grid_limit_kw)
            ws.write(i, 5, row.pv_forecast_kwh)
            ws.write(i, 6, row.price_ore)

    def _write_utfall_sheet(self, wb: object, data: ReportData) -> None:
        """Write Utfall tab."""
        ws = wb.add_worksheet("Utfall")  # type: ignore[attr-defined]
        headers = ["Timme", "Grid kW", "PV kW", "Bat SoC %", "EV SoC %", "Scenario"]
        for col, h in enumerate(headers):
            ws.write(0, col, h)
        for i, row in enumerate(data.actuals, 1):
            ws.write(i, 0, row.hour)
            ws.write(i, 1, row.grid_kw)
            ws.write(i, 2, row.pv_kw)
            ws.write(i, 3, row.bat_soc)
            ws.write(i, 4, row.ev_soc)
            ws.write(i, 5, row.scenario)

    def _write_forecast_sheet(self, wb: object, data: ReportData) -> None:
        """Write Prognos tab."""
        ws = wb.add_worksheet("Prognos")  # type: ignore[attr-defined]
        ws.write(0, 0, "Timme")
        ws.write(0, 1, "PV Prognos kWh")
        ws.write(0, 2, "Pris öre")
        for i, (pv, price) in enumerate(zip(data.pv_forecast, data.prices), 1):
            ws.write(i, 0, i - 1)
            ws.write(i, 1, pv)
            ws.write(i, 2, price)
