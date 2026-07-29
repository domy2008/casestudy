"""IntelliKnow KMS demo stack — one EC2 instance + CloudWatch on AWS China.

Deliberately minimal for a demo:
  - a tiny VPC (one public subnet, no NAT gateway — no recurring NAT cost),
  - one Amazon Linux 2023 EC2 instance that installs Docker and runs the stack
    via user-data (reusing deploy/ec2_bootstrap.sh's logic),
  - a security group locked to an admin CIDR for the UI/SSH,
  - an IAM instance role scoped to CloudWatch metrics + logs,
  - an SNS topic and the three CloudWatch alarms the app's metrics feed.

It creates only NEW resources and never references existing EC2 instances.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
)
from constructs import Construct

# Must match app/monitoring/publisher.py and deploy/setup_cloudwatch_alarms.py.
NAMESPACE = "IntelliKnow"
INTEGRATION_TOOLS = ("telegram", "teams")


class IntelliKnowDemoStack(cdk.Stack):
    """A single-instance demo deployment of IntelliKnow KMS."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        admin_cidr: str,
        alarm_email: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Network: minimal public VPC, no NAT (keeps the demo cheap) -----
        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=1,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                )
            ],
        )

        sg = ec2.SecurityGroup(
            self,
            "InstanceSg",
            vpc=vpc,
            description="IntelliKnow demo — admin UI, Teams webhook, SSH",
            allow_all_outbound=True,
        )
        admin = ec2.Peer.ipv4(admin_cidr)
        sg.add_ingress_rule(admin, ec2.Port.tcp(22), "SSH (admin only)")
        sg.add_ingress_rule(admin, ec2.Port.tcp(8501), "Admin UI (admin only)")
        # Teams Bot Framework must reach the webhook; TLS should terminate in
        # front of this in a real deployment. Open for the demo.
        sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(8000), "Teams webhook")

        # --- IAM: least-privilege role for CloudWatch metrics + logs --------
        role = iam.Role(
            self,
            "InstanceRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            description="IntelliKnow EC2 — CloudWatch metrics/logs publishing",
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "cloudwatch:PutMetricData",
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogStreams",
                ],
                resources=["*"],
            )
        )

        # --- Compute: one AL2023 instance, Docker installed via user-data ---
        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            "set -euxo pipefail",
            "dnf update -y",
            "dnf install -y docker git",
            "systemctl enable --now docker",  # boot recovery (Req 12.3)
            # Docker Compose v2 plugin.
            "mkdir -p /usr/libexec/docker/cli-plugins",
            "curl -sSL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 "
            "-o /usr/libexec/docker/cli-plugins/docker-compose",
            "chmod +x /usr/libexec/docker/cli-plugins/docker-compose",
            # Persistent data layout + host-only env placeholder (Req 12.4, 11.1).
            "mkdir -p /opt/intelliknow/data/{faiss,uploads,credentials,logbuffer}",
            "test -f /opt/intelliknow/.env || printf "
            "'CREDENTIAL_MASTER_KEY=\\nTELEGRAM_PROXY_URL=\\nAWS_DEFAULT_REGION=cn-north-1\\n' "
            "> /opt/intelliknow/.env",
            "chmod 600 /opt/intelliknow/.env",
            "usermod -aG docker ec2-user || true",
            # Fill in /opt/intelliknow/.env, then:
            #   git clone <repo> && cd intelliknow-kms && docker compose up -d --build
        )

        instance = ec2.Instance(
            self,
            "Instance",
            vpc=vpc,
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.T3, ec2.InstanceSize.MEDIUM
            ),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(),
            security_group=sg,
            role=role,
            user_data=user_data,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/xvda",
                    volume=ec2.BlockDeviceVolume.ebs(
                        20, volume_type=ec2.EbsDeviceVolumeType.GP3
                    ),
                )
            ],
        )

        # --- Monitoring: SNS topic + the three alarms the app feeds ---------
        topic = sns.Topic(self, "AlarmTopic", display_name="IntelliKnow demo alarms")
        if alarm_email:
            topic.add_subscription(subs.EmailSubscription(alarm_email))
        alarm_action = cw_actions.SnsAction(topic)

        latency = cloudwatch.Metric(
            namespace=NAMESPACE,
            metric_name="QueryLatencyMs",
            statistic="p95",
            period=cdk.Duration.minutes(5),
        )
        latency.create_alarm(
            self,
            "LatencyP95Alarm",
            alarm_name="IntelliKnow-QueryLatencyP95",
            threshold=3000,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        ).add_alarm_action(alarm_action)

        error_rate = cloudwatch.Metric(
            namespace=NAMESPACE,
            metric_name="ErrorRatePercent",
            statistic="Average",
            period=cdk.Duration.minutes(5),
        )
        error_rate.create_alarm(
            self,
            "ErrorRateAlarm",
            alarm_name="IntelliKnow-ErrorRate",
            threshold=5,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        ).add_alarm_action(alarm_action)

        for tool in INTEGRATION_TOOLS:
            metric = cloudwatch.Metric(
                namespace=NAMESPACE,
                metric_name=f"IntegrationHealthy{tool}",
                statistic="Minimum",
                period=cdk.Duration.minutes(5),
            )
            metric.create_alarm(
                self,
                f"IntegrationHealth{tool.capitalize()}Alarm",
                alarm_name=f"IntelliKnow-IntegrationHealth-{tool}",
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
            ).add_alarm_action(alarm_action)

        # --- Outputs --------------------------------------------------------
        cdk.CfnOutput(self, "InstancePublicIp", value=instance.instance_public_ip)
        cdk.CfnOutput(self, "AdminUiUrl", value=f"http://{instance.instance_public_ip}:8501")
        cdk.CfnOutput(self, "AlarmTopicArn", value=topic.topic_arn)
