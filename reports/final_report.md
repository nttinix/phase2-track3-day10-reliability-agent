# Day 10 Reliability Final Report

## 1. Architecture summary

The gateway checks cache first, then routes provider calls through a circuit breaker per provider. When the primary provider is unhealthy, requests fail fast into the fallback chain; when all providers fail, the gateway returns a static degraded-service response.

```
User Request
    |
    v
[Gateway] -> [Cache check] -> HIT: cached response
    |
    v MISS
[Circuit: primary] -> Provider primary
    | OPEN/error
    v
[Circuit: backup] -> Provider backup
    | OPEN/error
    v
[Static fallback]
```

## 2. Configuration

| Setting | Value | Reason |
|---|---:|---|
| failure_threshold | 3 | Detects real outages quickly without opening on one transient failure. |
| reset_timeout_seconds | 1.0 | Gives a failed provider time to recover before probe traffic. |
| success_threshold | 1 | One healthy probe is enough for this low-risk fake provider lab. |
| cache TTL | 300 | Keeps FAQ-style answers warm while limiting stale responses. |
| similarity_threshold | 0.92 | High threshold plus false-hit guardrails avoids date-sensitive stale hits. |
| load_test requests | 100 | Enough requests to exercise cache hits and circuit transitions reproducibly. |

## 3. SLO definitions

| SLI | SLO target | Actual value | Met? |
|---|---|---:|---|
| Availability | >= 99% | 1.0000 | yes |
| Latency P95 | < 2500 ms | 499.6500 | yes |
| Fallback success rate | >= 95% | 1.0000 | yes |
| Cache hit rate | >= 10% | 0.3675 | yes |
| Recovery time | < 5000 ms | 2428.0586 | yes |

## 4. Metrics

| Metric | Value |
|---|---:|
| total_requests | 400 |
| availability | 1.0000 |
| error_rate | 0.0000 |
| latency_p50_ms | 217.2300 |
| latency_p95_ms | 499.6500 |
| latency_p99_ms | 533.3600 |
| fallback_success_rate | 1.0000 |
| cache_hit_rate | 0.3675 |
| circuit_open_count | 36 |
| recovery_time_ms | 2428.0586 |
| estimated_cost | 0.1068 |
| estimated_cost_saved | 0.1470 |

## 5. Cache comparison

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---:|
| latency_p50_ms | 215.1600 | 0.2800 | -99.9% |
| latency_p95_ms | 237.8400 | 218.4000 | -8.2% |
| estimated_cost | 0.0579 | 0.0177 | -69.5% |
| cache_hit_rate | 0.0000 | 0.7200 | 0.7200 |

## 6. Redis shared cache

- In-memory cache is insufficient for multi-instance deployments because each process has isolated state.
- `SharedRedisCache` stores query/response hashes in Redis with TTL, so separate gateway instances share cache hits.

Evidence: `tests/test_redis_cache.py::test_shared_state_across_instances` creates two cache clients with the same prefix; client two reads the entry written by client one.

Redis CLI evidence command:

```bash
docker compose exec redis redis-cli KEYS "rl:cache:*"
```

## 7. Chaos scenarios

| Scenario | Expected behavior | Observed behavior | Pass/Fail |
|---|---|---|---|
| primary_timeout_100 | Primary opens circuit; backup serves traffic. | Criteria met by metrics run. | pass |
| primary_flaky_50 | Circuit opens under failures; successful requests continue. | Criteria met by metrics run. | pass |
| all_healthy | Requests succeed without degraded fallback. | Criteria met by metrics run. | pass |
| cache_stale_candidate | Different years do not false-hit cache. | Criteria met by metrics run. | pass |

## 8. Failure analysis

Remaining weakness: circuit breaker state is process-local. In a horizontally scaled deployment, one instance may open its circuit while another keeps sending traffic to the same unhealthy provider.

Production fix: store circuit state and counters in Redis or another shared low-latency store, and add jittered probe traffic to avoid all instances probing at once.

## 9. Next steps

1. Move circuit breaker counters and transition logs to shared storage.
2. Add concurrent load testing using `load_test.concurrency`.
3. Export Prometheus counters for request, cache, latency, and circuit state metrics.