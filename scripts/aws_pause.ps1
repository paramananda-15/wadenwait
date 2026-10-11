# scripts/aws_pause.ps1
# Pauses the AWS App Runner service to stop instance hours when not testing.
[CmdletBinding()]
param(
    [string]$Region = $(if ($env:AWS_REGION) { $env:AWS_REGION } else { "ap-southeast-2" }),
    [string]$StackName = "wade-or-wait"
)

$ErrorActionPreference = "Stop"

Write-Host "Finding App Runner service for stack '$StackName' in region '$Region'..." -ForegroundColor Cyan

$servicesJson = aws apprunner list-services --region $Region --output json
$services = ($servicesJson | ConvertFrom-Json).ServiceSummaryList

$targetService = $services | Where-Object { $_.ServiceName -like "$StackName*" }
if (-not $targetService) {
    Write-Error "No App Runner service found matching name '$StackName*' in region '$Region'."
    exit 1
}

$serviceArn = $targetService.ServiceArn
Write-Host "Pausing service: $($targetService.ServiceName) (Status: $($targetService.Status))..." -ForegroundColor Yellow
aws apprunner pause-service --service-arn $serviceArn --region $Region | Out-Null

Write-Host "Pause request sent successfully! App Runner service is transitioning to PAUSED." -ForegroundColor Green
Write-Host "Instance compute charges are halted. To resume later, run:" -ForegroundColor Cyan
Write-Host "  aws apprunner resume-service --service-arn `"$serviceArn`" --region $Region"
