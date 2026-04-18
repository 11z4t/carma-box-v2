"""Tests for SoC balancing — legacy Branch B path (PLAT-1696 refactor).

The behaviour these tests cover has moved to:
  - ``core/zero_grid.py`` (pure function, see ``tests/unit/test_zero_grid.py``)
  - ``core/budget.py`` (integration, see ``tests/unit/test_budget.py``)

The engine no longer routes to ``_compute_charge_plan`` + BatteryBalancer,
so these tests exercise a code path that can't be reached any more. Kept
as a historical marker; individual cases are mirrored by PLAT-1695 /
PLAT-1708 / PLAT-1714 regressions in the budget test module.
"""

from __future__ import annotations

import pytest

pytest.skip(
    "PLAT-1696: legacy SoC-balance path replaced by zero_grid + budget — "
    "equivalent coverage in tests/unit/test_zero_grid.py + test_budget.py",
    allow_module_level=True,
)
