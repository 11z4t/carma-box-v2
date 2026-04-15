"""CI gate configuration tests (PLAT-1596).

Verifies pyproject.toml enforces coverage, strict mypy, complexity budget,
and domain import layering.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

# ---------------------------------------------------------------------------
# Named test constants
# ---------------------------------------------------------------------------
_MIN_COVERAGE_PCT: int = 95
_MAX_COMPLEXITY: int = 25
_PYPROJECT_PATH: Path = Path(__file__).resolve().parents[2] / "pyproject.toml"
_CORE_DIR: Path = Path(__file__).resolve().parents[2] / "core"


def _load_pyproject() -> dict[str, object]:
    with open(_PYPROJECT_PATH, "rb") as f:
        return tomllib.load(f)


# ===========================================================================
# T1: Coverage threshold enforced
# ===========================================================================


class TestCoverageThreshold:
    """T1: pyproject.toml has coverage fail_under >= 95."""

    def test_ci_enforces_coverage_threshold(self) -> None:
        cfg = _load_pyproject()
        coverage = cfg.get("tool", {}).get("coverage", {}).get("report", {})  # type: ignore[union-attr]
        fail_under = coverage.get("fail_under", 0)  # type: ignore[union-attr]
        assert fail_under >= _MIN_COVERAGE_PCT, (
            f"Coverage fail_under={fail_under}, expected >= {_MIN_COVERAGE_PCT}"
        )


# ===========================================================================
# T2: Strict mypy enforced
# ===========================================================================


class TestStrictMypy:
    """T2: [tool.mypy] strict = true."""

    def test_ci_enforces_strict_mypy_all_modules(self) -> None:
        cfg = _load_pyproject()
        mypy = cfg.get("tool", {}).get("mypy", {})  # type: ignore[union-attr]
        assert mypy.get("strict") is True, (  # type: ignore[union-attr]
            "mypy strict mode not enabled in pyproject.toml"
        )


# ===========================================================================
# T3: Domain import layering
# ===========================================================================


class TestImportLayerDomainNoAdapters:
    """T3: core/ must not import from adapters/."""

    def test_import_layer_domain_no_adapters(self) -> None:
        violations: list[str] = []
        for py_file in _CORE_DIR.glob("**/*.py"):
            for i, line in enumerate(py_file.read_text().splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # ha_api is an interface boundary — allowed in core/
                if "ha_api" in stripped:
                    continue
                if "from adapters" in stripped or "import adapters" in stripped:
                    violations.append(f"{py_file.name}:{i}: {stripped}")

        assert not violations, (
            f"core/ imports adapters/ (layer violation): {violations}"
        )


# ===========================================================================
# T4: Complexity budget
# ===========================================================================


class TestComplexityBudget:
    """T4: [tool.ruff.lint.mccabe] max-complexity defined and <= budget."""

    def test_ci_complexity_budget_enforced(self) -> None:
        cfg = _load_pyproject()
        mccabe = (
            cfg.get("tool", {})  # type: ignore[union-attr]
            .get("ruff", {})  # type: ignore[union-attr]
            .get("lint", {})  # type: ignore[union-attr]
            .get("mccabe", {})  # type: ignore[union-attr]
        )
        max_c = mccabe.get("max-complexity")  # type: ignore[union-attr]
        assert max_c is not None, "max-complexity not defined"
        assert max_c <= _MAX_COMPLEXITY, (
            f"max-complexity={max_c}, expected <= {_MAX_COMPLEXITY}"
        )
