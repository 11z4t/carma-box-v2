"""Tests for PLAT-1550 per-battery mode dispatch — legacy Branch B path.

The historical bug (engine sending mode change to a generic 'scenario'
target instead of iterating snapshot.batteries) lived in the
``_compute_charge_plan`` / BatteryBalancer path that was removed by
PLAT-1696. Budget Allocator now iterates batteries natively in
``core/budget.allocate``; see ``tests/unit/test_budget.py::
test_plat1714_standby_emits_limit_zero`` and
``tests/unit/test_zero_grid.py::test_two_bats_*`` for equivalent
coverage.
"""

from __future__ import annotations

import pytest

pytest.skip(
    "PLAT-1696: legacy per-battery dispatch path removed — equivalent "
    "coverage in tests/unit/test_budget.py + test_zero_grid.py",
    allow_module_level=True,
)
