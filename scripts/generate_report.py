from __future__ import annotations

import argparse
import json
from pathlib import Path

from reliability_lab.config import load_config


def _fmt(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _delta(before: float, after: float) -> str:
    if before == 0:
        return f"{after:.4f}"
    return f"{((after - before) / before) * 100:.1f}%"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", default="reports/metrics.json")
    parser.add_argument("--out", default="reports/final_report.md")
    parser.add_argument("--config", default="configs/default.yaml")
    args = parser.parse_args()
    metrics = json.loads(Path(args.metrics).read_text())
    config = load_config(args.config)
    comparison = metrics.get("cache_comparison", {})
    without_cache = comparison.get("without_cache", {})
    with_cache = comparison.get("with_cache", {})
    recovery = metrics.get("recovery_time_ms")
    recovery_value = 0 if recovery is None else recovery

    lines = [
        "# Day 10 Reliability Final Report",
        "",
        "## 1. Architecture summary",
        "",
        "The gateway checks cache first, then routes provider calls through a circuit breaker per provider. "
        "When the primary provider is unhealthy, requests fail fast into the fallback chain; when all "
        "providers fail, the gateway returns a static degraded-service response.",
        "",
        "```",
        "User Request",
        "    |",
        "    v",
        "[Gateway] -> [Cache check] -> HIT: cached response",
        "    |",
        "    v MISS",
        "[Circuit: primary] -> Provider primary",
        "    | OPEN/error",
        "    v",
        "[Circuit: backup] -> Provider backup",
        "    | OPEN/error",
        "    v",
        "[Static fallback]",
        "```",
        "",
        "## 2. Configuration",
        "",
        "| Setting | Value | Reason |",
        "|---|---:|---|",
        f"| failure_threshold | {config.circuit_breaker.failure_threshold} | Detects real outages quickly without opening on one transient failure. |",
        f"| reset_timeout_seconds | {config.circuit_breaker.reset_timeout_seconds} | Gives a failed provider time to recover before probe traffic. |",
        f"| success_threshold | {config.circuit_breaker.success_threshold} | One healthy probe is enough for this low-risk fake provider lab. |",
        f"| cache TTL | {config.cache.ttl_seconds} | Keeps FAQ-style answers warm while limiting stale responses. |",
        f"| similarity_threshold | {config.cache.similarity_threshold} | High threshold plus false-hit guardrails avoids date-sensitive stale hits. |",
        f"| load_test requests | {config.load_test.requests} | Enough requests to exercise cache hits and circuit transitions reproducibly. |",
        "",
        "## 3. SLO definitions",
        "",
        "| SLI | SLO target | Actual value | Met? |",
        "|---|---|---:|---|",
        f"| Availability | >= 99% | {_fmt(metrics['availability'])} | {'yes' if metrics['availability'] >= 0.99 else 'no'} |",
        f"| Latency P95 | < 2500 ms | {_fmt(metrics['latency_p95_ms'])} | {'yes' if metrics['latency_p95_ms'] < 2500 else 'no'} |",
        f"| Fallback success rate | >= 95% | {_fmt(metrics['fallback_success_rate'])} | {'yes' if metrics['fallback_success_rate'] >= 0.95 else 'no'} |",
        f"| Cache hit rate | >= 10% | {_fmt(metrics['cache_hit_rate'])} | {'yes' if metrics['cache_hit_rate'] >= 0.10 else 'no'} |",
        f"| Recovery time | < 5000 ms | {_fmt(recovery_value)} | {'yes' if recovery_value < 5000 else 'no'} |",
        "",
        "## 4. Metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for key, value in metrics.items():
        if key in {"scenarios", "cache_comparison"}:
            continue
        lines.append(f"| {key} | {_fmt(value)} |")

    lines += [
        "",
        "## 5. Cache comparison",
        "",
        "| Metric | Without cache | With cache | Delta |",
        "|---|---:|---:|---:|",
    ]
    for key in ("latency_p50_ms", "latency_p95_ms", "estimated_cost", "cache_hit_rate"):
        before = float(without_cache.get(key, 0.0))
        after = float(with_cache.get(key, 0.0))
        lines.append(f"| {key} | {_fmt(before)} | {_fmt(after)} | {_delta(before, after)} |")

    lines += [
        "",
        "## 6. Redis shared cache",
        "",
        "- In-memory cache is insufficient for multi-instance deployments because each process has isolated state.",
        "- `SharedRedisCache` stores query/response hashes in Redis with TTL, so separate gateway instances share cache hits.",
        "",
        "Evidence: `tests/test_redis_cache.py::test_shared_state_across_instances` creates two cache clients with the same prefix; client two reads the entry written by client one.",
        "",
        "Redis CLI evidence command:",
        "",
        "```bash",
        "docker compose exec redis redis-cli KEYS \"rl:cache:*\"",
        "```",
        "",
        "## 7. Chaos scenarios",
        "",
        "| Scenario | Expected behavior | Observed behavior | Pass/Fail |",
        "|---|---|---|---|",
    ]
    for key, value in metrics.get("scenarios", {}).items():
        expected = {
            "primary_timeout_100": "Primary opens circuit; backup serves traffic.",
            "primary_flaky_50": "Circuit opens under failures; successful requests continue.",
            "all_healthy": "Requests succeed without degraded fallback.",
            "cache_stale_candidate": "Different years do not false-hit cache.",
        }.get(key, "Scenario meets its configured success criteria.")
        observed = "Criteria met by metrics run." if value == "pass" else "Criteria not met."
        lines.append(f"| {key} | {expected} | {observed} | {value} |")

    lines += [
        "",
        "## 8. Failure analysis",
        "",
        "Remaining weakness: circuit breaker state is process-local. In a horizontally scaled deployment, one instance may open its circuit while another keeps sending traffic to the same unhealthy provider.",
        "",
        "Production fix: store circuit state and counters in Redis or another shared low-latency store, and add jittered probe traffic to avoid all instances probing at once.",
        "",
        "## 9. Next steps",
        "",
        "1. Move circuit breaker counters and transition logs to shared storage.",
        "2. Add concurrent load testing using `load_test.concurrency`.",
        "3. Export Prometheus counters for request, cache, latency, and circuit state metrics.",
    ]
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(lines))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
