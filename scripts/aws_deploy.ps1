# scripts/aws_deploy.ps1
# Deploy Wade or Wait to AWS App Runner, DynamoDB, S3, and OpenAI-compatible Vision API.
[CmdletBinding()]
param(
    [string]$Region = $(if ($env:AWS_REGION) { $env:AWS_REGION } else { "ap-southeast-2" }),
    [string]$ImageTag = "latest",
    [string]$StackName = "wade-or-wait",
    [string]$RepoName = "wade-or-wait",
    [string]$AlertEmail = "",
    [int]$DailyCap = 100
)

$ErrorActionPreference = "Stop"

Write-Host "=== 1. Checking Prerequisites (docker, aws, sts) ===" -ForegroundColor Cyan
if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
    Write-Error "Error: 'aws' CLI is required but not installed or not in PATH."
    exit 1
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error "Error: 'docker' CLI is required but not installed or not in PATH."
    exit 1
}

try {
    $identityJson = aws sts get-caller-identity --output json
    $identity = $identityJson | ConvertFrom-Json
    $AccountId = $identity.Account
    $CallerArn = $identity.Arn
    Write-Host "Authenticated as: $CallerArn" -ForegroundColor Green
    Write-Host "AWS Account ID:   $AccountId" -ForegroundColor Green
    Write-Host "Target Region:    $Region" -ForegroundColor Green
} catch {
    Write-Error "Failed to verify AWS credentials with 'aws sts get-caller-identity'. Please configure AWS credentials before deploying."
    exit 1
}

Write-Host "`n=== 2. Checking Vision API Environment Variables ===" -ForegroundColor Cyan
$visionApiKey = $env:VISION_API_KEY
$visionBaseUrl = $env:VISION_API_BASE_URL
$visionModel = $env:VISION_MODEL

if (-not $visionApiKey) {
    Write-Error "Error: Environment variable VISION_API_KEY is required but not set."
    exit 1
}
if (-not $visionBaseUrl) {
    Write-Error "Error: Environment variable VISION_API_BASE_URL is required but not set."
    exit 1
}
if (-not $visionModel) {
    Write-Error "Error: Environment variable VISION_MODEL is required but not set."
    exit 1
}

Write-Host "Vision API Base URL: $visionBaseUrl" -ForegroundColor Green
Write-Host "Vision Model:        $visionModel" -ForegroundColor Green
Write-Host "Vision API Key:      [CONFIGURED - HIDDEN]" -ForegroundColor Green

Write-Host "`n=== 3. Checking ECR Repository ($RepoName) ===" -ForegroundColor Cyan
$repoCheck = aws ecr describe-repositories --repository-names $RepoName --region $Region 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "Creating ECR repository: $RepoName" -ForegroundColor Yellow
    aws ecr create-repository --repository-name $RepoName --region $Region --image-scanning-configuration scanOnPush=true | Out-Null
} else {
    Write-Host "ECR repository '$RepoName' exists." -ForegroundColor Green
}

$EcrRegistry = "$AccountId.dkr.ecr.$Region.amazonaws.com"
$ImageUri = "$EcrRegistry/$RepoName`:$ImageTag"

Write-Host "`n=== 4. Logging into ECR ===" -ForegroundColor Cyan
aws ecr get-login-password --region $Region | docker login --username AWS --password-stdin $EcrRegistry
if ($LASTEXITCODE -ne 0) {
    Write-Error "ECR docker login failed."
    exit 1
}

Write-Host "`n=== 5. Building and Pushing Docker Image ===" -ForegroundColor Cyan
Write-Host "Building Docker image: $ImageUri (INCLUDE_DETECTOR=0)"
docker build --build-arg INCLUDE_DETECTOR=0 -t $ImageUri .
if ($LASTEXITCODE -ne 0) {
    Write-Error "Docker build failed."
    exit 1
}

Write-Host "Pushing Docker image to ECR..."
docker push $ImageUri
if ($LASTEXITCODE -ne 0) {
    Write-Error "Docker push failed."
    exit 1
}

Write-Host "`n=== 6. Deploying CloudFormation Stack ($StackName) ===" -ForegroundColor Cyan
$paramOverrides = @(
    "ImageUri=$ImageUri",
    "VisionApiBaseUrl=$visionBaseUrl",
    "VisionModel=$visionModel",
    "VisionApiKey=$visionApiKey",
    "DailyAnalysisCap=$DailyCap"
)
if ($AlertEmail) {
    $paramOverrides += "AlertEmail=$AlertEmail"
}

aws cloudformation deploy `
    --template-file deploy/template.yaml `
    --stack-name $StackName `
    --region $Region `
    --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND `
    --parameter-overrides $paramOverrides

if ($LASTEXITCODE -ne 0) {
    Write-Error "CloudFormation deployment failed."
    exit 1
}

Write-Host "`n=== 7. Deployment Outputs ===" -ForegroundColor Green
$outputsJson = aws cloudformation describe-stacks `
    --stack-name $StackName `
    --region $Region `
    --query "Stacks[0].Outputs" `
    --output json

$outputs = $outputsJson | ConvertFrom-Json
$outputs | Format-Table -AutoSize

$serviceUrlObj = $outputs | Where-Object { $_.OutputKey -eq "ServiceUrl" }
if ($serviceUrlObj) {
    Write-Host "`n=================================================================" -ForegroundColor Cyan
    Write-Host "SUCCESS! Wade or Wait is live at: $($serviceUrlObj.OutputValue)" -ForegroundColor Cyan
    Write-Host "Test health endpoint: $($serviceUrlObj.OutputValue)/health" -ForegroundColor Cyan
    Write-Host "=================================================================" -ForegroundColor Cyan
}
