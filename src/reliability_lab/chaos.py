from __future__ import annotations

import json
import random
from pathlib import Path

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(config: LabConfig, provider_overrides: dict[str, float] | None = None) -> ReliabilityGateway:
    providers = []
    for p in config.providers:
        fail_rate = provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        providers.append(FakeLLMProvider(p.name, fail_rate, p.base_latency_ms, p.cost_per_1k_tokens))
    breakers = {
        p.name: CircuitBreaker(
            name=p.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
        )
        for p in config.providers
    }
    cache: ResponseCache | SharedRedisCache | None = None
    if config.cache.enabled:
        if config.cache.backend == "redis":
            cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
        else:
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
    return ReliabilityGateway(providers, breakers, cache)


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    """Derive recovery time from circuit breaker transition logs.

    Recovery time = time between circuit opening and next successful close.
    Returns the average recovery time across all breakers, or None if no recovery occurred.
    """
    recovery_times: list[float] = []
    for breaker in gateway.breakers.values():
        open_ts: float | None = None
        for entry in breaker.transition_log:
            if entry["to"] == "open" and open_ts is None:
                open_ts = float(entry["ts"])
            elif entry["to"] == "closed" and open_ts is not None:
                recovery_times.append((float(entry["ts"]) - open_ts) * 1000)
                open_ts = None
    if not recovery_times:
        return None
    return sum(recovery_times) / len(recovery_times)


def run_scenario(config: LabConfig, queries: list[str], scenario: ScenarioConfig) -> RunMetrics:
    """Run a single named chaos scenario."""
    gateway = build_gateway(config, scenario.provider_overrides or None)
    metrics = RunMetrics()
    request_count = config.load_test.requests

    if scenario.name in {"primary_timeout_100", "primary_flaky_50"}:
        gateway.cache = None

    if scenario.name == "cache_stale_candidate" and gateway.cache is not None:
        gateway.cache.set("Summarize refund policy for 2024 deadline", "Old refund policy")
        cached, _ = gateway.cache.get("Summarize refund policy for 2026 deadline")
        metrics.scenarios[scenario.name] = "pass" if cached is None else "fail"

    for _ in range(request_count):
        prompt = random.choice(queries)
        result = gateway.complete(prompt)
        metrics.total_requests += 1
        metrics.estimated_cost += result.estimated_cost
        if result.cache_hit:
            metrics.cache_hits += 1
            metrics.estimated_cost_saved += 0.001
        if result.route.startswith("fallback:"):
            metrics.fallback_successes += 1
            metrics.successful_requests += 1
        elif result.route == "static_fallback":
            metrics.static_fallbacks += 1
            metrics.failed_requests += 1
        else:
            metrics.successful_requests += 1
        if result.latency_ms:
            metrics.latencies_ms.append(result.latency_ms)

    metrics.circuit_open_count = sum(
        1 for breaker in gateway.breakers.values() for t in breaker.transition_log if t["to"] == "open"
    )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    if isinstance(gateway.cache, SharedRedisCache):
        gateway.cache.close()
    return metrics


def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    """Run all named scenarios from config, or a default run if none defined.
    """
    random.seed(7)
    if not config.scenarios:
        default_scenario = ScenarioConfig(name="default", description="baseline run")
        metrics = run_scenario(config, queries, default_scenario)
        metrics.scenarios = {"default": "pass" if metrics.successful_requests > 0 else "fail"}
        return metrics

    combined = RunMetrics()
    for scenario in config.scenarios:
        result = run_scenario(config, queries, scenario)

        if scenario.name in result.scenarios:
            combined.scenarios[scenario.name] = result.scenarios[scenario.name]
        elif scenario.name == "primary_timeout_100":
            passed = result.circuit_open_count > 0 and result.fallback_success_rate >= 0.95
            combined.scenarios[scenario.name] = "pass" if passed else "fail"
        elif scenario.name == "primary_flaky_50":
            passed = result.circuit_open_count > 0 and result.successful_requests > 0
            combined.scenarios[scenario.name] = "pass" if passed else "fail"
        elif scenario.name == "all_healthy":
            passed = result.availability >= 0.99 and result.error_rate == 0
            combined.scenarios[scenario.name] = "pass" if passed else "fail"
        else:
            passed = result.successful_requests > 0
            combined.scenarios[scenario.name] = "pass" if passed else "fail"

        combined.total_requests += result.total_requests
        combined.successful_requests += result.successful_requests
        combined.failed_requests += result.failed_requests
        combined.fallback_successes += result.fallback_successes
        combined.static_fallbacks += result.static_fallbacks
        combined.cache_hits += result.cache_hits
        combined.circuit_open_count += result.circuit_open_count
        combined.estimated_cost += result.estimated_cost
        combined.estimated_cost_saved += result.estimated_cost_saved
        combined.latencies_ms.extend(result.latencies_ms)
        if result.recovery_time_ms is not None:
            if combined.recovery_time_ms is None:
                combined.recovery_time_ms = result.recovery_time_ms
            else:
                combined.recovery_time_ms = (combined.recovery_time_ms + result.recovery_time_ms) / 2

    comparison_scenario = ScenarioConfig(
        name="cache_comparison",
        description="healthy providers for cache on/off comparison",
        provider_overrides={"primary": 0.0, "backup": 0.0},
    )
    cache_enabled_config = config.model_copy(deep=True)
    cache_enabled_config.scenarios = []
    cache_enabled_config.cache.enabled = True
    cache_enabled = run_scenario(cache_enabled_config, queries, comparison_scenario)

    cache_disabled_config = config.model_copy(deep=True)
    cache_disabled_config.scenarios = []
    cache_disabled_config.cache.enabled = False
    cache_disabled = run_scenario(cache_disabled_config, queries, comparison_scenario)
    combined.cache_comparison = {
        "without_cache": {
            "latency_p50_ms": round(cache_disabled.percentile(50), 2),
            "latency_p95_ms": round(cache_disabled.percentile(95), 2),
            "estimated_cost": round(cache_disabled.estimated_cost, 6),
            "cache_hit_rate": round(cache_disabled.cache_hit_rate, 4),
        },
        "with_cache": {
            "latency_p50_ms": round(cache_enabled.percentile(50), 2),
            "latency_p95_ms": round(cache_enabled.percentile(95), 2),
            "estimated_cost": round(cache_enabled.estimated_cost, 6),
            "cache_hit_rate": round(cache_enabled.cache_hit_rate, 4),
        },
    }

    return combined
