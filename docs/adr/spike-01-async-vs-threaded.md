# SPIKE-01: Asynkron kontrollloop vs threaded

**Status:** Decided  
**Date:** 2026-04-14  
**Decision:** Keep asyncio (current implementation)

## Context

CARMA Box v2 uses `asyncio` for its 30-second control loop. All I/O (HA REST API, SQLite, Slack webhooks) is async. The question: should we use asyncio or switch to a threaded model?

## Options

### Option A: asyncio (current)

**Pros:**
- Single-threaded — no race conditions, no locks needed
- Natural fit for I/O-bound workload (HTTP calls to HA, Easee, Solcast)
- `asyncio.create_task()` for non-blocking operations (e.g., fix_waiting_in_fully)
- Python 3.12 asyncio is mature and well-supported
- aiohttp, aiosqlite, asyncpg all available
- Lower memory footprint than thread pool

**Cons:**
- CPU-bound work blocks the event loop (but we have none — all logic is fast)
- Debugging async tracebacks slightly harder
- Some libraries (e.g., pdfplumber for reports) are sync-only — need `run_in_executor`
- GoodWe Modbus UDP is sync in goodwe lib — already handled via HA integration

### Option B: Threaded (ThreadPoolExecutor)

**Pros:**
- Simpler mental model for sequential operations
- No async/await boilerplate
- Sync libraries work directly

**Cons:**
- Race conditions: multiple threads reading/writing shared state
- Need locks for: battery state, SoC tracking, mode change state, audit trail
- GIL limits true parallelism (but we're I/O bound so this matters less)
- Higher memory per thread (~8MB stack each)
- Harder to cancel operations cleanly

### Option C: Hybrid (asyncio + thread pool for sync ops)

**Pros:**
- Best of both: async for I/O, threads for sync libraries
- `loop.run_in_executor()` bridges sync code

**Cons:**
- Complexity of two concurrency models
- Need to be careful about thread safety of shared state

## Decision

**Keep asyncio (Option A).**

Rationale:
1. All I/O is already async (aiohttp, aiosqlite)
2. Control loop is I/O-bound (HA API calls), not CPU-bound
3. No shared mutable state across concurrent tasks — single-threaded is safer
4. fix_waiting_in_fully already uses create_task() successfully
5. Excel reports (sync) run outside the control loop — not time-critical

## Consequences

- Continue using `async def` for all adapters and core logic
- Use `loop.run_in_executor()` for any future sync-only library calls
- Monitor event loop blocking via `asyncio.get_event_loop().slow_callback_duration`
