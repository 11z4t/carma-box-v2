"""REGRESSION: never grid-charge bat or EV during daytime — Branch B version.

The invariant itself ("no grid → bat during day") still holds and is
enforced by the Budget Allocator via ``core/zero_grid.py``: the surplus
model floors at 0 during daytime and the physical PV ceiling caps the
bat target at what is actually available from panels. Regression
coverage lives in:

  - ``tests/unit/test_budget.py::test_plat1714_bat_charge_uses_charge_battery_not_charge_pv``
  - ``tests/unit/test_budget.py::test_plat1695_grid_w_variation[...]``
  - ``tests/unit/test_zero_grid.py::test_grid_import_reduces_charge``

These three tests assert the new (post-PLAT-1696) behaviour: at any
level of grid import, the bat allocation shrinks (or flips to
discharge) within the same cycle.

This module is kept so the PLAT number is grep-able and the invariant
text stays in the repo.
"""

from __future__ import annotations

import pytest

pytest.skip(
    "PLAT-1696: Branch B path removed — invariant covered by "
    "test_plat1714_bat_charge_uses_charge_battery_not_charge_pv and "
    "test_plat1695_grid_w_variation in test_budget.py + test_zero_grid.py",
    allow_module_level=True,
)
