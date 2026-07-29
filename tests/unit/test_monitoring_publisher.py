"""Unit tests for the CloudWatch monitoring publisher (Req 13.1, 13.6).

Covers the three behaviors called out in task 14.2:
  - a publish failure buffers datapoints locally and resends them next cycle,
  - query processing continues uninterrupted when publication fails,
  - metric payloads carry query latency, error rate, and per-tool health.

The CloudWatch client is faked, so these tests never touch boto3 or AWS.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.monitoring.publisher import (
    METRIC_ERROR_RATE,
    METRIC_INTEGRATION_HEALTH,
    METRIC_LATENCY,
    NAMESPACE,
    CloudWatchPublisher,
    MetricSnapshot,
)


class FakeCloudWatch:
    """Minimal stand-in for a boto3 CloudWatch client.

    Records every ``put_metric_data`` call and can be toggled to fail, so tests
    can drive the success, failure/buffering, and recovery paths.
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[list[dict]] = []

    def put_metric_data(self, Namespace: str, MetricData: list[dict]) -> None:
        if self.fail:
            raise RuntimeError("simulated CloudWatch outage")
        assert Namespace == NAMESPACE
        self.calls.append(MetricData)


class FakeSource:
    """A metrics source returning a fixed snapshot; counts snapshot calls."""

    def __init__(self, snapshot: MetricSnapshot) -> None:
        self._snapshot = snapshot
        self.calls = 0

    def snapshot(self) -> MetricSnapshot:
        self.calls += 1
        return self._snapshot


def _snapshot() -> MetricSnapshot:
    """A representative snapshot exercising all three metric families."""
    return MetricSnapshot(
        latency_ms_samples=[120.0, 120.0, 480.0],
        error_rate_pct=7.5,
        integration_health={"telegram": True, "teams": False},
    )


def _metric_names(data: list[dict]) -> set[str]:
    return {d["MetricName"] for d in data}


def test_payload_carries_latency_error_rate_and_per_tool_health(tmp_path: Path) -> None:
    """A successful cycle publishes latency, error rate, and per-tool health."""
    client = FakeCloudWatch()
    pub = CloudWatchPublisher(tmp_path, client_factory=lambda: client)

    published = pub.publish_cycle(FakeSource(_snapshot()))

    assert published is True
    assert len(client.calls) == 1
    names = _metric_names(client.calls[0])
    assert METRIC_LATENCY in names
    assert METRIC_ERROR_RATE in names
    assert f"{METRIC_INTEGRATION_HEALTH}telegram" in names
    assert f"{METRIC_INTEGRATION_HEALTH}teams" in names

    by_name = {d["MetricName"]: d for d in client.calls[0]}
    # Latency is a value/count distribution with duplicates collapsed.
    latency = by_name[METRIC_LATENCY]
    assert dict(zip(latency["Values"], latency["Counts"]))[120.0] == 2.0
    assert latency["Unit"] == "Milliseconds"
    # Error rate carries the interval percentage.
    assert by_name[METRIC_ERROR_RATE]["Value"] == 7.5
    # Health is 1 for healthy, 0 for unhealthy.
    assert by_name[f"{METRIC_INTEGRATION_HEALTH}telegram"]["Value"] == 1.0
    assert by_name[f"{METRIC_INTEGRATION_HEALTH}teams"]["Value"] == 0.0


def test_publish_failure_buffers_locally_and_processing_continues(tmp_path: Path) -> None:
    """When publish fails, datapoints are buffered and the cycle never raises."""
    client = FakeCloudWatch(fail=True)
    pub = CloudWatchPublisher(tmp_path, client_factory=lambda: client)

    # Must not raise — query processing continues uninterrupted (Req 13.6).
    published = pub.publish_cycle(FakeSource(_snapshot()))

    assert published is False
    buffered = list(tmp_path.glob("*.json"))
    assert len(buffered) == 1
    # The buffered file holds the full datapoint batch for later delivery.
    saved = json.loads(buffered[0].read_text(encoding="utf-8"))
    assert _metric_names(saved) == {
        METRIC_LATENCY,
        METRIC_ERROR_RATE,
        f"{METRIC_INTEGRATION_HEALTH}telegram",
        f"{METRIC_INTEGRATION_HEALTH}teams",
    }


def test_buffered_datapoints_resent_on_next_successful_cycle(tmp_path: Path) -> None:
    """Buffered datapoints are flushed on the next cycle once CloudWatch recovers."""
    # Cycle 1: outage -> buffer.
    failing = FakeCloudWatch(fail=True)
    pub_fail = CloudWatchPublisher(tmp_path, client_factory=lambda: failing)
    pub_fail.publish_cycle(FakeSource(_snapshot()))
    assert len(list(tmp_path.glob("*.json"))) == 1

    # Cycle 2: recovery -> flush the buffered batch AND publish the current one.
    healthy = FakeCloudWatch()
    pub_ok = CloudWatchPublisher(tmp_path, client_factory=lambda: healthy)
    published = pub_ok.publish_cycle(FakeSource(_snapshot()))

    assert published is True
    assert len(healthy.calls) == 2  # one flushed batch + current interval
    assert list(tmp_path.glob("*.json")) == []  # buffer fully drained


def test_empty_snapshot_publishes_nothing(tmp_path: Path) -> None:
    """An interval with no metrics makes no CloudWatch call and reports success."""
    client = FakeCloudWatch()
    pub = CloudWatchPublisher(tmp_path, client_factory=lambda: client)

    published = pub.publish_cycle(FakeSource(MetricSnapshot()))

    assert published is True
    assert client.calls == []


def test_collection_failure_is_swallowed(tmp_path: Path) -> None:
    """A metrics-source error is logged and swallowed, not propagated."""

    class BrokenSource:
        def snapshot(self) -> MetricSnapshot:
            raise RuntimeError("analytics unavailable")

    client = FakeCloudWatch()
    pub = CloudWatchPublisher(tmp_path, client_factory=lambda: client)

    published = pub.publish_cycle(BrokenSource())

    assert published is False
    assert client.calls == []
