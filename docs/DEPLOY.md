# AWS Deployment Guide: Wade or Wait

This guide details how to deploy **Wade or Wait** to AWS within the AWS Free Tier and promotional credit limits with zero extra spending.

---

## 1. Target Architecture & Cost Drivers

The deployment uses a lean, serverless-oriented footprint designed to avoid idle VPC, NAT Gateway, or Load Balancer costs:

- **Compute**: One container running on **AWS App Runner** (min 1, max 2 instances; smallest size `0.25 vCPU`, `0.5 GB` RAM). HTTPS and TLS termination are managed directly by App Runner.
  - *Cost driver*: Active container vCPU-hours and memory-hours, plus provisioned memory when idle.
- **Database**: **Amazon DynamoDB** with On-Demand billing (`PAY_PER_REQUEST`). No provisioned capacity minimums.
  - *Cost driver*: Read/Write request units (RRU / WRU). Local reads filter expired TTL items in memory to eliminate write-on-read expenses.
- **Object Storage**: **Amazon S3** (Private bucket, Block All Public Access enabled, CORS rule enabled for presigned PUT/GET, Lifecycle rule expiring objects after 14 days).
  - *Cost driver*: Storage GB-months and PUT/GET API requests. Objects expire automatically after 14 days. Images are served via presigned GET URLs.
- **Vision Perception**: Generic OpenAI-compatible chat completions API (`LLM_PROVIDER=openai_compat`) configured with `VISION_API_BASE_URL`, `VISION_API_KEY`, and `VISION_MODEL` (e.g., `gpt-4o-mini`).
  - *Cost driver*: External API calls and token count. Controlled via image resizing (long edge $\le 1024$ px JPEG), image-hash result caching, and an atomic DynamoDB daily cap (`DAILY_ANALYSIS_CAP=100`).
- **Monitoring & Logs**: **Amazon CloudWatch Logs** with a 7-day retention policy.
  - *Cost driver*: Ingestion GB and log storage GB.

> [!NOTE]
> No VPC connectors, no NAT Gateways, no Elastic Load Balancers (ALB/NLB), no RDS instances, and no SageMaker endpoints are deployed. Default region is `ap-southeast-2`.

---

## 2. Account Hygiene & Preparation

### 2.1 Identity and Access Management
- **Never use root credentials**: Do not generate access keys for the AWS root account.
- **Enable MFA**: Enforce Multi-Factor Authentication (FIDO2 or authenticator app) on your root account and administrative users.
- **Least-Privilege Deployer**: Deploy using an IAM User or AWS IAM Identity Center (SSO) with appropriate permissions for CloudFormation, ECR, App Runner, DynamoDB, S3, IAM, and CloudWatch.

### 2.2 Billing & Free Tier Check
1. In the AWS Console, open **Billing and Cost Management**.
2. Go to **Billing** $\rightarrow$ **Credits** to confirm promotional credit balance and eligible services.
3. Check **Free Tier** usage to monitor current consumption.

### 2.3 Configure OpenAI-Compatible Vision API
Ensure you have an API key and base URL for any OpenAI-compatible vision model:
- `VISION_API_BASE_URL`: Base URL (e.g. `https://api.openai.com/v1`).
- `VISION_API_KEY`: Secret API key (never committed, never logged, passed with `NoEcho: true`).
- `VISION_MODEL`: Model identifier (e.g. `gpt-4o-mini`).
- The system connects with temperature `0.0`, request timeout `25s`, at most one retry, and structured JSON parsing.

### 2.4 Set AWS Budgets Alerts
The CloudFormation template (`deploy/template.yaml`) can automatically create budget alerts when given the `AlertEmail` parameter. Alternatively, you can create budget alerts via the AWS CLI:

```bash
aws budgets create-budget \
    --account-id $(aws sts get-caller-identity --query Account --output text) \
    --budget file://scripts/budget-10usd.json \
    --notifications-with-subscribers file://scripts/notifications-10usd.json
```

Or configure via Console:
1. Open **AWS Budgets** $\rightarrow$ **Create budget**.
2. Select **Cost budget** $\rightarrow$ Monthly $\rightarrow$ Fixed ($10, $25, and $50).
3. Set notification email address for threshold alerts at 100% of budgeted amount.

---

## 3. Database Schema & Index Design

DynamoDB uses three tables with On-Demand billing and TTL enabled:

### 3.1 Table: `wade-reports`
Stores flood reports partitioned spatially using geohash-5 cells (~4.9 km $\times$ 4.9 km) to allow fast queries without scanning.

- **Partition Key**: `cell` (String) - e.g. `tdr1v` (Geohash precision 5).
- **Sort Key**: `sk` (String):
  - Ephemeral active record: `REPORT#<id>` (includes `expiresAt` attribute set to `now + 14 days`).
  - History record: `HIST#<id>` (omits `expiresAt`, kept for historical analysis).
- **Global Secondary Index (GSI)**: `IdIndex`
  - Partition Key: `id` (String)
  - Projection: `ALL`
  - Used for $O(1)$ single-report lookups (`get_report(id)`).
- **TTL Attribute**: `expiresAt` (Unix epoch seconds).
- **Read-Time Filtering**: Because DynamoDB TTL deletion can lag by up to 48 hours, all read queries explicitly filter out items where `expiresAt < current_timestamp`.
- **Zero-Write Read Semantics**: `changed_since` and `nearby` compute `STALE` and `REPORTED_CLEAR` statuses dynamically at query time, incurring zero DynamoDB write costs on polling.
- **Daily VLM Cap Item**: Atomic counter item stored with `cell = "DAILY_CAP"` and `sk = "DATE#YYYY-MM-DD"`, incremented atomically with conditional check `< DAILY_ANALYSIS_CAP`.

### 3.2 Table: `wade-votes`
Stores crowd confirmation votes with a strict uniqueness guarantee.

- **Partition Key**: `report_id` (String)
- **Sort Key**: `device_id` (String)
- **Attributes**: `vote` ("still_flooded" | "cleared"), `created_at`, `expiresAt`.
- **Enforcement**: Votes are inserted with `ConditionExpression="attribute_not_exists(report_id) AND attribute_not_exists(device_id)"` to enforce exactly one vote per device per report without distributed locks.

### 3.3 Table: `wade-clear-time-log`
Stores physics-based clearance time predictions and logs actual clearance timestamps.

- **Partition Key**: `id` (String, UUID)
- **Global Secondary Indexes**:
  - `RoadIndex`: Partition Key `road_id` (String), Sort Key `issued_at` (Number).
  - `CellIndex`: Partition Key `cell_key` (String), Sort Key `issued_at` (Number).
- **TTL Attribute**: `expiresAt` (14 days).

---

## 4. Container & Detector Configuration

### 4.1 Image Size Optimization
The Dockerfile (`Dockerfile`) includes a build argument `INCLUDE_DETECTOR=0|1`:
- **Default (`INCLUDE_DETECTOR=0`)**:
  - Does not install PyTorch or Torchvision.
  - Image size: **~220 MB**.
  - `PERCEPTION_MODE=vlm` uses Bedrock Converse API exclusively.
  - Explanation image and fusion degrade gracefully if detector is unavailable.
- **With Detector (`INCLUDE_DETECTOR=1`)**:
  - Installs CPU-only PyTorch and preloads MobileNetV3 SSD weights.
  - Image size: **~850 MB**.

### 4.2 Rate Limiting
- An in-memory sliding-window rate limiter is included for local use and single instances.
- In App Runner, traffic is load-balanced across up to 2 instances; rate limits are per-instance and approximate.
- Client IP is extracted safely from the leftmost IP in the `X-Forwarded-For` header.

---

## 5. Deployment Procedures

### 5.1 Prerequisites
- Docker running locally (Docker Desktop or Docker in WSL).
- AWS CLI v2 installed (`aws --version`).
- Verify credentials:
  ```powershell
  aws sts get-caller-identity
  ```

### 5.2 Deploying with PowerShell (`scripts/aws_deploy.ps1`)
Set your OpenAI-compatible Vision API credentials and run the deployment script:

```powershell
$env:VISION_API_KEY = "your-api-key"
$env:VISION_API_BASE_URL = "https://api.openai.com/v1"
$env:VISION_MODEL = "gpt-4o-mini"

.\scripts\aws_deploy.ps1 `
    -Region "ap-southeast-2" `
    -ImageTag "latest" `
    -StackName "wade-or-wait" `
    -RepoName "wade-or-wait" `
    -AlertEmail "your-email@example.com" `
    -DailyCap 100
```

*(Or on Linux/macOS/WSL using bash)*:
```bash
export VISION_API_KEY="your-api-key"
export VISION_API_BASE_URL="https://api.openai.com/v1"
export VISION_MODEL="gpt-4o-mini"

./scripts/aws_deploy.sh latest wade-or-wait wade-or-wait
```

The script will:
1. Verify pre-flight dependencies (`docker`, `aws`, `aws sts get-caller-identity`).
2. Verify that `VISION_API_KEY`, `VISION_API_BASE_URL`, and `VISION_MODEL` are set.
3. Create the Amazon ECR repository if missing.
4. Build and push the container image to ECR.
5. Deploy `deploy/template.yaml` via CloudFormation.
6. Print the live HTTPS App Runner service URL.

---

## 6. Verification and Smoke Testing

Verify the deployed service using the automated smoke test script. The script accepts either `-ServiceUrl` or `-BaseUrl` (or resolves the URL automatically from the stack name):

```powershell
# Using ServiceUrl
.\scripts\aws_smoke_test.ps1 -ServiceUrl "https://xxxxxx.ap-southeast-2.awsapprunner.com"

# Using BaseUrl alias
.\scripts\aws_smoke_test.ps1 -BaseUrl "https://xxxxxx.ap-southeast-2.awsapprunner.com"

# Or automatically looking up the stack in ap-southeast-2
.\scripts\aws_smoke_test.ps1 -StackName "wade-or-wait" -Region "ap-southeast-2"
```

*(Or using bash)*:
```bash
./scripts/aws_smoke_test.sh "https://xxxxxx.ap-southeast-2.awsapprunner.com"
# or lookup from stack
./scripts/aws_smoke_test.sh "" wade-or-wait
```

The smoke test validates:
1. `GET /health` and `GET /api/health` return HTTP 200 OK.
2. `POST /upload-url` returns an upload key and presigned URL.
3. Photo upload completes successfully.
4. `POST /reports` generates flood verdicts.
5. `GET /map/updates` returns server timestamps and pollable changes.

---

## 7. Pausing and Resuming (Cost Protection)

When you are not testing or demonstrating the application, pause the App Runner service to stop instance computing charges:

### 7.1 Pause Service
```powershell
.\scripts\aws_pause.ps1 -Region "ap-southeast-2" -StackName "wade-or-wait"
```
*(Or bash)*:
```bash
./scripts/aws_pause.sh wade-or-wait
```

### 7.2 Resume Service
To resume the service when needed:
```bash
aws apprunner resume-service --service-arn "<SERVICE_ARN>" --region "ap-southeast-2"
```

---

## 8. Complete Teardown

To permanently delete all AWS resources and eliminate all continuing costs:

```powershell
.\scripts\aws_teardown.ps1 -StackName "wade-or-wait" -Region "ap-southeast-2"
```

*(Or bash)*:
```bash
./scripts/aws_teardown.sh wade-or-wait wade-or-wait
```

The teardown script will:
1. Prompt for confirmation.
2. Empty all uploaded objects from the private S3 bucket.
3. Delete the CloudFormation stack (App Runner service, DynamoDB tables, S3 bucket, IAM roles, and CloudWatch log groups).
4. Delete the Amazon ECR repository and container images.
5. Print a summary of deleted resources.

---

## 9. Demo Video Screenshot Checklist

For your demo video or presentation, capture screenshots of the following AWS Console pages:

1. **AWS App Runner $\rightarrow$ Services**: Showing service status *Running* (or *Paused*), environment configuration (`LLM_PROVIDER=openai_compat`, `VISION_API_BASE_URL`, `VISION_MODEL`, `VISION_API_KEY` hidden), instance size `0.25 vCPU / 0.5 GB`, and min 1 / max 2 auto-scaling.
2. **Amazon DynamoDB $\rightarrow$ Tables**: Showing `wade-reports` items with `cell` (geohash), `sk` (`REPORT#` and `HIST#`), and `expiresAt` TTL attributes enabled.
3. **Amazon S3 $\rightarrow$ Buckets**: Showing private bucket properties (*Block all public access: On*), CORS configuration (*AllowedMethods: GET, PUT, HEAD*), and Lifecycle configuration rule (*14-day expiration*).
4. **Amazon CloudWatch $\rightarrow$ Log groups**: Showing App Runner service log group with `Retention: 7 days`.
