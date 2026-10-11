#!/usr/bin/env bash
# scripts/aws_teardown.sh
# Deletes CloudFormation stack, empties S3 bucket, and deletes ECR repository.
set -euo pipefail

REGION="${AWS_REGION:-ap-southeast-2}"
STACK_NAME="wadenwait-prod"
REPO_NAME="wadenwait-prod"
FORCE=""

if [ $# -gt 0 ]; then
    if [[ "$1" =~ ^(af|il|ca|ap|eu|me|sa|us)-[a-z]+-[0-9]+$ ]]; then
        REGION="$1"
        STACK_NAME="${2:-wadenwait-prod}"
        REPO_NAME="$STACK_NAME"
        FORCE="${3:-}"
    else
        STACK_NAME="$1"
        REPO_NAME="${2:-$STACK_NAME}"
        FORCE="${3:-}"
    fi
fi

echo "=== AWS Teardown for $STACK_NAME in $REGION ==="
if [ "$FORCE" != "-y" ] && [ "$FORCE" != "--force" ]; then
    read -rp "Are you sure you want to permanently delete stack $STACK_NAME, S3 bucket, DynamoDB tables, and ECR images? [y/N] " CONFIRM
    if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
        echo "Teardown aborted."
        exit 0
    fi
fi

# 1. Empty S3 Bucket
echo "1. Checking S3 Photo Bucket..."
BUCKET_NAME=$(aws cloudformation describe-stack-resource \
    --stack-name "$STACK_NAME" \
    --logical-resource-id PhotoBucket \
    --region "$REGION" \
    --query "StackResourceDetail.PhysicalResourceId" \
    --output text 2>/dev/null || true)

if [ -n "$BUCKET_NAME" ] && [ "$BUCKET_NAME" != "None" ]; then
    echo "Emptying S3 bucket: $BUCKET_NAME..."
    aws s3 rm "s3://$BUCKET_NAME" --recursive --region "$REGION" || true
fi

# 2. Delete CloudFormation stack
echo "2. Deleting CloudFormation stack: $STACK_NAME..."
aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$REGION"
echo "Waiting for stack deletion..."
aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$REGION"

# 3. Delete ECR repo
echo "3. Deleting ECR repository: $REPO_NAME..."
aws ecr delete-repository --repository-name "$REPO_NAME" --region "$REGION" --force 2>/dev/null || true

echo "=== TEARDOWN COMPLETE ==="
echo "All AWS resources for $STACK_NAME have been deleted."
