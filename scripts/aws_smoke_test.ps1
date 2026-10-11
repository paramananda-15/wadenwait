# scripts/aws_smoke_test.ps1
# Smoke test for deployed AWS App Runner service or local Wade or Wait server.
[CmdletBinding()]
param(
    [Alias("BaseUrl")]
    [string]$ServiceUrl = "",
    [string]$StackName = "wade-or-wait",
    [string]$Region = $(if ($env:AWS_REGION) { $env:AWS_REGION } else { "ap-southeast-2" })
)

$ErrorActionPreference = "Stop"

if (-not $ServiceUrl) {
    Write-Host "Looking up ServiceUrl from CloudFormation stack '$StackName'..." -ForegroundColor Cyan
    try {
        $outputsJson = aws cloudformation describe-stacks `
            --stack-name $StackName `
            --region $Region `
            --query "Stacks[0].Outputs" `
            --output json 2>$null
        $outputs = $outputsJson | ConvertFrom-Json
        $urlObj = $outputs | Where-Object { $_.OutputKey -eq "ServiceUrl" }
        if ($urlObj) {
            $ServiceUrl = $urlObj.OutputValue.TrimEnd("/")
        }
    } catch {
        Write-Host "Could not automatically resolve ServiceUrl from CloudFormation." -ForegroundColor Yellow
    }
}

if (-not $ServiceUrl) {
    Write-Host "Defaulting to local test: http://127.0.0.1:8000" -ForegroundColor Yellow
    $ServiceUrl = "http://127.0.0.1:8000"
}

$ServiceUrl = $ServiceUrl.TrimEnd("/")
Write-Host "`nTarget Service URL: $ServiceUrl" -ForegroundColor Cyan

# -------------------------------------------------------------
# 1. Health Checks
# -------------------------------------------------------------
Write-Host "`n=== 1. Testing /health & /api/health ===" -ForegroundColor Cyan
try {
    $healthResp = Invoke-RestMethod -Uri "$ServiceUrl/health" -Method Get
    Write-Host "  GET /health: OK -> $(ConvertTo-Json -Compress $healthResp)" -ForegroundColor Green

    $apiHealthResp = Invoke-RestMethod -Uri "$ServiceUrl/api/health" -Method Get
    Write-Host "  GET /api/health: OK -> $(ConvertTo-Json -Compress $apiHealthResp)" -ForegroundColor Green
} catch {
    Write-Error "Health check failed: $_"
    exit 1
}

# -------------------------------------------------------------
# 2. Upload and Submit Report
# -------------------------------------------------------------
Write-Host "`n=== 2. Testing Photo Upload & Report Submission ===" -ForegroundColor Cyan
try {
    $uploadUrlInfo = Invoke-RestMethod -Uri "$ServiceUrl/upload-url" -Method Post
    $uploadKey = $uploadUrlInfo.key
    Write-Host "  Generated Upload Key: $uploadKey" -ForegroundColor Gray

    # Create a small valid test JPEG in memory
    $testJpegBytes = [byte[]]@(
        0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01,
        0x01, 0x01, 0x00, 0x60, 0x00, 0x60, 0x00, 0x00, 0xFF, 0xDB, 0x00, 0x43,
        0x00, 0x08, 0x06, 0x06, 0x07, 0x06, 0x05, 0x08, 0x07, 0x07, 0x07, 0x09,
        0x09, 0x08, 0x0A, 0x0C, 0x14, 0x0D, 0x0C, 0x0B, 0x0B, 0x0C, 0x19, 0x12,
        0x13, 0x0F, 0x14, 0x1D, 0x1A, 0x1F, 0x1E, 0x1D, 0x1A, 0x1C, 0x1C, 0x20,
        0x24, 0x2E, 0x27, 0x20, 0x22, 0x2C, 0x23, 0x1C, 0x1C, 0x28, 0x37, 0x29,
        0x2C, 0x30, 0x31, 0x34, 0x34, 0x34, 0x1F, 0x27, 0x39, 0x3D, 0x38, 0x32,
        0x3C, 0x2E, 0x33, 0x34, 0x32, 0xFF, 0xC0, 0x00, 0x0B, 0x08, 0x00, 0x01,
        0x00, 0x01, 0x01, 0x01, 0x11, 0x00, 0xFF, 0xC4, 0x00, 0x1F, 0x00, 0x00,
        0x01, 0x05, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
        0x09, 0x0A, 0x0B, 0xFF, 0xDA, 0x00, 0x08, 0x01, 0x01, 0x00, 0x00, 0x3F,
        0x00, 0xBF, 0x00, 0xFF, 0xD9
    )

    $tempFilePath = [System.IO.Path]::GetTempFileName() + ".jpg"
    [System.IO.File]::WriteAllBytes($tempFilePath, $testJpegBytes)

    # Upload using curl.exe or multipart
    $uploadTargetUrl = if ($uploadUrlInfo.url.StartsWith("http")) { $uploadUrlInfo.url } else { "$ServiceUrl$($uploadUrlInfo.url)" }
    Write-Host "  Uploading test photo to: $uploadTargetUrl" -ForegroundColor Gray

    if ($uploadUrlInfo.url.StartsWith("http")) {
        # Presigned S3 PUT
        Invoke-RestMethod -Uri $uploadTargetUrl -Method Put -InFile $tempFilePath -ContentType "image/jpeg"
    } else {
        # Multipart POST to /upload
        curl.exe -s -F "key=$uploadKey" -F "file=@$tempFilePath;type=image/jpeg" "$ServiceUrl/upload" | Out-Null
    }
    Remove-Item -Path $tempFilePath -Force

    # Submit report
    $reportPayload = @{
        key = $uploadKey
        lat = 12.9716
        lng = 77.5946
        accuracy = 4.2
        mode = "suv"
        capturedAt = [int][double]::Parse((Get-Date -UFormat %s))
    } | ConvertTo-Json

    $reportResp = Invoke-RestMethod -Uri "$ServiceUrl/reports" -Method Post -Body $reportPayload -ContentType "application/json"
    Write-Host "  Report Submitted! Verdicts:" -ForegroundColor Green
    Write-Host "    Perception Mode: $($reportResp.perception_mode)" -ForegroundColor Gray
    Write-Host "    Depth CM:        $($reportResp.depth_cm -join ' - ')" -ForegroundColor Gray
    Write-Host "    Verdicts:        $(ConvertTo-Json -Compress $reportResp.verdicts)" -ForegroundColor Green
} catch {
    Write-Error "Upload or report submission failed: $_"
    exit 1
}

# -------------------------------------------------------------
# 3. Fetch Map Polling Updates
# -------------------------------------------------------------
Write-Host "`n=== 3. Testing /map/updates Polling ===" -ForegroundColor Cyan
try {
    $bbox = "77.50,12.90,77.70,13.10"
    $updatesResp = Invoke-RestMethod -Uri "$ServiceUrl/map/updates?bbox=$bbox&since=0&mode=suv" -Method Get
    Write-Host "  Server Time:     $($updatesResp.server_time)" -ForegroundColor Green
    Write-Host "  Changes Count:   $($updatesResp.changes.Count)" -ForegroundColor Green
    if ($updatesResp.changes.Count -gt 0) {
        $first = $updatesResp.changes[0]
        Write-Host "  Sample Change:   ReportId=$($first.report_id) Status=$($first.status) Label='$($first.label)'" -ForegroundColor Green
    }
} catch {
    Write-Error "Failed to fetch /map/updates: $_"
    exit 1
}

Write-Host "`n=== ALL SMOKE TESTS PASSED SUCCESSFULLY! ===" -ForegroundColor Green
