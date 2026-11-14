"""Reads canary health metrics from Prometheus."""

import logging
import math
from typing import Protocol

import httpx

log = logging.getLogger(__name__)

# {app}, {track} and {window} are filled in per query. Metric names match demo-app/app.py.
QUERIES = {
    # A pod that has never returned a 5xx has no 5xx series at all, so the numerator would
    # be empty and the whole query "no data". `or vector(0)` turns that into 0 errors.
    # No traffic at all still gives an empty denominator, so we still fail closed.
    "error_rate": (
        '(sum(rate(http_requests_total{{app="{app}",track="{track}",status=~"5.."}}[{window}s]))'
        " or vector(0))"
        ' / sum(rate(http_requests_total{{app="{app}",track="{track}"}}[{window}s]))'
    ),
    "p95_latency_ms": (
        "1000 * histogram_quantile(0.95, sum by (le) ("
        'rate(http_request_duration_seconds_bucket{{app="{app}",track="{track}"}}[{window}s])))'
    ),
    # Not a health metric: used to check there is enough traffic to judge at all.
    "request_count": (
        'sum(increase(http_requests_total{{app="{app}",track="{track}"}}[{window}s]))'
    ),
}


class MetricsSource(Protocol):
    def query(self, metric: str, app: str, track: str, window_seconds: int) -> float | None:
        """Return the metric value, or None when there is no data."""
        ...


class PrometheusMetrics:
    def __init__(
        self, base_url: str, timeout: float = 5.0, transport: httpx.BaseTransport | None = None
    ):
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

    def query(self, metric: str, app: str, track: str, window_seconds: int) -> float | None:
        promql = QUERIES[metric].format(app=app, track=track, window=window_seconds)
        try:
            resp = self._client.get("/api/v1/query", params={"query": promql})
            resp.raise_for_status()
            result = resp.json()["data"]["result"]
        except (httpx.HTTPError, KeyError, ValueError):
            log.exception("prometheus query failed: %s", metric)
            return None
        if not result:
            return None
        value = float(result[0]["value"][1])
        return None if math.isnan(value) else value
