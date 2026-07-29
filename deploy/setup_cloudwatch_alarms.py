"""Provision CloudWatch alarms and the SNS notification topic for IntelliKnow KMS.

Creates (idempotently) in the target AWS China region (Req 13.3-13.5, 13.7):

1. An SNS topic (``intelliknow-alarms``) with an optional email subscription.
2. Alarm: query latency p95 > threshold (default 3000 ms) over a 5-minute window.
3. Alarm: error rate > threshold (default 5%) over a 5-minute window.
4. One alarm per integration tool: IntegrationHealthy{tool} unhealthy (< 1)
   over a 5-minute window.

The application publishes the underlying metrics (``QueryLatencyMs``,
``ErrorRatePercent``, ``IntegrationHealthy{tool}``) to the ``IntelliKnow``
namespace every 60 seconds (see app/monitoring/publisher.py).

Usage (run once per environment, with credentials allowed to manage
CloudWatch alarms and SNS in the AWS China partition):

    python deploy/setup_cloudwatch_alarms.py \
        --region cn-north-1 \
        --alarm-email ops@example.com \
        --latency-threshold-ms 3000 \
        --error-rate-threshold-pct 5

This script only creates/updates alarms and a topic; it never deletes
resources.
"""

from __future__ import annotations

import argparse
import sys

import boto3

NAMESPACE = "IntelliKnow"
TOPIC_NAME = "intelliknow-alarms"
INTEGRATION_TOOLS = ("telegram", "teams")


def parse_args() -> argparse.Namespace:
    """Parse command-line options for region, thresholds, and notification email."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--region",
        default="cn-north-1",
        help="AWS China region (cn-north-1 or cn-northwest-1). Default: cn-north-1",
    )
    parser.add_argument(
        "--alarm-email",
        default=None,
        help="Email address to subscribe to the SNS alarm topic (optional; "
        "the subscription must be confirmed from the received email).",
    )
    parser.add_argument(
        "--latency-threshold-ms",
        type=float,
        default=3000.0,
        help="p95 latency alarm threshold in milliseconds. Default: 3000 (Req 13.3)",
    )
    parser.add_argument(
        "--error-rate-threshold-pct",
        type=float,
        default=5.0,
        help="Error-rate alarm threshold in percent. Default: 5 (Req 13.5)",
    )
    return parser.parse_args()


def ensure_topic(sns, email: str | None) -> str:
    """Create (or fetch) the SNS alarm topic and optionally subscribe an email.

    Returns the topic ARN. ``sns.create_topic`` is idempotent by name.
    """
    topic_arn = sns.create_topic(Name=TOPIC_NAME)["TopicArn"]
    if email:
        sns.subscribe(TopicArn=topic_arn, Protocol="email", Endpoint=email)
        print(f"Subscribed {email} to {topic_arn} (confirm via the email you receive).")
    return topic_arn


def put_alarms(cloudwatch, topic_arn: str, latency_ms: float, error_pct: float) -> None:
    """Create or update all IntelliKnow alarms, each notifying the SNS topic."""
    # 1. Latency p95 over 5 minutes (Req 13.3).
    cloudwatch.put_metric_alarm(
        AlarmName="IntelliKnow-QueryLatencyP95",
        AlarmDescription=(
            f"Query latency p95 exceeded {latency_ms:.0f} ms over 5 minutes"
        ),
        Namespace=NAMESPACE,
        MetricName="QueryLatencyMs",
        ExtendedStatistic="p95",
        Period=300,
        EvaluationPeriods=1,
        Threshold=latency_ms,
        ComparisonOperator="GreaterThanThreshold",
        TreatMissingData="notBreaching",
        AlarmActions=[topic_arn],
    )
    print(f"Alarm IntelliKnow-QueryLatencyP95: p95 > {latency_ms:.0f} ms / 5 min")

    # 2. Error rate over 5 minutes (Req 13.5).
    cloudwatch.put_metric_alarm(
        AlarmName="IntelliKnow-ErrorRate",
        AlarmDescription=(
            f"Error rate exceeded {error_pct:.1f}% over 5 minutes"
        ),
        Namespace=NAMESPACE,
        MetricName="ErrorRatePercent",
        Statistic="Average",
        Period=300,
        EvaluationPeriods=1,
        Threshold=error_pct,
        ComparisonOperator="GreaterThanThreshold",
        TreatMissingData="notBreaching",
        AlarmActions=[topic_arn],
    )
    print(f"Alarm IntelliKnow-ErrorRate: > {error_pct:.1f}% / 5 min")

    # 3. Integration health, one alarm per tool (Req 13.4).
    #    The app publishes IntegrationHealthy{tool} as 1 (healthy) / 0 (unhealthy)
    #    from its 5-minute health checks.
    for tool in INTEGRATION_TOOLS:
        metric = f"IntegrationHealthy{tool}"
        cloudwatch.put_metric_alarm(
            AlarmName=f"IntelliKnow-IntegrationHealth-{tool}",
            AlarmDescription=f"{tool} integration health check reported unhealthy",
            Namespace=NAMESPACE,
            MetricName=metric,
            Statistic="Minimum",
            Period=300,
            EvaluationPeriods=1,
            Threshold=1,
            ComparisonOperator="LessThanThreshold",
            TreatMissingData="breaching",  # no health datapoint = unhealthy
            AlarmActions=[topic_arn],
        )
        print(f"Alarm IntelliKnow-IntegrationHealth-{tool}: {metric} < 1 / 5 min")


def main() -> int:
    """Entry point: provision the SNS topic and all alarms in the given region."""
    args = parse_args()
    if not args.region.startswith("cn-"):
        print(
            f"warning: {args.region} is not an AWS China region; "
            "expected cn-north-1 or cn-northwest-1",
            file=sys.stderr,
        )

    session = boto3.session.Session(region_name=args.region)
    sns = session.client("sns")
    cloudwatch = session.client("cloudwatch")

    topic_arn = ensure_topic(sns, args.alarm_email)
    put_alarms(cloudwatch, topic_arn, args.latency_threshold_ms, args.error_rate_threshold_pct)
    print("Done. All alarms notify:", topic_arn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
