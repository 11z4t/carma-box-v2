# SPIKE-03: Observerabilitetslösning för kunddrift

**Status:** Decided  
**Date:** 2026-04-14  
**Decision:** Prometheus + JSON logs + HA sensors (current stack, no Loki/Grafana yet)

## Context

CARMA Box v2 needs operational visibility for:
1. Is the system running? (health)
2. What is it doing? (decision audit)
3. Is it performing well? (savings, peak reduction)
4. What went wrong? (error investigation)

## Options

### Option A: Full observability stack (Prometheus + Loki + Grafana)

**Pros:** Industry standard, rich dashboards, alerting, log correlation
**Cons:** Heavy infrastructure (3 services), overkill for single-site residential
**Verdict:** Defer until multi-customer deployment

### Option B: Prometheus metrics + JSON logs (current)

**Pros:**
- health.py already exposes Prometheus metrics on :8412
- DecisionLog writes structured JSON per cycle
- HA sensors provide real-time dashboard visibility
- journalctl for log access (systemd)
- Low operational overhead

**Cons:**
- No log aggregation across sites
- No built-in alerting (Slack notifications cover this)
- Historical analysis requires SQLite queries

### Option C: HA-native only (sensors + logbook)

**Pros:** Zero additional infrastructure
**Cons:** No Prometheus, no external monitoring, no structured queries

## Decision

**Option B: Prometheus + JSON logs + HA sensors.**

This is already implemented:
- `health.py`: /health endpoint with cycle count, uptime, last error
- `decision_log.py`: structured per-cycle audit trail
- Dashboard write-back: scenario, rules, decision reason sensors
- Slack notifications for guard triggers and errors
- SQLite local_db for historical cycle/event/audit data

## Future Enhancement (multi-customer)

When deploying to multiple sites:
1. Add Loki for centralized log aggregation
2. Add Grafana dashboards per customer
3. Add Alertmanager for oncall notifications
4. Hub sync already prepares data for PostgreSQL aggregation

## Consequences

- No new infrastructure needed for single-site cutover
- Monitor via: HA dashboard + journalctl + Slack + /health endpoint
- Hub sync provides data pipeline for future multi-site observability
