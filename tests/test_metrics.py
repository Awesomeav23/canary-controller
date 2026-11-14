import httpx
import pytest

from canary.metrics import QUERIES, PrometheusMetrics


def prom(result=None, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"status": "success", "data": {"result": result or []}})

    return PrometheusMetrics("http://prom", transport=httpx.MockTransport(handler))


def test_parses_value():
    m = prom([{"metric": {}, "value": [1700000000, "0.25"]}])
    assert m.query("error_rate", "demo", "canary", 60) == 0.25


def test_empty_result_is_no_data():
    assert prom([]).query("error_rate", "demo", "canary", 60) is None


def test_nan_is_no_data():
    assert (
        prom([{"metric": {}, "value": [1700000000, "NaN"]}]).query(
            "p95_latency_ms", "demo", "canary", 60
        )
        is None
    )


def test_http_error_is_no_data():
    assert prom(status=503).query("error_rate", "demo", "canary", 60) is None


def test_error_rate_defaults_to_zero_errors():
    # Regression: with no 5xx series the query used to return nothing, which made every
    # healthy canary look like "no data" and roll back. Found running the kind demo.
    assert "or vector(0)" in QUERIES["error_rate"]


@pytest.mark.parametrize("metric", sorted(QUERIES))
def test_queries_fill_in_labels(metric):
    q = QUERIES[metric].format(app="demo", track="canary", window=60)
    assert 'app="demo"' in q and 'track="canary"' in q and "[60s]" in q
