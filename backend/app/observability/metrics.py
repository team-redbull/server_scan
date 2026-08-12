"""Prometheus metrics.

`prometheus-client`'s default registry is used directly (not a custom
registry passed around via DI) — that's the library's own documented
pattern and matches what every Prometheus-scraping tool expects to find
mounted at `/metrics` with no extra wiring.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests handled",
    labelnames=("method", "path", "status"),
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    labelnames=("method", "path"),
)

mongo_ping_failures_total = Counter(
    "mongo_ping_failures_total",
    "Count of failed MongoDB readiness pings",
)

cache_operations_total = Counter(
    "cache_operations_total",
    "Cache operations by outcome",
    labelnames=("operation", "outcome"),  # outcome: hit|miss|error
)
