"""CloudWatch metric publishing with local buffering on failure (Req 13).

This module is the application-side half of the observability stack. The
infrastructure-side half — the CloudWatch alarms and the cloudwatch-agent that
ships container stdout to CloudWatch Logs — lives under ``deploy/`` and consumes
the metrics this publisher emits.

Every publishing cycle (at most 60 seconds apart, Req 13.1) the publisher:

1. Collects a :class:`MetricSnapshot` from an injected metrics source (the
   Analytics module in production; a fake in tests).
2. Builds CloudWatch ``MetricDatum`` records for query latency, error rate, and
   per-tool integration health.
3. Attempts to flush any datapoints buffered from previous failed cycles, then
   publishes the current batch via boto3 ``put_metric_data``.
4. On any publication failure, appends the affected datapoints to a local buffer
   under ``/data/logbuffer/`` for redelivery on the next successful cycle
   (Req 13.6). Failures never propagate to the caller, so query processing is
   never interrupted.

The CloudWatch client is injected via ``client_factory`` so the whole module can
be exercised in tests without touching AWS or boto3's network layer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

logger = logging.getLogger(__name__)

# CloudWatch namespace the alarms in deploy/setup_cloudwatch_alarms.py watch.
NAMESPACE = "IntelliKnow"

# Metric names — must match deploy/setup_cloudwatch_alarms.py.
METRIC_LATENCY = "QueryLatencyMs"
METRIC_ERROR_RATE = "ErrorRatePercent"
METRIC_INTEGRATION_HEALTH = "IntegrationHealthy"  # + verbatim tool suffix, e.g. ...telegram

# CloudWatch accepts at most 1000 metrics per PutMetricData request.
_MAX_METRICS_PER_CALL = 1000

# Default publishing cadence (Req 13.1: at most 60 seconds).
DEFAULT_INTERVAL_S = 60.0


@dataclass
class MetricSnapshot:
    """A point-in-time view of the metrics to publish for one interval.

    Attributes:
        latency_ms_samples: Individual query latencies (ms) observed during the
            interval. Published as a value/count distribution so CloudWatch can
            compute the p95 the latency alarm evaluates (Req 13.3).
        error_rate_pct: Percentage of queries with response status Failed over
            the interval, 0..100 (Req 13.1, 13.5). ``None`` when no queries ran,
            in which case the error-rate metric is omitted for the cycle.
        integration_health: Map of Frontend_Tool name -> healthy flag from the
            5-minute health checks (Req 13.4). Published as 1 (healthy) / 0
            (unhealthy) under ``IntegrationHealthy{Tool}``.
    """

    latency_ms_samples: list[float] = field(default_factory=list)
    error_rate_pct: float | None = None
    integration_health: dict[str, bool] = field(default_factory=dict)


class MetricsSource(Protocol):
    """Supplies the metrics for one publishing interval.

    Implemented by the Analytics module in production; the publisher depends only
    on this narrow interface so it stays decoupled and unit-testable.
    """

    def snapshot(self) -> MetricSnapshot:
        """Return the metrics accumulated since the previous call."""
        ...


def _utcnow() -> datetime:
    """Current UTC time (indirection kept for test monkeypatching)."""
    return datetime.now(timezone.utc)


class CloudWatchPublisher:
    """Publishes IntelliKnow metrics to CloudWatch, buffering locally on failure.

    Args:
        buffer_dir: Directory for on-disk retention of datapoints that could not
            be published (``/data/logbuffer`` in production). Created if absent.
        namespace: CloudWatch namespace for all metrics. Defaults to
            ``IntelliKnow`` to match the alarm setup script.
        region: AWS China region for the CloudWatch client (e.g. ``cn-north-1``).
        client_factory: Zero-arg callable returning a CloudWatch client exposing
            ``put_metric_data``. Defaults to a lazy boto3 client. Injected in
            tests to avoid any AWS dependency.
        interval_s: Seconds between publishing cycles (Req 13.1 caps this at 60).
        now: Clock function returning an aware UTC datetime; injectable for tests.
    """

    def __init__(
        self,
        buffer_dir: str | Path,
        *,
        namespace: str = NAMESPACE,
        region: str | None = None,
        client_factory: Callable[[], object] | None = None,
        interval_s: float = DEFAULT_INTERVAL_S,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.buffer_dir = Path(buffer_dir)
        self.buffer_dir.mkdir(parents=True, exist_ok=True)
        self.namespace = namespace
        self.region = region
        self.interval_s = interval_s
        self._now = now
        self._client_factory = client_factory or self._default_client_factory
        self._client: object | None = None

    def _default_client_factory(self) -> object:
        """Create a boto3 CloudWatch client for the configured region.

        Imported lazily so importing this module never requires boto3 or AWS
        configuration (keeps the test surface clean).
        """
        import boto3

        return boto3.client("cloudwatch", region_name=self.region)

    @property
    def client(self) -> object:
        """The CloudWatch client, created on first use."""
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    # -- metric construction ------------------------------------------------

    def build_metric_data(self, snapshot: MetricSnapshot) -> list[dict]:
        """Convert a snapshot into CloudWatch ``MetricDatum`` dicts.

        Latency is emitted as a values/counts distribution (deduplicated) so
        CloudWatch can compute percentile statistics such as the p95 the latency
        alarm evaluates. Error rate is a single average value, omitted entirely
        when no queries ran during the interval. Integration health is one 1/0
        datum per tool.

        Args:
            snapshot: The metrics collected for the interval.

        Returns:
            A list of MetricDatum dicts suitable for ``put_metric_data``.
        """
        ts = self._now()
        data: list[dict] = []

        if snapshot.latency_ms_samples:
            # Collapse duplicate latency values into value/count pairs to stay
            # well under CloudWatch's per-datum array limits on busy intervals.
            counts: dict[float, int] = {}
            for sample in snapshot.latency_ms_samples:
                counts[float(sample)] = counts.get(float(sample), 0) + 1
            data.append(
                {
                    "MetricName": METRIC_LATENCY,
                    "Timestamp": ts,
                    "Unit": "Milliseconds",
                    "Values": list(counts.keys()),
                    "Counts": [float(c) for c in counts.values()],
                }
            )

        if snapshot.error_rate_pct is not None:
            data.append(
                {
                    "MetricName": METRIC_ERROR_RATE,
                    "Timestamp": ts,
                    "Unit": "Percent",
                    "Value": float(snapshot.error_rate_pct),
                }
            )

        for tool, healthy in snapshot.integration_health.items():
            # Tool name is used verbatim so the metric name matches the alarm
            # deploy/setup_cloudwatch_alarms.py provisions (IntegrationHealthy{tool}).
            data.append(
                {
                    "MetricName": f"{METRIC_INTEGRATION_HEALTH}{tool}",
                    "Timestamp": ts,
                    "Unit": "None",
                    "Value": 1.0 if healthy else 0.0,
                }
            )

        return data

    # -- publishing ---------------------------------------------------------

    def publish_cycle(self, source: MetricsSource) -> bool:
        """Run one publishing cycle: flush the buffer, then publish current data.

        Never raises. Any collection, build, or publish failure is logged and the
        affected datapoints are retained on disk for the next cycle (Req 13.6),
        so query processing is never interrupted.

        Args:
            source: Provides the metrics snapshot for this interval.

        Returns:
            True if the current interval's datapoints were published to
            CloudWatch this cycle; False if they were buffered for retry.
        """
        # Always try to drain previously buffered datapoints first so retained
        # data is delivered as soon as CloudWatch is reachable again.
        self._flush_buffer()

        try:
            snapshot = source.snapshot()
            data = self.build_metric_data(snapshot)
        except Exception:  # never let metric collection break the caller
            logger.exception("metric snapshot/collection failed; skipping cycle")
            return False

        if not data:
            return True  # nothing to publish this interval

        try:
            self._put(data)
            return True
        except Exception:
            logger.warning(
                "CloudWatch put_metric_data failed; buffering %d datapoints locally",
                len(data),
            )
            self._buffer(data)
            return False

    def _put(self, data: list[dict]) -> None:
        """Send metric data to CloudWatch, chunked under the per-request limit.

        Raises whatever the underlying client raises; callers handle buffering.
        """
        for start in range(0, len(data), _MAX_METRICS_PER_CALL):
            chunk = data[start : start + _MAX_METRICS_PER_CALL]
            self.client.put_metric_data(Namespace=self.namespace, MetricData=chunk)

    # -- local buffering (Req 13.6) -----------------------------------------

    def _buffer(self, data: list[dict]) -> None:
        """Persist unpublished datapoints to the local buffer as one JSON file.

        The write itself is guarded: if even buffering fails (e.g. disk full),
        the error is logged and swallowed so query processing continues.
        """
        try:
            name = f"{int(time.time() * 1000)}-{uuid.uuid4().hex}.json"
            path = self.buffer_dir / name
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, default=_json_default), encoding="utf-8")
            tmp.replace(path)  # atomic publish of the buffer file
        except Exception:
            logger.exception("failed to buffer metrics locally; dropping datapoints")

    def _flush_buffer(self) -> None:
        """Re-send buffered datapoints; delete each file only on successful send.

        Files that still fail to publish are left in place for a later cycle.
        Never raises.
        """
        try:
            files = sorted(self.buffer_dir.glob("*.json"))
        except Exception:
            logger.exception("failed to list metric buffer directory")
            return

        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                for datum in data:
                    if isinstance(datum.get("Timestamp"), str):
                        datum["Timestamp"] = datetime.fromisoformat(datum["Timestamp"])
                self._put(data)
            except Exception:
                # CloudWatch still unreachable (or a bad file) — keep it and stop
                # draining this cycle to preserve ordering and avoid busy-looping.
                logger.warning("could not flush buffered metrics %s; will retry", path.name)
                return
            else:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    # -- background loop ----------------------------------------------------

    async def run_forever(self, source: MetricsSource) -> None:
        """Publish metrics on a fixed cadence until the task is cancelled.

        Intended to be launched as a FastAPI startup background task. Sleeps
        ``interval_s`` between cycles and exits cleanly on cancellation.
        """
        logger.info(
            "monitoring publisher started (namespace=%s, interval=%.0fs, buffer=%s)",
            self.namespace,
            self.interval_s,
            self.buffer_dir,
        )
        try:
            while True:
                self.publish_cycle(source)
                await asyncio.sleep(self.interval_s)
        except asyncio.CancelledError:
            logger.info("monitoring publisher stopping")
            raise


def _json_default(value: object) -> str:
    """JSON serializer for datetimes stored in buffered metric data."""
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
