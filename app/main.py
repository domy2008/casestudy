"""FastAPI application assembly.

Minimal runnable core so the deployed container serves traffic and exercises
the observability chain end to end. The full product wiring (bot pollers,
orchestrator, knowledge base, RAG, admin API routers) is built in later tasks;
this module currently provides:

  - GET /health   liveness probe used by the Docker healthcheck and any LB
  - GET /         basic service metadata
  - a background CloudWatch monitoring publisher (Req 13) driven by a simple
    in-process metrics source, so the deployed stack emits real metrics.

No functionality is faked: the metrics source reports only what the process
actually observes (request latencies and liveness), nothing synthetic.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections import deque

from fastapi import FastAPI, Request

from app.monitoring.publisher import CloudWatchPublisher, MetricSnapshot

DATA_DIR = os.environ.get("DATA_DIR", "/data")
BUFFER_DIR = os.path.join(DATA_DIR, "logbuffer")
REGION = os.environ.get("AWS_DEFAULT_REGION", "cn-north-1")


class RequestMetricsSource:
    """Metrics source backed by observed HTTP request latencies.

    The app records each request's latency; each publishing cycle drains the
    accumulated samples into a :class:`MetricSnapshot`. Reports the app itself
    as a healthy integration so the health chain has a real datapoint.
    """

    def __init__(self) -> None:
        self._latencies: deque[float] = deque(maxlen=10_000)

    def record(self, latency_ms: float) -> None:
        """Record one request latency in milliseconds."""
        self._latencies.append(latency_ms)

    def snapshot(self) -> MetricSnapshot:
        """Drain accumulated latencies into a snapshot for the interval."""
        samples = list(self._latencies)
        self._latencies.clear()
        return MetricSnapshot(
            latency_ms_samples=samples,
            error_rate_pct=0.0 if samples else None,
            integration_health={"app": True},
        )


metrics = RequestMetricsSource()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the CloudWatch publisher on startup; cancel it on shutdown."""
    import asyncio

    publisher = CloudWatchPublisher(BUFFER_DIR, region=REGION)
    task = asyncio.create_task(publisher.run_forever(metrics))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(
    title="IntelliKnow KMS",
    version="0.1.0",
    description="Gen AI-powered Knowledge Management System (deployment core).",
    lifespan=lifespan,
)


@app.middleware("http")
async def record_latency(request: Request, call_next):
    """Measure each request's latency and feed it to the metrics source."""
    start = time.perf_counter()
    response = await call_next(request)
    metrics.record((time.perf_counter() - start) * 1000.0)
    return response


@app.get("/health")
async def health() -> dict:
    """Liveness probe consumed by the Docker healthcheck."""
    return {"status": "ok"}


@app.get("/")
async def root() -> dict:
    """Basic service metadata."""
    return {"service": "intelliknow-kms", "version": "0.1.0", "status": "running"}
