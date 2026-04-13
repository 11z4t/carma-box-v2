# SPIKE-04: Teststrategi för säkerhetslogik med HA-beroenden

**Status:** Decided  
**Date:** 2026-04-14  
**Decision:** Mock HA API at adapter boundary, contract tests for Protocol compliance

## Context

Safety-critical code (guards, mode change, EV controller) depends on HA sensor data. How do we test that this code works correctly without a live HA instance?

## Current Test Strategy (668 tests)

### Unit tests (core/)
- Pure function tests: guards, balancer, state machine, planner
- Mock `HAApiClient` at adapter boundary
- `make_battery_state()`, `make_snapshot()` factory helpers
- No I/O, no network, runs in <10s

### Regression tests (B1-B15)
- One test per known production bug
- Reproduces exact failure condition
- Verifies fix is in place

### Edge case tests (PLAT-1385)
- SoC 0/100%, temperature extremes, HA disconnected
- Grid at Ellevio limit, dual battery asymmetric

### Code quality tests
- ruff, mypy --strict, anti-pattern detection
- Raw string detection (EMSMode, auto mode)
- Regression suite completeness (B1-B15)

## Identified Gaps

### Gap 1: No contract tests for adapter Protocols
**Problem:** GoodWeAdapter claims to satisfy InverterPort, but mypy can't verify Protocol compliance with dict invariance.
**Fix:** Add explicit `isinstance(adapter, InverterPort)` runtime check in tests.

### Gap 2: No integration test with mock HA server
**Problem:** Unit tests mock individual methods. No test verifies the full HTTP flow.
**Fix:** Add aiohttp test server that simulates HA REST API responses.

### Gap 3: No chaos/fault injection tests
**Problem:** We test "HA disconnected" but not "HA returns garbage JSON" or "HA responds slowly".
**Fix:** Add fault injection tests: malformed JSON, 500 errors, timeouts.

## Recommendations

### Immediate (before cutover)
1. Current 668 tests are sufficient for v2.0.0 cutover
2. Edge case tests cover critical safety paths
3. Regression suite covers all known bugs

### Post-cutover (v2.1)
1. Contract tests: verify adapter Protocol compliance
2. Mock HA server: full HTTP integration test
3. Fault injection: malformed responses, timeouts
4. Property-based tests (hypothesis) for balancer allocation invariants

## Example Implementations

### Contract test
```python
def test_goodwe_satisfies_inverter_port():
    """GoodWeAdapter must satisfy InverterPort Protocol."""
    adapter = GoodWeAdapter(mock_api, mock_config)
    # Verify all Protocol methods exist with correct signatures
    assert hasattr(adapter, 'set_ems_mode')
    assert hasattr(adapter, 'set_ems_power_limit')
    assert hasattr(adapter, 'set_fast_charging')
    assert hasattr(adapter, 'get_fast_charging')
    assert hasattr(adapter, 'get_ems_mode')
```

### Fault injection test
```python
async def test_ha_returns_garbage_json():
    """Guard should handle malformed HA response gracefully."""
    mock_api.get_state = AsyncMock(return_value="not_a_number")
    # Should not crash — fallback to safe default
    snapshot = await service._collect_snapshot(True)
    assert snapshot is not None  # or None with logged error
```

## Consequences

- No test infrastructure changes needed for cutover
- Post-cutover test improvements tracked as separate stories
- Safety-critical paths have sufficient coverage via unit + edge + regression tests
