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

echo "Checking for EC2 instance in CloudFormation stack '$STACK_NAME' in region '$REGION'..."
INSTANCE_ID=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue | [0]" \
    --output text 2>/dev/null || true)

if [ -n "$INSTANCE_ID" ] && [ "$INSTANCE_ID" != "None" ]; then
    echo "Stopping EC2 instance: $INSTANCE_ID"
    aws ec2 stop-instances --instance-ids "$INSTANCE_ID" --region "$REGION" >/dev/null
    echo "EC2 instance stopped successfully. Compute charges are halted."
    echo "To resume: aws ec2 start-instances --instance-ids \"$INSTANCE_ID\" --region \"$REGION\""
    exit 0
fi

echo "Finding App Runner service for stack '$STACK_NAME' in region '$REGION'..."
SERVICE_ARN=$(aws apprunner list-services --region "$REGION" --query "ServiceSummaryList[?contains(ServiceName, '${STACK_NAME}')].ServiceArn | [0]" --output text 2>/dev/null || true)

if [ -n "$SERVICE_ARN" ] && [ "$SERVICE_ARN" != "None" ]; then
    echo "Pausing service: $SERVICE_ARN"
    aws apprunner pause-service --service-arn "$SERVICE_ARN" --region "$REGION" >/dev/null
    echo "App Runner service paused successfully. Instance compute charges are halted."
    echo "To resume: aws apprunner resume-service --service-arn \"$SERVICE_ARN\" --region \"$REGION\""
    exit 0
fi

echo "No EC2 instance or App Runner service found for stack '$STACK_NAME'."

