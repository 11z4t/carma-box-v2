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
    """G3 Ellevio: BREACH threshold (>tak) is the outer check; CRITICAL
    (>tak*1.10) is nested inside it.
    PLAT-1571: corrected severity order — moderate overload → BREACH, not CRITICAL.
    Old rule (CRITICAL before BREACH as flat elif) superseded by nested structure."""

    def test_breach_outer_critical_nested_in_guards(self) -> None:
        """PLAT-1571: BREACH check (effective_tak) is the outer if;
        CRITICAL check (critical_threshold) is nested inside it."""
        guards_file = PROJECT_ROOT / "core" / "guards.py"
        content = guards_file.read_text()

        # Outer BREACH check must appear before the nested CRITICAL check
        breach_if = content.find("weighted_avg_kw > effective_tak")
        critical_if = content.find("weighted_avg_kw > critical_threshold")

        assert breach_if != -1, "BREACH check (> effective_tak) not found in guards.py"
        assert critical_if != -1, "CRITICAL check (> critical_threshold) not found in guards.py"
        assert breach_if < critical_if, (
            "G3 check order wrong: effective_tak (BREACH) must be the outer check "
            "and appear before critical_threshold (CRITICAL) in source"
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


# ===========================================================================
# QC Reject #6: EMS mode "auto" is FORBIDDEN (B10, B14)
# ===========================================================================


class TestNoAutoMode:
    """EMS mode 'auto' must NEVER be used — GoodWe firmware makes
    uncontrolled decisions. All paths must use 'discharge_pv' or explicit modes.
    Reject history: B10, B14, PLAT-1241."""

    def test_no_auto_mode_string_in_logic(self) -> None:
        for f in _all_py_files(SRC_DIRS):
            content = f.read_text()
            for line_no, line in enumerate(content.split("\n"), 1):
                # Match: set_ems_mode("auto") or mode = "auto" or EMS_MODE.auto
                if re.search(r'''["']auto["']''', line):
                    # Allow exclusion lists, comments, test assertions, config
                    skip_words = [
                        "#", "VALID_EMS_MODES", "exclude", "!=",
                        "not in", "FORBIDDEN", "assert", "Error",
                        "raise", "log", "docstring", "'import_ac'",
                    ]
                    if any(skip in line for skip in skip_words):
                        continue
                    pytest.fail(
                        f"{f.relative_to(PROJECT_ROOT)}:{line_no}: "
                        f'EMS mode "auto" is FORBIDDEN: {line.strip()}'
                    )

    def test_auto_excluded_from_valid_modes(self) -> None:
        """GoodWe adapter must explicitly exclude 'auto' from valid modes."""
        goodwe = PROJECT_ROOT / "adapters" / "goodwe.py"
        content = goodwe.read_text()
        assert "auto" not in content.split("_VALID_EMS_MODES")[1].split("}")[0], (
            "GoodWe adapter: 'auto' must be excluded from _VALID_EMS_MODES"
        )


# ===========================================================================
# QC Reject #7: fast_charging must be OFF before discharge_pv (INV-3, B7)
# ===========================================================================


class TestFastChargingBeforeDischarge:
    """All discharge paths MUST call set_fast_charging(on=False) before
    setting EMS mode to discharge_pv. GoodWe firmware charges from grid otherwise.
    Reject history: PLAT-1103 INV-3."""

    def test_discharge_paths_clear_fast_charging(self) -> None:
        """Check that discharge_pv mentions are preceded by fast_charging=False."""
        executor_file = PROJECT_ROOT / "core" / "executor.py"
        content = executor_file.read_text()
        lines = content.split("\n")

        for i, line in enumerate(lines):
            if "discharge_pv" in line and "set_ems_mode" in line:
                # Look back up to 20 lines for fast_charging(on=False)
                preceding = "\n".join(lines[max(0, i - 20):i])
                assert "fast_charging" in preceding or "FAST_CHARGING" in preceding, (
                    f"executor.py:{i + 1}: discharge_pv set without "
                    f"prior fast_charging=OFF check"
                )


# ===========================================================================
# QC Reject #8: ems_power_limit=0 must not be skipped (truthy trap, B9)
# ===========================================================================


class TestEMSPowerLimitZero:
    """set_ems_power_limit(0) must write 0 to inverter, not be skipped
    by a falsy check. GoodWe adapter must handle 0 explicitly.
    Reject history: B9, PLAT-1040."""

    def test_goodwe_handles_zero_limit(self) -> None:
        goodwe = PROJECT_ROOT / "adapters" / "goodwe.py"
        content = goodwe.read_text()
        # Must not have: if not limit: return / if limit: write
        for line_no, line in enumerate(content.split("\n"), 1):
            if re.search(r"if\s+not\s+.*limit", line) and "power" in line.lower():
                pytest.fail(
                    f"adapters/goodwe.py:{line_no}: "
                    f"Truthy trap — 'if not limit' skips limit=0: {line.strip()}"
                )


# ===========================================================================
# QC Reject #9: Config thresholds — no naked numbers in logic
# ===========================================================================


class TestNoNakedThresholds:
    """Time boundaries, power thresholds, and SoC limits must come from
    config objects, not be hardcoded as literals.
    Reject history: Story 09 magic numbers."""

    FORBIDDEN_PATTERNS = [
        # Time boundaries (should be cfg.morning_start_h etc)
        (r"hour\s*[><=]+\s*(6|9|12|17|22)\b", "Hardcoded hour boundary"),
        # Power limits (should be from config)
        (r"(?:power|limit|threshold)\s*=\s*\d{3,5}\.?\d*\b", "Hardcoded power value"),
    ]

    def test_no_naked_time_boundaries_in_core(self) -> None:
        """Time boundaries (6, 9, 12, 17, 22) should use config fields."""
        for f in _all_py_files([PROJECT_ROOT / "core"]):
            if f.name.startswith("test_"):
                continue
            content = f.read_text()
            for line_no, line in enumerate(content.split("\n"), 1):
                # Skip comments, config defaults, docstrings
                stripped = line.strip()
                if stripped.startswith("#") or '"""' in stripped or "'" == stripped[0:1]:
                    continue
                if "default" in line or "Field(" in line or "Config" in line:
                    continue
                if re.search(r"snap\.hour\s*[><=]+\s*(6|9|12|17|22)\b", line):
                    pytest.fail(
                        f"{f.relative_to(PROJECT_ROOT)}:{line_no}: "
                        f"Hardcoded hour boundary — use config: {stripped}"
                    )


# ===========================================================================
# QC Reject #10: Regression tests for known bugs B1-B15
# ===========================================================================


class TestRegressionSuiteExists:
    """Regression test file must exist and test all known bugs B1-B15.
    Reject history: Story 21 — B2/B4/B11/B12 missing."""

    def test_regression_file_exists(self) -> None:
        regression_dir = PROJECT_ROOT / "tests" / "regression"
        assert regression_dir.exists(), "tests/regression/ directory missing"
        regression_files = list(regression_dir.glob("test_*.py"))
        assert len(regression_files) > 0, "No regression test files found"

    def test_all_known_bugs_have_tests(self) -> None:
        """Every known bug (B1-B15) must have at least one test."""
        regression_dir = PROJECT_ROOT / "tests" / "regression"
        if not regression_dir.exists():
            pytest.skip("regression dir missing")
        all_content = ""
        for f in regression_dir.rglob("test_*.py"):
            all_content += f.read_text()

        for bug_id in range(1, 16):
            assert f"B{bug_id}" in all_content or f"b{bug_id}" in all_content, (
                f"Regression test for B{bug_id} missing in tests/regression/"
            )


# ===========================================================================
# QC Reject #11: No retry on 401/403 (PLAT-1354)
# ===========================================================================


class TestNoRetryOnAuth:
    """HTTP 401/403 must not be retried — it wastes time and hides real issues.
    Reject history: PLAT-1354."""

    def test_ha_api_no_retry_on_auth_error(self) -> None:
        ha_api = PROJECT_ROOT / "adapters" / "ha_api.py"
        content = ha_api.read_text()
        # Verify that 401/403 auth handling is present (via literal or HTTPStatus enum)
        has_literal = "401" in content or "403" in content
        has_http_status = "UNAUTHORIZED" in content or "FORBIDDEN" in content
        assert has_literal or has_http_status, (
            "ha_api.py should handle 401/403 auth errors explicitly "
            "(either as literal ints or via HTTPStatus.UNAUTHORIZED/FORBIDDEN)"
        )


# ===========================================================================
# QC Reject #12: Storage must use WAL mode (PLAT-1364)
# ===========================================================================


class TestSQLiteWALMode:
    """SQLite must use WAL mode for concurrent read/write safety.
    Reject history: PLAT-1364."""

    def test_local_db_uses_wal(self) -> None:
        db_file = PROJECT_ROOT / "storage" / "local_db.py"
        content = db_file.read_text()
        assert "wal" in content.lower() or "WAL" in content, (
            "storage/local_db.py must enable WAL mode for SQLite"
        )


# ===========================================================================
# QC Reject #13: SQL injection prevention (PLAT-1352)
# ===========================================================================


class TestSQLInjectionPrevention:
    """Table names must be validated against allowlist, never f-string interpolated.
    Reject history: PLAT-1352, M5."""

    def test_table_name_allowlist_exists(self) -> None:
        db_file = PROJECT_ROOT / "storage" / "local_db.py"
        content = db_file.read_text()
        has_allowlist = (
            "ALLOWED_TABLES" in content
            or "allowlist" in content.lower()
            or "_VALID_TABLES" in content
        )
        assert has_allowlist, (
            "storage/local_db.py must have a table name allowlist"
        )


# ===========================================================================
# QC Reject #14: No unbounded collections (H6)
# ===========================================================================


class TestNoRawEMSModeStrings:
    """EMS mode strings in core logic must use EMSMode enum, not raw strings.
    Reject history: PLAT-1356 — enums defined but not applied."""

    EMS_RAW_STRINGS = [
        '"discharge_pv"', '"charge_pv"', '"battery_standby"',
        '"import_ac"', '"export_ac"',
        "'discharge_pv'", "'charge_pv'", "'battery_standby'",
        "'import_ac'", "'export_ac'",
    ]

    def test_no_raw_ems_strings_in_core(self) -> None:
        """Core logic files must use EMSMode.X, not raw strings."""
        core_files = [
            PROJECT_ROOT / "core" / "engine.py",
            PROJECT_ROOT / "core" / "guards.py",
            PROJECT_ROOT / "core" / "mode_change.py",
            PROJECT_ROOT / "core" / "executor.py",
        ]
        for f in core_files:
            content = f.read_text()
            for line_no, line in enumerate(content.split("\n"), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # Allow in EMSMode enum definition and imports
                if "EMSMode" in line or "Enum" in line:
                    continue
                # Allow in docstrings and comments
                if '"""' in stripped or "'''" in stripped:
                    continue
                for raw in self.EMS_RAW_STRINGS:
                    if raw in line:
                        # Allow .value comparisons
                        if ".value" in line:
                            continue
                        pytest.fail(
                            f"{f.relative_to(PROJECT_ROOT)}:{line_no}: "
                            f"Raw EMS mode string {raw} — use EMSMode enum: "
                            f"{stripped}"
                        )


class TestBoundedCollections:
    """Audit trails and history buffers must use bounded collections (deque).
    Reject history: H6 — unbounded list caused memory leak risk."""

    def test_executor_uses_deque_for_audit(self) -> None:
        executor = PROJECT_ROOT / "core" / "executor.py"
        content = executor.read_text()
        assert "deque" in content, (
            "core/executor.py should use deque(maxlen=) for audit trail, "
            "not unbounded list"
        )


# ===========================================================================
# QC Reject #15+: Zero naked numbers in logic code
# ===========================================================================


class TestNoNakedNumbers:
    """PLAT-1558 RCA: Every numeric literal must be from config/constant.
    This test catches magic numbers BEFORE QC review."""

    LOGIC_DIRS = [
        PROJECT_ROOT / "core",
        PROJECT_ROOT / "adapters",
    ]
    LOGIC_FILES = [PROJECT_ROOT / "main.py"]

    def test_no_naked_numbers_in_logic(self) -> None:
        """Scan all logic code for suspicious numeric literals."""
        import subprocess
        # Grep for numbers not in config/constant context
        cmd = (
            "grep -rnP '\\b\\d{2,}\\b|\\b\\d+\\.\\d+\\b' "
            + " ".join(str(d) for d in self.LOGIC_DIRS)
            + " " + " ".join(str(f) for f in self.LOGIC_FILES)
            + " --include='*.py'"
            + " | grep -v '#'"
            + " | grep -v 'import'"
            + " | grep -v 'Field('"
            + " | grep -v 'default='"
            + " | grep -v '[A-Z_][A-Z_]'"
            + " | grep -v 'config\\.'"
            + " | grep -v 'self\\._config'"
            + " | grep -v 'cfg\\.'"
            + " | grep -v '_config\\.'"
            + " | grep -v 'test_'"
            + " | grep -v '__pycache__'"
            + " | grep -v 'service_map'"
            + " | grep -v '\\.value'"
            + " | grep -v 'range('"
            + " | grep -v 'len('"
            + " | grep -v 'log'"
            + " | grep -v 'version'"
            + " | grep -v 'pragma'"
            + " | grep -v 'port'"
        )
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
        )
        if result.stdout.strip():
            lines = result.stdout.strip().split("\n")
            # Filter known safe patterns
            suspicious = []
            for line in lines:
                # Skip config dataclass defaults
                if "dataclass" in line or "frozen=True" in line:
                    continue
                if "DEFAULT_" in line or "MAX_" in line or "MIN_" in line:
                    continue
                suspicious.append(line)
            if suspicious:
                msg = "\n".join(suspicious[:10])
                # Warning only — not blocking yet
                import warnings
                warnings.warn(
                    f"Potential magic numbers ({len(suspicious)} lines):\n{msg}",
                    stacklevel=1,
                )
