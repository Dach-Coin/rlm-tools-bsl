<#
.SYNOPSIS
    Install or update rlm-tools-bsl from PyPI as a Windows service (HTTP MCP server).

.DESCRIPTION
    Downloads and installs rlm-tools-bsl from PyPI, registers it as a Windows
    service, starts the server, and verifies the health endpoint.

    If the service is already installed, stops it, upgrades the package,
    and restarts.

    Prerequisites (install before running):
      - Python 3.10+  https://python.org  (check "Add Python to PATH")
      - uv            https://docs.astral.sh/uv/

    Optional LLM env vars (for llm_query helper):
      Set system environment variables or pass --EnvFile:
        RLM_LLM_BASE_URL, RLM_LLM_API_KEY, RLM_LLM_MODEL  (OpenAI-compatible)
        ANTHROPIC_API_KEY                                    (Anthropic API)
      Without LLM keys all core features still work (find_module, grep, xml parsing).

    Must be run as Administrator.

.PARAMETER BindHost
    Host to bind the HTTP server (default: 127.0.0.1)

.PARAMETER Port
    Port for the HTTP server (default: 9000)

.PARAMETER EnvFile
    Path to .env file. If omitted, looks for .env in the script directory.

.PARAMETER NativeTls
    Use system TLS certificates instead of uv's built-in ones.
    Required in corporate networks where a proxy/firewall replaces TLS certificates.

.EXAMPLE
    PowerShell -ExecutionPolicy Bypass -File .\simple-install-from-pip.ps1

.EXAMPLE
    PowerShell -ExecutionPolicy Bypass -File .\simple-install-from-pip.ps1 -Port 9001 -EnvFile "C:\Users\me\.env"

.EXAMPLE
    PowerShell -ExecutionPolicy Bypass -File .\simple-install-from-pip.ps1 -NativeTls
#>

param(
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 9000,
    [string]$EnvFile = "",
    [switch]$NativeTls
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# --- Check Administrator ---
$currentPrincipal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Error "Run this script as Administrator (right-click -> Run as Administrator)."
    exit 1
}

# --- Check uv ---
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "uv not found. Install it:`n  PowerShell: irm https://astral.sh/uv/install.ps1 | iex`nThen re-run this script."
    exit 1
}

# --- Check Python ---
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "Python not found. Install Python 3.10+ from https://python.org (check 'Add Python to PATH')."
    exit 1
}

# --- Early PATH prepend: ensure uv tool bin dir is visible in this session ---
# Otherwise an existing rlm-tools-bsl installation may not be detected (and
# stop/uninstall would be skipped), even though `uv tool install` placed the
# binary in `uv tool dir --bin` long ago. Prepend BEFORE the existence check.
$uvBinDirEarly = (& uv tool dir --bin 2>$null)
if ($uvBinDirEarly -and (Test-Path $uvBinDirEarly)) {
    if (($env:PATH -split ';') -notcontains $uvBinDirEarly) {
        $env:PATH = "$uvBinDirEarly;$env:PATH"
    }
}

# --- Step 1: Stop and uninstall existing service if running ---
$isUpdate = $false
if (Get-Command rlm-tools-bsl -ErrorAction SilentlyContinue) {
    $isUpdate = $true
    Write-Host ""
    Write-Host "=== Existing installation detected -- upgrading ===" -ForegroundColor Cyan
    try {
        & rlm-tools-bsl service stop 2>$null
        Write-Host "Service stopped."
    } catch {
        Write-Host "Service was not running (OK)."
    }
    try {
        & rlm-tools-bsl service uninstall 2>$null
        Write-Host "Service uninstalled."
    } catch {
        Write-Host "Service was not installed (OK)."
    }
}

Write-Host ""
if ($isUpdate) {
    Write-Host "=== Step 1: Upgrade rlm-tools-bsl from PyPI ===" -ForegroundColor Cyan
} else {
    Write-Host "=== Step 1: Install rlm-tools-bsl from PyPI ===" -ForegroundColor Cyan
}

# Cleanup stale dist-info / .pth from global+user site-packages.
# Windows pythonservice.exe loads from global Python's site-packages, so a
# stale dist-info silently shadows the new wheel.
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
$exePath = if ($pythonCmd) { $pythonCmd.Source } else { "python" }
$globalSitePackages = & $exePath -c "import site; print(site.getsitepackages()[0])" 2>$null
$userSitePackages = & $exePath -c "import site; print(site.getusersitepackages())" 2>$null

foreach ($sp in @($globalSitePackages, $userSitePackages)) {
    if (-not $sp -or -not (Test-Path $sp)) { continue }
    Get-ChildItem -Path $sp -Directory -Filter "*rlm_tools_bsl*" -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Host "  Removing stale: $($_.FullName)" -ForegroundColor Yellow
        Remove-Item -Recurse -Force $_.FullName
    }
    Get-ChildItem -Path $sp -File -Filter "*rlm_tools_bsl*" -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Host "  Removing stale: $($_.FullName)" -ForegroundColor Yellow
        Remove-Item -Force $_.FullName
    }
}

# Force a fresh wheel pull from PyPI. `cache clean` drops the cached package but
# NOT uv's cached index metadata, which has its own TTL -- without --refresh a
# release published minutes ago can still resolve to the PREVIOUS version, and
# the two install steps below can then land different versions.
& uv cache clean rlm-tools-bsl

$uvInstallArgs = @("tool", "install", "rlm-tools-bsl[service]", "--force", "--reinstall", "--upgrade", "--refresh")
if ($NativeTls) { $uvInstallArgs += "--native-tls" }
& uv @uvInstallArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error "Installation failed. If you see TLS certificate errors, re-run with -NativeTls flag. For corporate networks behind a proxy that replaces TLS certificates, -NativeTls makes uv use system trust store."
    exit 1
}

# Also update the global Python that the Windows service uses.
# shutil.which() in _service_win.py finds this exe, not the uv tool one.
$globalPython = if ($exePath) { & $exePath -c "import sys; print(sys.executable)" 2>$null } else { "" }
if ($globalPython -and (Test-Path $globalPython)) {
    Write-Host "Updating global Python package ($globalPython)..." -ForegroundColor Cyan
    # --reinstall-package is required, not cosmetic: --upgrade only compares the
    # version NUMBER, so an install left over from simple-install.ps1 (same
    # version, but built from the local tree) satisfies the requirement and uv
    # reports "Audited" without replacing it -- the service would then run the
    # working tree while this script claims it installed the PyPI release.
    $uvPipArgs = @("pip", "install", "rlm-tools-bsl", "--upgrade", "--refresh", "--reinstall-package", "rlm-tools-bsl", "--python", $globalPython)
    if ($NativeTls) { $uvPipArgs += "--native-tls" }
    & uv @uvPipArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Global Python update failed - service may run an older version."
    }
}

# Ensure rlm-tools-bsl is in PATH for this session
if (-not (Get-Command rlm-tools-bsl -ErrorAction SilentlyContinue)) {
    Write-Host "Adding uv tool bin directory to PATH..." -ForegroundColor Yellow
    $uvBinDir = (& uv tool dir --bin 2>$null)
    if ($uvBinDir -and (Test-Path $uvBinDir)) {
        $env:PATH = "$uvBinDir;$env:PATH"
    }
    & uv tool update-shell 2>$null
}

Write-Host ""
Write-Host "=== Step 2: Register service ===" -ForegroundColor Cyan

$installArgs = @("service", "install", "--host", $BindHost, "--port", "$Port")

if ($EnvFile) {
    $installArgs += @("--env", $EnvFile)
} elseif (Test-Path (Join-Path $PSScriptRoot ".env")) {
    $resolvedEnv = (Resolve-Path (Join-Path $PSScriptRoot ".env")).Path
    Write-Host "Found .env: $resolvedEnv"
    $installArgs += @("--env", $resolvedEnv)
} else {
    Write-Host "No .env found - service will start without it (set LLM keys as system env vars if needed)."
}

& rlm-tools-bsl @installArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error "Service registration failed."
    exit 1
}

Write-Host ""
Write-Host "=== Step 3: Start service ===" -ForegroundColor Cyan
rlm-tools-bsl service start
if ($LASTEXITCODE -ne 0) {
    Write-Error "Service start failed."
    exit 1
}

Write-Host ""
Write-Host "=== Step 4: Verify ===" -ForegroundColor Cyan
Write-Host "Waiting for server to start (up to ~40s total on slower machines: 4 attempts x 10s)..."

# /health is lightweight (does not create an MCP session), /mcp is the real endpoint.
$healthUrl = "http://${BindHost}:${Port}/health"
$mcpUrl = "http://${BindHost}:${Port}/mcp"
$ok = $false
for ($attempt = 1; $attempt -le 4; $attempt++) {
    Start-Sleep -Seconds 10
    try {
        $response = Invoke-WebRequest -Uri $healthUrl -Method GET -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
        Write-Host "Server responding (HTTP $($response.StatusCode)). OK." -ForegroundColor Green
        $ok = $true
        break
    } catch {
        if ($null -ne $_.Exception.Response) {
            $code = [int]$_.Exception.Response.StatusCode
            Write-Host "Server responding (HTTP $code). OK." -ForegroundColor Green
            $ok = $true
            break
        }
        if ($attempt -lt 4) {
            Write-Host "  Attempt $attempt/4: not ready yet, retrying..." -ForegroundColor Yellow
        }
    }
}

if (-not $ok) {
    Write-Warning "Server not responding at $healthUrl after 4 attempts"
    Write-Warning "Check status: rlm-tools-bsl service status"
    Write-Warning "Logs:         ~/.config/rlm-tools-bsl/logs/server.log"
    exit 1
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host " Done! HTTP MCP server is running." -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Version:  $(& rlm-tools-bsl --version 2>&1)"
Write-Host "Endpoint: $mcpUrl"
Write-Host "Health:   $healthUrl"
Write-Host ""
Write-Host "Add to .claude.json / mcp.json:"
Write-Host ""
Write-Host "{`n  `"mcpServers`": {`n    `"rlm-tools-bsl`": {`n      `"type`": `"http`",`n      `"url`": `"$mcpUrl`"`n    }`n  }`n}"
Write-Host ""
Write-Host "Service management:"
Write-Host "  rlm-tools-bsl service status"
Write-Host "  rlm-tools-bsl service stop"
Write-Host "  rlm-tools-bsl service start"
Write-Host "  rlm-tools-bsl service uninstall"
Write-Host ""
Write-Host "Update to latest version:"
Write-Host "  PowerShell -ExecutionPolicy Bypass -File .\simple-install-from-pip.ps1"
