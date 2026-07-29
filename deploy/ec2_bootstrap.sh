#!/usr/bin/env bash
# IntelliKnow KMS — one-time EC2 host bootstrap for AWS China (Amazon Linux 2023).
#
# Prepares a fresh EC2 instance to run the Docker Compose stack:
#   - installs Docker + the compose plugin
#   - enables the Docker daemon at boot (systemd) so all containers with
#     restart: unless-stopped come back within 5 minutes of OS boot (Req 12.3)
#   - creates the persistent data directory and the host-only .env file
#     placeholder (Req 12.4, 11.1)
#
# Run as root (or via sudo) once, then follow deploy/DEPLOYMENT.md.

set -euo pipefail

echo "==> Installing Docker"
dnf install -y docker docker-compose-plugin || {
    # Fallback for hosts where the compose plugin package name differs.
    dnf install -y docker
    DOCKER_CONFIG=/usr/lib/docker/cli-plugins
    mkdir -p "$DOCKER_CONFIG"
    echo "Install the compose plugin manually if 'docker compose' is unavailable." >&2
}

echo "==> Enabling Docker at boot (systemd) — required for Req 12.3"
systemctl enable --now docker

echo "==> Creating persistent data layout under /opt/intelliknow"
mkdir -p /opt/intelliknow/data/{faiss,uploads,credentials,logbuffer}

if [ ! -f /opt/intelliknow/.env ]; then
    echo "==> Creating placeholder host-only env file (fill in real values!)"
    install -m 600 /dev/null /opt/intelliknow/.env
    cat > /opt/intelliknow/.env <<'EOF'
# Host-only secrets — never commit this file (Req 11.1).
# See .env.example in the repository for documentation of each variable.
CREDENTIAL_MASTER_KEY=
TELEGRAM_PROXY_URL=
AWS_DEFAULT_REGION=cn-north-1
EOF
    echo "    -> Edit /opt/intelliknow/.env and set CREDENTIAL_MASTER_KEY and TELEGRAM_PROXY_URL"
else
    echo "==> /opt/intelliknow/.env already exists; leaving it untouched"
fi

# Non-root operators in the docker group can manage the stack.
if id ec2-user >/dev/null 2>&1; then
    usermod -aG docker ec2-user
fi

echo "==> Bootstrap complete."
echo "Next steps:"
echo "  1. Fill in /opt/intelliknow/.env"
echo "  2. Attach an instance profile allowing cloudwatch:PutMetricData,"
echo "     logs:*, and (for alarm setup) cloudwatch:PutMetricAlarm + sns:*"
echo "  3. From the repo root: docker compose up -d --build"
echo "  4. Provision alarms: python deploy/setup_cloudwatch_alarms.py --region cn-north-1"
