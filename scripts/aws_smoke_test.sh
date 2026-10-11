#!/usr/bin/env bash
# scripts/aws_smoke_test.sh
# Smoke test for deployed AWS App Runner service or local server.
set -euo pipefail

SERVICE_URL="${1:-}"
if [[ "$SERVICE_URL" == --service-url=* ]] || [[ "$SERVICE_URL" == --base-url=* ]]; then
    SERVICE_URL="${SERVICE_URL#*=}"
elif [[ "$SERVICE_URL" == "--service-url" ]] || [[ "$SERVICE_URL" == "--base-url" ]]; then
    SERVICE_URL="${2:-}"
fi
STACK_NAME="${2:-wade-or-wait}"
REGION="${AWS_REGION:-ap-southeast-2}"

if [ -z "$SERVICE_URL" ]; then
    echo "Looking up ServiceUrl from CloudFormation stack '$STACK_NAME'..."
    SERVICE_URL=$(aws cloudformation describe-stacks \
        --stack-name "$STACK_NAME" \
        --region "$REGION" \
        --query "Stacks[0].Outputs[?OutputKey=='ServiceUrl'].OutputValue | [0]" \
        --output text 2>/dev/null || true)
fi

if [ -z "$SERVICE_URL" ] || [ "$SERVICE_URL" = "None" ]; then
    echo "Defaulting to local test: http://127.0.0.1:8000"
    SERVICE_URL="http://127.0.0.1:8000"
fi

SERVICE_URL="${SERVICE_URL%/}"
echo "Target Service URL: $SERVICE_URL"

echo -e "\n=== 1. Testing /health & /api/health ==="
curl -sf "$SERVICE_URL/health"
echo ""
curl -sf "$SERVICE_URL/api/health"
echo ""

echo -e "\n=== 2. Testing Photo Upload & Report Submission ==="
UPLOAD_INFO=$(curl -sf -X POST "$SERVICE_URL/upload-url")
KEY=$(echo "$UPLOAD_INFO" | grep -o '"key":"[^"]*' | cut -d'"' -f4)
TARGET_URL=$(echo "$UPLOAD_INFO" | grep -o '"url":"[^"]*' | cut -d'"' -f4)

TMP_IMG=$(mktemp --suffix=.jpg)
# 1x1 JPEG
echo "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA=" | base64 -d > "$TMP_IMG"


if [[ "$TARGET_URL" == http* ]]; then
    curl -sf -L -X PUT -H "Content-Type: image/jpeg" --data-binary @"$TMP_IMG" "$TARGET_URL"
else
    curl -sf -F "key=$KEY" -F "file=@$TMP_IMG;type=image/jpeg" "$SERVICE_URL/upload"
fi
rm -f "$TMP_IMG"

REPORT_JSON=$(curl -sf -X POST "$SERVICE_URL/reports" \
    -H "Content-Type: application/json" \
    -d "{\"key\":\"$KEY\",\"lat\":12.9716,\"lng\":77.5946,\"accuracy\":4.2,\"mode\":\"suv\"}")
echo "Report response: $REPORT_JSON"

echo -e "\n=== 3. Testing /map/updates Polling ==="
UPDATES=$(curl -sf "$SERVICE_URL/map/updates?bbox=77.50,12.90,77.70,13.10&since=0&mode=suv")
echo "Map updates response: $UPDATES"

echo -e "\n=== ALL SMOKE TESTS PASSED! ==="
