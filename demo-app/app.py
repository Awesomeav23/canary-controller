"""A tiny service whose failure rate is baked in at build time, so "v2-bad" really is bad."""

import os
import random
import time

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

APP = os.getenv("APP_NAME", "demo")
TRACK = os.getenv("TRACK", "stable")  # set per Deployment: stable or canary
VERSION = os.getenv("VERSION", "dev")
ERROR_RATE = float(os.getenv("ERROR_RATE", "0"))
EXTRA_LATENCY_MS = float(os.getenv("EXTRA_LATENCY_MS", "0"))

REQUESTS = Counter("http_requests_total", "Requests served", ["app", "track", "status"])
LATENCY = Histogram("http_request_duration_seconds", "Request latency", ["app", "track"])

app = FastAPI()


@app.get("/")
def root() -> Response:
    start = time.perf_counter()
    if EXTRA_LATENCY_MS:
        time.sleep(EXTRA_LATENCY_MS / 1000)
    status = 500 if random.random() < ERROR_RATE else 200
    REQUESTS.labels(APP, TRACK, str(status)).inc()
    LATENCY.labels(APP, TRACK).observe(time.perf_counter() - start)
    return Response(f"{VERSION} ({TRACK})\n", status_code=status, media_type="text/plain")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
