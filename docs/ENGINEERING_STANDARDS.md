# CARMA Box v2 — Engineering Standards

Living document defining code quality norms enforced across all VMs.
Every PR, commit, and QC review references these standards.

Last updated: 2026-04-16 (Sprint 1, PLAT-1604).

---

## 1. No Magic Numbers

### Rule

Every numeric literal with semantic meaning MUST be a named constant
or config field. Inline numbers in business logic are REJECTED without
discussion.

### Why

Magic numbers hide intent, resist refactoring, and cause silent bugs
when the same value appears in multiple places. The CARMA Box codebase
has had **5+ QC rejects** caused by inline 80.0, 230, 999.0, 0.001,
and 1000 literals that should have been config fields.

### What counts as a magic number

- Thresholds: `if soc > 80.0` — the 80.0 has meaning ("weekend skip SoC")
- Conversion factors: `* 1000` — kW→W conversion
- Sentinel values: `prices.get(h, 999.0)` — "unknown price" fallback
- Timing: `sleep(30)` — cycle interval

### What does NOT count

- Dataclass defaults in `field(default=0.0)` — these are zero-init
- Mathematical constants: `/ 100.0` for percentage conversion (ubiquitous)
- Boolean-like: `0`, `1`, `-1` when meaning is obvious from context
- Test inputs: `make_battery_state(soc_pct=60.0)` — test fixture values

### How to fix

1. **Config field** (preferred for tunable values):
   ```python
   # In PlannerConfig:
   ev_weekend_skip_soc_pct: float = 80.0

   # In planner.py:
   if ev_soc_pct > cfg.ev_weekend_skip_soc_pct:
   ```

2. **Module constant** (for conversion factors and sentinels):
   ```python
   _W_TO_KW: float = 1000.0
   _PRICE_SORT_SENTINEL_ORE: float = 999.0
   NEAR_ZERO_KW: float = 0.05
   ```

### REJECT example

```python
# REJECTED — what does 80.0 mean?
if ev_soc_pct > 80.0:
    ev_skip = True
```

### Correct example

```python
# ACCEPTED — named, documented, configurable
if ev_soc_pct > cfg.ev_weekend_skip_soc_pct:
    ev_skip = True
```

### Guard tests

Every magic number fix MUST include a guard test that prevents
regression:

```python
class TestNoNaked1000InMain:
    def test_no_naked_1000(self) -> None:
        source = Path("main.py").read_text()
        for i, line in enumerate(source.splitlines(), 1):
            if "1000" in line and not line.strip().startswith("#"):
                if "_W_TO_KW" not in line and "_MS_PER_S" not in line:
                    assert False, f"Naked 1000 at line {i}"
```

---

## 2. LÄRDOM Commits

### Rule

When a QC REJECT identifies a systemic issue, the fix commit MUST
include a LÄRDOM (lesson learned) that documents:

1. **Root cause** — why the mistake happened
2. **Fix** — what was changed
3. **Guard test** — automated prevention of recurrence

### When required

- First REJECT on a story → LÄRDOM commit mandatory
- Recurring pattern (same class of error across stories) → LÄRDOM escalation

### Commit format

```
PLAT-XXXX: LÄRDOM [category]: description

Root cause: <why the mistake happened>
Fix: <what was changed>
Guard: <test that prevents recurrence>
```

### Categories

| Category | Example |
|----------|---------|
| `magic-numbers` | Inline 80.0 instead of config field |
| `hardcoded-ids` | battery_id="scenario" instead of per-battery loop |
| `broad-exception` | `except Exception` instead of specific types |
| `missing-test` | No guard test for the constraint |
| `config-drift` | Value in code differs from site.yaml |

### Example

```
PLAT-1558: LÄRDOM [magic-numbers-ESKALERING]: fix ALL 7 naked numbers

Root cause: thresholds written inline without checking if a config
field exists. Pattern: developer writes logic before scanning config.
Fix: extracted 7 values to PlannerConfig + config/schema.py.
Guard: TestNoNakedNumbers scans all .py files for common patterns.
```

---

## 3. Guard Tests

### Rule

Every structural constraint (no magic numbers, no hardcoded IDs, no
broad exceptions) MUST have an automated guard test that reads the
source code and verifies the constraint.

### Why

Code review catches ~80% of violations. Guard tests catch 100% and
prevent regression. A guard test runs every CI cycle and will fail
the moment someone re-introduces a naked literal.

### Pattern

Guard tests read source files as text and scan for forbidden patterns:

```python
class TestNoHardcodedScenarioString:
    def test_no_scenario_string_as_battery_id(self) -> None:
        source = Path("core/engine.py").read_text()
        assert 'battery_id="scenario"' not in source
        assert "battery_id='scenario'" not in source
```

### Guard test naming

- `test_no_naked_1000_in_engine` — scans engine.py for `* 1000`
- `test_no_naked_005_in_engine` — scans for inline NEAR_ZERO_KW
- `test_no_hardcoded_scenario_battery_id` — scans for `battery_id="scenario"`
- `test_freeze_check_inside_try_except` — verifies structural ordering

### Where to place

Guard tests go in the unit test file for the module they protect:
- `test_engine.py` for engine.py guards
- `test_main.py` for main.py guards
- `test_planner.py` for planner.py guards

---

## 4. QC Process

### How QC works

1. **Developer** commits code to master on Gitea
2. **Developer** sends QC request to `inbox-901/QC-PLATXXXX-DATE.json`
3. **Storm (VM-901)** pulls code, runs full test suite, verifies ACs
4. **Storm** sends verdict to `inbox-900/` — PASS or REJECT
5. On REJECT: developer fixes, creates LÄRDOM commit if first reject

### QC request format

```json
{
  "from": "VM-900 (Phoenix)",
  "to": "VM-901 (Storm)",
  "subject": "QC REQUEST: PLAT-XXXX — title",
  "timestamp": "ISO-8601",
  "commit": "short-hash",
  "repo": "carma-box-v2",
  "ac_status": {
    "AC1": "DONE — description",
    "AC2": "DONE — description"
  },
  "bevis": {
    "full_suite": "849/849 PASS",
    "ruff": "0 violations",
    "mypy_strict": "0 errors"
  }
}
```

### Definition of Done (DoD) — standard

Every story must meet ALL of these before QC submission:

- [ ] All acceptance criteria implemented
- [ ] `python3 -m pytest tests/ -q` → all PASS
- [ ] `ruff check .` → 0 violations
- [ ] `mypy --strict core/ main.py` → 0 errors
- [ ] No magic numbers in diff
- [ ] Guard tests for structural constraints
- [ ] LÄRDOM commit if this is a re-reject

### What Storm verifies

1. **grep-bevis** — specific patterns absent/present
2. **Full test suite** — all tests pass (including new ones)
3. **Ruff + mypy** — zero violations/errors
4. **Isolation test** — new tests pass in isolation
5. **No regression** — existing test count preserved or increased
6. **No magic numbers** — diff contains no naked numeric literals

---

## 5. Constant Naming

### Module-private constants

Use `_UPPER_SNAKE_CASE` for module-private constants:

```python
# core/engine.py
_W_TO_KW: float = 1000.0
_MS_PER_S: int = 1000
NEAR_ZERO_KW: float = 0.05  # Public — used in tests

# core/planner.py
_WATTS_PER_KW: int = 1000
_PRICE_SORT_SENTINEL_ORE: float = 999.0
```

### Config fields

Use `lower_snake_case` in dataclass config fields:

```python
@dataclass(frozen=True)
class PlannerConfig:
    ev_weekend_skip_soc_pct: float = 80.0
    grid_voltage_v: float = 230.0
    pv_replan_threshold: float = 0.2
```

### Enum values

Use `UPPER_SNAKE_CASE` for enum members:

```python
class EMSMode(str, Enum):
    CHARGE_PV = "charge_pv"
    DISCHARGE_PV = "discharge_pv"
    BATTERY_STANDBY = "battery_standby"
```

### Constants per file vs shared module

Constants are defined **per file**, not in a shared constants module.
This keeps dependencies local and avoids circular imports:

- `engine.py` has its own `_W_TO_KW`
- `models.py` has its own `_W_TO_KW`
- `plan_executor.py` has its own `_W_TO_KW`

This is intentional — the alternative (shared `constants.py`) creates
tight coupling between unrelated modules.

---

## 6. Exception Handling

### Rule

Never use bare `except Exception`. Always catch specific exception
types with `exc_info=True` for full stack traces.

### Pattern

```python
# REJECTED
try:
    await do_something()
except Exception as exc:
    logger.error("Failed: %s", exc)

# ACCEPTED
try:
    await do_something()
except (asyncio.TimeoutError, OSError) as exc:
    logger.error("I/O failure: %s", exc, exc_info=True)
except (ValueError, KeyError) as exc:
    logger.error("Data error: %s", exc, exc_info=True)
```

### Why

- Broad `except Exception` silently swallows programming errors
- `exc_info=True` gives full stack trace for post-mortem debugging
- Specific types allow different recovery strategies

---

## 7. Per-Battery Operations

### Rule

All inverter commands MUST iterate `snapshot.batteries` and address
each battery by its `battery_id`. Never use generic IDs like
`"scenario"` or `"all"`.

### Pattern

```python
# REJECTED
self._mode_manager.request_change(
    battery_id="scenario",  # What battery is "scenario"?
    target_mode=mode,
)

# ACCEPTED
for bat in snapshot.batteries:
    self._mode_manager.request_change(
        battery_id=bat.battery_id,
        target_mode=mode,
        reason=f"Scenario {scenario.value}",
    )
```

### Why

The system has multiple batteries (Kontor 15kWh + Förråd 5kWh) with
different SoC levels, different CT placements, and different capacity.
A generic ID silently drops commands for all but one battery.

---

## 8. Adapter Contracts

### Rule

All hardware adapters MUST implement their ABC interface completely.
Contract tests verify this at CI time.

### Pattern

```python
class TestInverterAdapterContract:
    def test_implements_interface(self) -> None:
        assert issubclass(GoodWeAdapter, InverterAdapter)

    def test_all_methods_present(self) -> None:
        for name, method in inspect.getmembers(InverterAdapter):
            if getattr(method, "__isabstractmethod__", False):
                assert hasattr(GoodWeAdapter, name)
```

---

## 9. Pre-Commit Checklist

Before every commit, verify:

```bash
# 1. Tests
python3 -m pytest tests/ -q

# 2. Lint
ruff check .

# 3. Type check
mypy --strict core/ main.py

# 4. Magic number scan
grep -rn '\b1000\b' core/ main.py | grep -v '#\|_W_TO_KW\|_MS_PER_S'
# Must return empty
```

All four MUST pass before pushing. No exceptions.
