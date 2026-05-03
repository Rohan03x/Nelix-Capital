<#
.SYNOPSIS
  DCF Dashboard dev helper.

USAGE
  .\dev.ps1              # restart Flask (kills stale port-5000 processes first)
  .\dev.ps1 -Check AMZN  # restart + quick API health-check for a ticker
  .\dev.ps1 -Test        # run pytest suite then start server
  .\dev.ps1 -KillOnly    # just kill any port-5000 process, don't restart
#>
param(
    [string]$Check   = "",
    [switch]$Test,
    [switch]$KillOnly
)

Set-Location $PSScriptRoot

function Kill-Port5000 {
    $found = netstat -ano | Select-String ":5000 "
    $pids2 = $found |
        ForEach-Object { ($_ -split '\s+')[-1] } |
        Where-Object { $_ -match '^\d+$' } |
        Sort-Object -Unique

    if ($pids2) {
        Write-Host "  Killing PIDs on :5000 -> $($pids2 -join ', ')" -ForegroundColor Yellow
        foreach ($p in $pids2) {
            try { Stop-Process -Id ([int]$p) -Force -ErrorAction SilentlyContinue } catch {}
        }
        Start-Sleep -Milliseconds 500
        Write-Host "  Port 5000 cleared." -ForegroundColor Green
    } else {
        Write-Host "  Port 5000 is free." -ForegroundColor Green
    }
}

Write-Host ""
Write-Host "=== DCF Dev Helper ===" -ForegroundColor Cyan
Kill-Port5000

if ($KillOnly) { exit 0 }

& ".\.venv\Scripts\Activate.ps1" 2>$null

if ($Test) {
    Write-Host ""
    Write-Host "Running test suite..." -ForegroundColor Cyan
    python -m pytest tests/ -q --tb=short
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Tests FAILED -- server not started." -ForegroundColor Red
        exit 1
    }
    Write-Host "All tests passed." -ForegroundColor Green
}

Write-Host ""
Write-Host "Starting Flask on http://127.0.0.1:5000 ..." -ForegroundColor Cyan

$job = Start-Job -ScriptBlock {
    Set-Location $using:PSScriptRoot
    & ".\.venv\Scripts\python.exe" run.py 2>&1
}

$ready = $false
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $tcp = [System.Net.Sockets.TcpClient]::new("127.0.0.1", 5000)
        $tcp.Close()
        $ready = $true
        break
    } catch {}
}

if (-not $ready) {
    Write-Host "Server did not start in time. Last output:" -ForegroundColor Red
    Receive-Job $job | Select-Object -Last 20
    exit 1
}
Write-Host "  Flask is up." -ForegroundColor Green

function Test-Ticker([string]$ticker) {
    Write-Host ""
    Write-Host "Health-check: $ticker ..." -ForegroundColor Cyan
    try {
        $raw  = (Invoke-WebRequest "http://127.0.0.1:5000/api/dashboard/$ticker" -UseBasicParsing).Content
        $json = $raw | ConvertFrom-Json
        Write-Host ("  source        : " + $json.data_quality.source)
        Write-Host ("  data_freshness: " + $json.data_freshness)
        Write-Host ("  IV            : `$" + $json.intrinsic_value) -ForegroundColor Cyan
        Write-Host ("  price         : `$" + $json.price)
        Write-Host ("  upside        : " + $json.upside_pct + "%")
        Write-Host ("  years         : " + ($json.historical.years -join ", "))
        Write-Host ("  confidence    : " + $json.confidence_score + "/100")
    } catch {
        Write-Host ("  ERROR: " + $_) -ForegroundColor Red
    }
}

if ($Check) { Test-Ticker $Check }

Write-Host ""
Write-Host "Server running. Streaming logs (Ctrl+C to stop):" -ForegroundColor Yellow
Write-Host ""
while ($true) {
    $out = Receive-Job $job
    if ($out) { $out | ForEach-Object { Write-Host $_ } }
    Start-Sleep -Milliseconds 300
}
