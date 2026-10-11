#!/usr/bin/env bash
# scripts/aws_deploy.sh
# Deploy Wade or Wait to AWS App Runner, DynamoDB, S3, and OpenAI-compatible Vision API.
set -euo pipefail

REGION="${AWS_REGION:-ap-southeast-2}"
IMAGE_TAG="latest"
STACK_NAME="wadenwait-prod"
REPO_NAME="wadenwait-prod"
ALERT_EMAIL="${ALERT_EMAIL:-}"
DAILY_CAP="${DAILY_ANALYSIS_CAP:-100}"

if [ $# -gt 0 ]; then
    if [[ "$1" =~ ^(af|il|ca|ap|eu|me|sa|us)-[a-z]+-[0-9]+$ ]]; then
        REGION="$1"
        STACK_NAME="${2:-wadenwait-prod}"
        ALERT_EMAIL="${3:-${ALERT_EMAIL:-}}"
        REPO_NAME="$STACK_NAME"
    else
        IMAGE_TAG="$1"
        STACK_NAME="${2:-wadenwait-prod}"
        REPO_NAME="${3:-$STACK_NAME}"
    fi
fi

echo "=== 1. Checking Prerequisites (docker, aws, sts) ==="
if ! command -v aws >/dev/null 2>&1; then
    echo "Error: 'aws' CLI is required but not installed or not in PATH." >&2
    exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "Error: 'docker' CLI is required but not installed or not in PATH." >&2
    exit 1
fi

IDENTITY=$(aws sts get-caller-identity --output json) || {
    echo "Error: 'aws sts get-caller-identity' failed. Check your AWS credentials." >&2
    exit 1
}
ACCOUNT_ID=$(echo "$IDENTITY" | grep -o '"Account": "[^"]*' | cut -d'"' -f4)
CALLER_ARN=$(echo "$IDENTITY" | grep -o '"Arn": "[^"]*' | cut -d'"' -f4)
echo "Authenticated as: $CALLER_ARN"
echo "AWS Account ID:   $ACCOUNT_ID"
echo "Target Region:    $REGION"

echo -e "\n=== 2. Checking Vision API Environment Variables ==="
VISION_API_KEY="${VISION_API_KEY:-}"
VISION_API_BASE_URL="${VISION_API_BASE_URL:-}"
VISION_MODEL="${VISION_MODEL:-}"

if [ -z "$VISION_API_KEY" ]; then
    echo "Error: Environment variable VISION_API_KEY is required but not set." >&2
    exit 1
fi
if [ -z "$VISION_API_BASE_URL" ]; then
    echo "Error: Environment variable VISION_API_BASE_URL is required but not set." >&2
    exit 1
fi
if [ -z "$VISION_MODEL" ]; then
    echo "Error: Environment variable VISION_MODEL is required but not set." >&2
    exit 1
fi
echo "Vision API Base URL: $VISION_API_BASE_URL"
echo "Vision Model:        $VISION_MODEL"
echo "Vision API Key:      [CONFIGURED - HIDDEN]"

echo -e "\n=== 3. Checking ECR Repository ($REPO_NAME) ==="
if ! aws ecr describe-repositories --repository-names "$REPO_NAME" --region "$REGION" >/dev/null 2>&1; then
    echo "Creating ECR repository: $REPO_NAME"
    aws ecr create-repository --repository-name "$REPO_NAME" --region "$REGION" --image-scanning-configuration scanOnPush=true >/dev/null
else
    echo "ECR repository '$REPO_NAME' exists."
fi

ECR_REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
IMAGE_URI="${ECR_REGISTRY}/${REPO_NAME}:${IMAGE_TAG}"

echo -e "\n=== 4. Logging into ECR ==="
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$ECR_REGISTRY"

echo -e "\n=== 5. Building and Pushing Docker Image ==="
echo "Building image: $IMAGE_URI"
docker build --build-arg INCLUDE_DETECTOR=0 -t "$IMAGE_URI" .
docker push "$IMAGE_URI"

echo -e "\n=== 6. Deploying CloudFormation Stack ($STACK_NAME) ==="
# Notice: Never echo PARAMS or the secret key
PARAMS="ImageUri=$IMAGE_URI VisionApiBaseUrl=$VISION_API_BASE_URL VisionModel=$VISION_MODEL VisionApiKey=$VISION_API_KEY DailyAnalysisCap=$DAILY_CAP"
if [ -n "$ALERT_EMAIL" ]; then
    PARAMS="$PARAMS AlertEmail=$ALERT_EMAIL"
fi

aws cloudformation deploy \
    --template-file deploy/template.yaml \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
    --parameter-overrides $PARAMS

echo -e "\n=== 7. Deployment Outputs ==="
aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query "Stacks[0].Outputs" \
    --output table

SERVICE_URL=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='ServiceUrl'].OutputValue | [0]" \
    --output text 2>/dev/null || true)

if [ -n "$SERVICE_URL" ] && [ "$SERVICE_URL" != "None" ]; then
    echo -e "\nWaiting for application container to initialize (usually 30-45s)..."
    for i in {1..24}; do
        if curl -sf --connect-timeout 3 "$SERVICE_URL/health" >/dev/null 2>&1; then
            echo "Service is healthy and responding!"
            break
        fi
        sleep 5
    done

    echo ""
    echo "================================================================="
    echo "SUCCESS! Wade or Wait is live at: $SERVICE_URL"
    echo "Test health endpoint: $SERVICE_URL/health"
    echo "================================================================="
fi

