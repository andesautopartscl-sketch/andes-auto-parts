# Andes Assistant — arranque local (ERP + Gateway)
# No imprime secretos. No modifica .env.

param(
    [switch]$SkipErp,
    [switch]$SkipGateway
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    Write-Host "FATAL: missing .venv at $Py"
    exit 2
}

function Test-PortListen([int]$Port) {
    try {
        $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
        return [bool]$c
    } catch {
        return $false
    }
}

function Wait-HttpOk([string]$Url, [int]$Seconds = 30) {
    for ($i = 0; $i -lt $Seconds; $i++) {
        try {
            $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
            if ($r.StatusCode -eq 200) { return $true }
        } catch {}
        Start-Sleep -Seconds 1
    }
    return $false
}

Write-Host "ROOT=$Root"

# Load .env into process without printing values
$envPath = Join-Path $Root ".env"
if (Test-Path $envPath) {
    foreach ($line in Get-Content $envPath) {
        if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
        $k, $v = $line.Split('=', 2)
        $k = $k.Trim()
        $v = $v.Trim().Trim('"').Trim("'")
        if (-not $k) { continue }
        if (-not [string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable($k, "Process"))) { continue }
        Set-Item -Path "Env:$k" -Value $v
    }
    Write-Host "DOTENV=loaded"
} else {
    Write-Host "DOTENV=missing (using process env)"
}

$env:ANDES_ASSISTANT_NL_ENABLED = if ($env:ANDES_ASSISTANT_NL_ENABLED) { $env:ANDES_ASSISTANT_NL_ENABLED } else { "0" }
$env:ANDES_ORCH_PLANNER = if ($env:ANDES_ORCH_PLANNER) { $env:ANDES_ORCH_PLANNER } else { "fake" }
Write-Host "NL=$($env:ANDES_ASSISTANT_NL_ENABLED) ORCH=$($env:ANDES_ORCH_PLANNER) KEY_PRESENT=$([bool]$env:ANDES_LLM_API_KEY)"

if (-not $SkipErp) {
    if (Test-PortListen 5000) {
        Write-Host "ERP_PORT=5000 already listening (reuse)"
    } else {
        Write-Host "Starting ERP on :5000 ..."
        Start-Process -FilePath $Py -ArgumentList "run.py" -WorkingDirectory $Root -WindowStyle Hidden
    }
    if (-not (Wait-HttpOk "http://127.0.0.1:5000/health" 40)) {
        # Fallback: login page may work even if /health is old process
        if (-not (Wait-HttpOk "http://127.0.0.1:5000/" 5)) {
            Write-Host "FATAL: ERP health failed"
            exit 1
        }
        Write-Host "ERP_UP=true (root ok; /health may be unavailable on old process)"
    } else {
        Write-Host "ERP_UP=true"
    }
}

if (-not $SkipGateway) {
    if (Test-PortListen 5055) {
        Write-Host "GATEWAY_PORT=5055 already listening (reuse)"
    } else {
        Write-Host "Starting Gateway on :5055 ..."
        $env:PYTHONPATH = Join-Path $Root "andes_agent"
        Start-Process -FilePath $Py -ArgumentList "-m", "andes_agent" -WorkingDirectory (Join-Path $Root "andes_agent") -WindowStyle Hidden
    }
    if (-not (Wait-HttpOk "http://127.0.0.1:5055/health" 40)) {
        Write-Host "FATAL: Gateway health failed"
        exit 1
    }
    Write-Host "GATEWAY_UP=true"
}

Write-Host "READY"
exit 0
