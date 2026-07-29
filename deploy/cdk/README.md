# IntelliKnow KMS — CDK (AWS China, Beijing `cn-north-1`)

A minimal, single-stack demo deployment: one EC2 instance running the Docker
Compose stack, plus CloudWatch alarms and an SNS topic. It creates only new
resources and never touches existing EC2 instances in the account.

## What it creates

- A small VPC (one public subnet, **no NAT gateway** — no recurring NAT cost)
- One Amazon Linux 2023 `t3.medium` EC2 instance (Docker installed via user-data)
- A security group: SSH + admin UI (8501) restricted to your CIDR; Teams webhook (8000) open
- An IAM instance role scoped to `cloudwatch:PutMetricData` and `logs:*`
- SNS topic + 3 CloudWatch alarms (latency p95, error rate, per-tool integration health)

## Prerequisites

1. **Rotate the leaked keys first**, then store the new AWS China key under a profile:

   ```ini
   # ~/.aws/credentials
   [aws-cn]
   aws_access_key_id = <new key>
   aws_secret_access_key = <new secret>
   ```

2. Node + AWS CDK CLI installed (`npm i -g aws-cdk`), and Python deps:

   ```bash
   cd deploy/cdk
   python3 -m venv .venv && . .venv/bin/activate
   pip install -r requirements.txt
   ```

## Deploy

```bash
export CDK_DEFAULT_REGION=cn-north-1
cdk --profile aws-cn bootstrap
cdk --profile aws-cn diff  -c admin_cidr=<your.ip>/32 -c alarm_email=you@example.com
cdk --profile aws-cn deploy -c admin_cidr=<your.ip>/32 -c alarm_email=you@example.com
```

`cdk diff` shows exactly what will be created — review it before `deploy`.

## After deploy

1. SSH to the instance, fill in `/opt/intelliknow/.env` (`CREDENTIAL_MASTER_KEY`, `TELEGRAM_PROXY_URL`).
2. `git clone <repo> && cd intelliknow-kms && docker compose up -d --build`
3. Open the admin UI at the `AdminUiUrl` output.

## Tear down

```bash
cdk --profile aws-cn destroy
```

> Note: `admin_cidr` defaults to `0.0.0.0/0` if not set — fine for a quick demo,
> but pass your own `/32` since the admin UI has no auth of its own.
