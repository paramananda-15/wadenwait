#!/usr/bin/env bash
# scripts/aws_pause.sh
# Pauses the AWS App Runner service to stop instance hours when not testing.
set -euo pipefail

REGION="${AWS_REGION:-ap-southeast-2}"
STACK_NAME="wadenwait-prod"

if [ $# -gt 0 ]; then
    if [[ "$1" =~ ^(af|il|ca|ap|eu|me|sa|us)-[a-z]+-[0-9]+$ ]]; then
        REGION="$1"
        STACK_NAME="${2:-wadenwait-prod}"
    else
        STACK_NAME="$1"
        REGION="${2:-$REGION}"
    fi
fi

echo "Finding App Runner service for stack '$STACK_NAME' in region '$REGION'..."
SERVICE_ARN=$(aws apprunner list-services --region "$REGION" --query "ServiceSummaryList[?contains(ServiceName, '${STACK_NAME}')].ServiceArn | [0]" --output text)

if [ -z "$SERVICE_ARN" ] || [ "$SERVICE_ARN" = "None" ]; then
    echo "Error: No App Runner service found matching '${STACK_NAME}' in region '$REGION'."
    exit 1
fi

echo "Pausing service: $SERVICE_ARN"
aws apprunner pause-service --service-arn "$SERVICE_ARN" --region "$REGION" >/dev/null
echo "App Runner service paused successfully. Instance compute charges are halted."
echo "To resume: aws apprunner resume-service --service-arn \"$SERVICE_ARN\" --region \"$REGION\""
