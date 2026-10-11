# scripts/aws_teardown.ps1
# Deletes the CloudFormation stack, empties/deletes S3 bucket, and deletes ECR repository.
[CmdletBinding()]
param(
    [string]$Region = $(if ($env:AWS_REGION) { $env:AWS_REGION } else { "ap-southeast-2" }),
    [string]$StackName = "wade-or-wait",
    [string]$RepoName = "wade-or-wait",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

Write-Host "=== AWS Teardown for $StackName in $Region ===" -ForegroundColor Yellow
if (-not $Force) {
    $confirm = Read-Host "Are you sure you want to permanently delete the stack, DynamoDB tables, S3 bucket contents, and ECR images? [y/N]"
    if ($confirm -ne 'y' -and $confirm -ne 'Y') {
        Write-Host "Teardown aborted by user." -ForegroundColor Gray
        exit 0
    }
}

$deletedItems = @()

# 1. Empty S3 Bucket before deleting stack
Write-Host "`n1. Checking S3 Photo Bucket..." -ForegroundColor Cyan
try {
    $bucketRes = aws cloudformation describe-stack-resource `
        --stack-name $StackName `
        --logical-resource-id PhotoBucket `
        --region $Region `
        --output json 2>$null | ConvertFrom-Json

    if ($bucketRes -and $bucketRes.StackResourceDetail.PhysicalResourceId) {
        $bucketName = $bucketRes.StackResourceDetail.PhysicalResourceId
        Write-Host "Emptying S3 bucket: $bucketName..." -ForegroundColor Yellow
        aws s3 rm "s3://$bucketName" --recursive --region $Region
        $deletedItems += "S3 Bucket contents ($bucketName)"
    }
} catch {
    Write-Host "Note: Bucket not found or stack already being deleted." -ForegroundColor Gray
}

# 2. Delete CloudFormation Stack
Write-Host "`n2. Deleting CloudFormation stack: $StackName..." -ForegroundColor Cyan
aws cloudformation delete-stack --stack-name $StackName --region $Region
Write-Host "Waiting for stack deletion to complete (this may take 2-4 minutes)..."
aws cloudformation wait stack-delete-complete --stack-name $StackName --region $Region
$deletedItems += "CloudFormation stack: $StackName"
$deletedItems += "App Runner Service, IAM Roles, CloudWatch Log Group"
$deletedItems += "DynamoDB Tables (ReportsTable, VotesTable, ClearTimeLogTable)"
$deletedItems += "S3 PhotoBucket"

# 3. Delete ECR Repository and container images
Write-Host "`n3. Deleting ECR Repository: $RepoName..." -ForegroundColor Cyan
try {
    aws ecr delete-repository --repository-name $RepoName --region $Region --force | Out-Null
    $deletedItems += "ECR repository & images: $RepoName"
} catch {
    Write-Host "Note: ECR repository already deleted or not found." -ForegroundColor Gray
}

Write-Host "`n=== TEARDOWN COMPLETE ===" -ForegroundColor Green
Write-Host "Deleted resources:" -ForegroundColor Green
foreach ($item in $deletedItems) {
    Write-Host "  - $item" -ForegroundColor Green
}
Write-Host "All AWS resources for Wade or Wait have been completely removed. No remaining spend." -ForegroundColor Cyan
