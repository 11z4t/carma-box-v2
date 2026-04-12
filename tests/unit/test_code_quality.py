"""Code quality tests — catches recurring QC reject patterns.

These tests encode LESSONS LEARNED from QC rejects by 901.
Each test corresponds to a specific reject pattern that must never recur.

Run: python3 -m pytest tests/unit/test_code_quality.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIRS = [PROJECT_ROOT / "core", PROJECT_ROOT / "adapters", PROJECT_ROOT / "config"]
TEST_DIR = PROJECT_ROOT / "tests"


def _all_py_files(dirs: list[Path]) -> list[Path]:
    """Collect all .py files from given directories."""
    files: list[Path] = []
    for d in dirs:
        files.extend(d.rglob("*.py"))
    return [f for f in files if f.name != "__init__.py"]


def _all_test_files() -> list[Path]:
    return list(TEST_DIR.rglob("test_*.py"))


# ===========================================================================
# QC Reject #1: Unused imports (ruff F401)
# ===========================================================================


class TestNoUnusedImports:
    """Every QC reject included unused imports. Verify ruff catches them."""

    def test_ruff_clean(self) -> None:
        """ruff check should pass with 0 errors."""
        import subprocess

        result = subprocess.run(
            ["python3", "-m", "ruff", "check", "."],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0, f"ruff errors:\n{result.stdout}"


# ===========================================================================
# QC Reject #2: Global pytestmark asyncio on sync tests
# ===========================================================================


class TestNoPytestmarkAsyncio:
    """Global pytestmark = pytest.mark.asyncio is FORBIDDEN.
    Use per-class @pytest.mark.asyncio instead.
    Reject history: Story 03, 04, 11."""

    def test_no_global_pytestmark_asyncio(self) -> None:
        for f in _all_test_files():
            content = f.read_text()
            matches = re.findall(r"^pytestmark\s*=\s*pytest\.mark\.asyncio", content, re.MULTILINE)
            assert not matches, (
                f"{f.relative_to(PROJECT_ROOT)}: "
                f"Global pytestmark asyncio found. Use per-class @pytest.mark.asyncio."
            )


# ===========================================================================
# QC Reject #3: Hardcoded magic numbers in logic code
# ===========================================================================


class TestNoHardcodedFloors:
    """SoC floors, SoH thresholds must come from config, not literals.
    Reject history: Story 04 B3."""

    def test_no_hardcoded_soc_floors_in_logic(self) -> None:
        """Logic code must not have 'floor = 15.0' etc — use config."""
        for f in _all_py_files(SRC_DIRS):
            content = f.read_text()
            # Match: floor = 15.0 / floor = 20.0 / floor = 25.0
            # But NOT in dataclass field defaults (Config classes)
            for line_no, line in enumerate(content.split("\n"), 1):
                if re.search(r"floor\s*=\s*(15|20|25)\.0", line):
                    # Allow in Config dataclass defaults (branch only hit if
                    # a config file uses floor= assignment — not exercised in tests)
                    in_config = (  # pragma: no cover
                        "Config" in line
                        or "default=" in line
                        or "Field(" in line
                    )
                    if in_config:  # pragma: no cover
                        continue
                    pytest.fail(  # pragma: no cover
                        f"{f.relative_to(PROJECT_ROOT)}:{line_no}: "
                        f"Hardcoded SoC floor: {line.strip()}"
                    )


# ===========================================================================
# QC Reject #4: G3 check order (CRITICAL vs BREACH)
# ===========================================================================


class TestG3CheckOrder:
    """G3 Ellevio: CRITICAL threshold (>tak*1.10) must be checked BEFORE
    BREACH (>tak). Otherwise CRITICAL is dead code.
    Reject history: Story 04 BUG-G3-01."""

    def test_critical_before_breach_in_guards(self) -> None:
        guards_file = PROJECT_ROOT / "core" / "guards.py"
        content = guards_file.read_text()

        # In the elif chain, critical check should come FIRST
        critical_if = content.find("weighted_avg_kw > critical_threshold")
        breach_if = content.find("weighted_avg_kw > effective_tak")

        assert critical_if < breach_if, (
            "G3 check order wrong: critical_threshold must be checked "
            "BEFORE effective_tak (otherwise CRITICAL is dead code)"
        )


# ===========================================================================
# QC Reject #5: Entity IDs must exist in HA
# ===========================================================================


class TestEntityIdsInConfig:
    """Entity IDs in site.yaml should follow HA naming conventions.
    Reject history: Story 01 — 16 fabricated entity IDs."""

    def test_no_fabricated_goodwe_entity_patterns(self) -> None:
        """Known wrong patterns from Story 01 reject."""
        site_yaml = PROJECT_ROOT / "config" / "site.yaml"
        content = site_yaml.read_text()

        # These patterns were fabricated and don't exist in HA
        fabricated_patterns = [
            "sensor.goodwe_kontor_battery_temperature",
            "sensor.goodwe_kontor_load_power",
            "sensor.goodwe_kontor_battery_soh",
            "sensor.goodwe_forrad_battery_temperature",
            "sensor.goodwe_forrad_pv_power",
            "sensor.goodwe_forrad_grid_power",
            "sensor.goodwe_forrad_load_power",
            "sensor.goodwe_forrad_battery_soh",
            "sensor.ellevio_aktuell_topp",
            "sensor.shellypro1pm_vp_kontor_power",
            "switch.vp_pool",
            "sensor.vp_pool_power",
            "switch.pool_heater",
            "sensor.pool_heater_power",
        ]
        for pattern in fabricated_patterns:
            assert pattern not in content, (
                f"Fabricated entity ID found in site.yaml: {pattern}"
            )


# ===========================================================================
# General: mypy strict
# ===========================================================================


class TestMypyStrict:
    """mypy --strict must pass with 0 errors."""

    def test_mypy_clean(self) -> None:
        import subprocess

        result = subprocess.run(
            ["python3", "-m", "mypy", "--strict", "config/", "core/", "adapters/", "main.py"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0, f"mypy errors:\n{result.stdout}"
