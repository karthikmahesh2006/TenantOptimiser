#!/usr/bin/env bash
set -euo pipefail

# Deploy script for Alloc Platform to AWS EC2 host via SSM
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Source resource variables if present
STATE_FILE="$REPO_ROOT/infra/state/resources.env"
if [ -f "$STATE_FILE" ]; then
    # shellcheck disable=SC1090
    source "$STATE_FILE"
fi

BUCKET="${BUCKET:-}"
IID="${IID:-}"

if [ -z "$BUCKET" ] || [ -z "$IID" ]; then
    echo "ERROR: BUCKET and IID must be defined in infra/state/resources.env or exported as environment variables." >&2
    exit 1
fi

echo "=== [1/4] Packaging repository archive ==="
TMP_TGZ="/tmp/latest.tgz"
tar czf "$TMP_TGZ" -C "$REPO_ROOT" --exclude='.git' --exclude='infra/state' --exclude='.venv' --exclude='__pycache__' --exclude='.pytest_cache' .

echo "=== [2/4] Uploading to s3://${BUCKET}/releases/latest.tgz ==="
aws s3 cp "$TMP_TGZ" "s3://${BUCKET}/releases/latest.tgz"
rm -f "$TMP_TGZ"

echo "=== [3/4] Executing remote install on instance $IID via AWS SSM ==="
CID=$(aws ssm send-command \
    --instance-ids "$IID" \
    --document-name "AWS-RunShellScript" \
    --parameters 'commands=["/usr/local/bin/alloc-install"]' \
    --query "Command.CommandId" \
    --output text)

echo "SSM Command dispatched: $CID. Awaiting execution..."
aws ssm wait command-executed --command-id "$CID" --instance-id "$IID"

STATUS=$(aws ssm get-command-invocation \
    --command-id "$CID" \
    --instance-id "$IID" \
    --query "Status" \
    --output text)

OUTPUT=$(aws ssm get-command-invocation \
    --command-id "$CID" \
    --instance-id "$IID" \
    --query "StandardOutputContent" \
    --output text)

STDERR=$(aws ssm get-command-invocation \
    --command-id "$CID" \
    --instance-id "$IID" \
    --query "StandardErrorContent" \
    --output text)

echo "Remote install status: $STATUS"
echo "--- Remote Output ---"
echo "$OUTPUT"

if [ "$STATUS" != "Success" ]; then
    echo "ERROR: Remote installation failed on instance $IID!" >&2
    echo "--- Remote Stderr ---" >&2
    echo "$STDERR" >&2
    exit 1
fi

echo "=== [4/4] Smoke test API status endpoint via SSM ==="
SMOKE_CID=$(aws ssm send-command \
    --instance-ids "$IID" \
    --document-name "AWS-RunShellScript" \
    --parameters 'commands=["curl -s -f http://127.0.0.1:8000/api/v1/status"]' \
    --query "Command.CommandId" \
    --output text)

aws ssm wait command-executed --command-id "$SMOKE_CID" --instance-id "$IID"
SMOKE_STATUS=$(aws ssm get-command-invocation \
    --command-id "$SMOKE_CID" \
    --instance-id "$IID" \
    --query "Status" \
    --output text)

SMOKE_OUTPUT=$(aws ssm get-command-invocation \
    --command-id "$SMOKE_CID" \
    --instance-id "$IID" \
    --query "StandardOutputContent" \
    --output text)

if [ "$SMOKE_STATUS" != "Success" ]; then
    echo "ERROR: Smoke test GET /api/v1/status failed!" >&2
    exit 1
fi

echo "Smoke test passed. Response: $SMOKE_OUTPUT"
echo "=== Deployment Succeeded! ==="
