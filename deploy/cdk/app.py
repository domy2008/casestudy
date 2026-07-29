#!/usr/bin/env python3
"""CDK app entrypoint for IntelliKnow KMS — AWS China (Beijing, cn-north-1).

Deploys a single, self-contained demo stack. Keep it simple: one EC2 instance
running the Docker Compose stack, a security group, an IAM role for CloudWatch,
and the alarms + SNS topic. No VPC sprawl, no load balancer.

Deploy (only after configuring the aws-cn profile with rotated keys):

    cd deploy/cdk
    pip install -r requirements.txt
    cdk --profile aws-cn bootstrap
    cdk --profile aws-cn diff       # review first
    cdk --profile aws-cn deploy
"""

import os

import aws_cdk as cdk

from intelliknow_stack import IntelliKnowDemoStack

app = cdk.App()

# Admin IP allowed to reach the admin UI (8501) and SSH (22). Pass your own:
#   cdk deploy -c admin_cidr=1.2.3.4/32
admin_cidr = app.node.try_get_context("admin_cidr") or "0.0.0.0/0"

# Optional email for CloudWatch alarm notifications.
alarm_email = app.node.try_get_context("alarm_email")

IntelliKnowDemoStack(
    app,
    "IntelliKnowDemo",
    admin_cidr=admin_cidr,
    alarm_email=alarm_email,
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "cn-north-1"),
    ),
    description="IntelliKnow KMS demo — single EC2 + CloudWatch (AWS China Beijing)",
)

app.synth()
