"""Monitoring API: CloudWatch metrics/alarms proxied for the Admin UI.

Exposes ``GET /monitoring/cloudwatch`` so the portal's Dashboard can render
the CloudWatch charts natively — the customer sees latency, error-rate, and
alarm data behind the portal's own login, with **no AWS account or console
sign-in required**. The backend reads CloudWatch with the EC2 instance role
(read-only ``GetMetricData``/``DescribeAlarms``), so no AWS credentials ever
reach the browser.

Results are cached in-process for :data:`CACHE_TTL_S` seconds to stay far
below CloudWatch API rate limits regardless of how many admins keep the
Dashboard open.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.config import get_settings

logger = logging.getLogger(__name__)

__all__ = ["monitoring_router", "CACHE_TTL_S"]

#: CloudWatch namespace the app publishes to (see app/monitoring/publisher.py).
NAMESPACE = "IntelliKnow"

#: Only alarms with this name prefix are reported.
ALARM_PREFIX = "IntelliKnow"

#: Seconds a fetched snapshot is served from cache before CloudWatch is asked
#: again. The publisher emits datapoints every 60s, so 60s loses nothing.
CACHE_TTL_S = 60.0

#: Metric aggregation period in seconds (matches the alarm/dashboard setup).
PERIOD_S = 300

#: Bounds for the requested lookback window.
HOURS_MIN, HOURS_MAX, HOURS_DEFAULT = 1, 72, 6

monitoring_router = APIRouter(prefix="/monitoring", tags=["monitoring"])

# (window_hours) -> (fetched_monotonic, payload)
_cache: dict[int, tuple[float, dict[str, Any]]] = {}


def _series(result: dict[str, Any]) -> dict[str, list]:
    """Convert one GetMetricData result into a JSON-friendly series."""
    stamps = result.get("Timestamps") or []
    values = result.get("Values") or []
    pairs = sorted(zip(stamps, values), key=lambda p: p[0])
    return {
        "timestamps": [t.isoformat() for t, _ in pairs],
        "values": [float(v) for _, v in pairs],
    }


def _fetch_snapshot(hours: int) -> dict[str, Any]:
    """Read metrics + alarm states from CloudWatch for the last ``hours``."""
    import boto3

    settings = get_settings()
    client = boto3.client("cloudwatch", region_name=settings.aws_region)
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)

    queries = [
        {
            "Id": qid,
            "MetricStat": {
                "Metric": {"Namespace": NAMESPACE, "MetricName": metric},
                "Period": PERIOD_S,
                "Stat": stat,
            },
        }
        for qid, metric, stat in (
            ("latency_p95", "QueryLatencyMs", "p95"),
            ("latency_p50", "QueryLatencyMs", "p50"),
            ("error_rate", "ErrorRatePercent", "Average"),
        )
    ]
    data = client.get_metric_data(
        MetricDataQueries=queries, StartTime=start, EndTime=end
    )
    series = {
        r["Id"]: _series(r) for r in data.get("MetricDataResults", [])
    }

    alarms = [
        {
            "name": a["AlarmName"],
            "state": a["StateValue"],
            "description": a.get("AlarmDescription", ""),
        }
        for a in client.describe_alarms(AlarmNamePrefix=ALARM_PREFIX).get(
            "MetricAlarms", []
        )
    ]

    return {
        "window_hours": hours,
        "fetched_at": end.isoformat(),
        "region": settings.aws_region,
        "series": series,
        "alarms": alarms,
    }


@monitoring_router.get(
    "/cloudwatch",
    summary="CloudWatch metrics and alarm states for the portal Dashboard",
)
def cloudwatch_snapshot(
    hours: int = Query(
        HOURS_DEFAULT,
        ge=HOURS_MIN,
        le=HOURS_MAX,
        description="Lookback window in hours.",
    ),
) -> dict[str, Any]:
    """Return latency/error-rate series and alarm states, cached ~60s.

    Raises:
        HTTPException: 502 when CloudWatch cannot be reached (missing role
            permissions, region misconfiguration, or throttling).
    """
    cached = _cache.get(hours)
    if cached is not None and time.monotonic() - cached[0] < CACHE_TTL_S:
        return cached[1]

    try:
        payload = _fetch_snapshot(hours)
    except Exception as exc:  # noqa: BLE001 - surface as a clean 502
        logger.warning("CloudWatch snapshot fetch failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="CloudWatch data is unavailable right now.",
        )

    _cache[hours] = (time.monotonic(), payload)
    return payload
