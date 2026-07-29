<#
.SYNOPSIS
    Install or upgrade rlm-tools-bsl from local source as a Windows service.

.DESCRIPTION
    Single idempotent script for both fresh installs and upgrades.

    Behavior:
      - Fresh install: builds from current source tree, registers and starts
        the service, verifies /health.
      - Upgrade (re-run after `git pull`): stops existing service, cleans
        stale uv cache and dangling site-packages artifacts, rebuilds and
        restarts.

    Prerequisites (install before running):
      - Python 3.10+  https://python.org  (check "Add Python to PATH")
      - uv            https://docs.astral.sh/uv/

    Optional LLM env vars (for llm_query helper):
      Create .env next to this script, or set system environment variables:
        RLM_LLM_BASE_URL, RLM_LLM_API_KEY, RLM_LLM_MODEL  (OpenAI-compatible)
        ANTHROPIC_API_KEY                                  (Anthropic API)
      Without LLM keys all core features still work (find_module, grep, xml parsing).

    Must be run as Administrator.

    For PyPI-based install use simple-install-from-pip.ps1 instead.

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
    PowerShell -ExecutionPolicy Bypass -File .\simple-install.ps1

.EXAMPLE
    PowerShell -ExecutionPolicy Bypass -File .\simple-install.ps1 -EnvFile "C:\Users\me\.env" -Port 9001

.EXAMPLE
    PowerShell -ExecutionPolicy Bypass -File .\simple-install.ps1 -NativeTls
#>

param(
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 9000,
    [string]$EnvFile = "",
    [switch]$NativeTls
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# --- Pre-checks: Administrator ---
$currentPrincipal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Run this script as Administrator (right-click -> Run as Administrator)."
    exit 1
}

# --- Pre-checks: uv ---
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "uv not found. Install it:`n  PowerShell: irm https://astral.sh/uv/install.ps1 | iex`nThen re-run this script."
    exit 1
}

# --- Pre-checks: Python ---
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

# --- Detect mode (fresh vs upgrade) ---
$existing = Get-Command rlm-tools-bsl -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Upgrade detected (existing rlm-tools-bsl found at $($existing.Source))." -ForegroundColor Cyan
} else {
    Write-Host "Fresh install (no existing rlm-tools-bsl on PATH)." -ForegroundColor Cyan
}

Write-Host ""
Write-Host "=== Step 1: Stop & uninstall existing service (if any) ===" -ForegroundColor Cyan
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

Write-Host ""
Write-Host "=== Step 2: Clean stale installs & rebuild ===" -ForegroundColor Cyan

# Remove dangling ~*rlm_tools_bsl* dirs / dist-info / .pth in user and global
# site-packages. These can shadow the correct version after reinstall.
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

# Remove stale dist/ from source tree (can confuse uv)
$distDir = Join-Path $PSScriptRoot "dist"
if (Test-Path $distDir) {
    Write-Host "  Removing stale dist/: $distDir" -ForegroundColor Yellow
    Remove-Item -Recurse -Force $distDir
}

# --upgrade and --refresh keep this path symmetric with simple-install-from-pip.ps1.
# Without --upgrade an already-installed dependency that still satisfies the
# constraints is left untouched, so a rebuild from source could leave the service
# on an outdated dependency. --refresh also drops uv's cached index metadata and
# the cached local build, which `cache clean` alone does not cover.
& uv cache clean rlm-tools-bsl
$uvInstallArgs = @("tool", "install", "${PSScriptRoot}[service]", "--force", "--reinstall", "--upgrade", "--refresh")
if ($NativeTls) { $uvInstallArgs += "--native-tls" }
& uv @uvInstallArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error "Build failed."
    exit 1
}

# Also update the global Python that the Windows service uses.
# shutil.which() in _service_win.py finds this exe, not the uv tool one.
$globalPython = & $exePath -c "import sys; print(sys.executable)" 2>$null
if ($globalPython -and (Test-Path $globalPython)) {
    Write-Host "Updating global Python package ($globalPython)..." -ForegroundColor Cyan
    # --reinstall-package mirrors simple-install-from-pip.ps1: --upgrade compares
    # only the version NUMBER, so an install of the same version coming from PyPI
    # satisfies the requirement and uv skips it, leaving the service on the
    # published wheel instead of the local build this script is meant to deploy.
    $uvPipArgs = @("pip", "install", $PSScriptRoot, "--upgrade", "--refresh", "--reinstall-package", "rlm-tools-bsl", "--python", $globalPython)
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
Write-Host "=== Step 3: Register service ===" -ForegroundColor Cyan

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
Write-Host "=== Step 4: Start service ===" -ForegroundColor Cyan
& rlm-tools-bsl service start
if ($LASTEXITCODE -ne 0) {
    Write-Error "Service start failed."
    exit 1
}

Write-Host ""
Write-Host "=== Step 5: Verify ===" -ForegroundColor Cyan
Write-Host "Waiting for server to start (up to ~40s total on slower machines: 4 attempts x 10s)..."

# Use /health (lightweight, does not create an MCP session) instead of /mcp.
$url = "http://${BindHost}:${Port}/health"
$ok = $false
for ($attempt = 1; $attempt -le 4; $attempt++) {
    Start-Sleep -Seconds 10
    try {
        $response = Invoke-WebRequest -Uri $url -Method GET -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
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
    Write-Warning "Server not responding at $url after 4 attempts"
    Write-Warning "Check status: rlm-tools-bsl service status"
    Write-Warning "Logs:         ~/.config/rlm-tools-bsl/logs/server.log"
    exit 1
}

$mcpUrl = "http://${BindHost}:${Port}/mcp"
Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host " Done! HTTP MCP server is running." -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Version:  $(& rlm-tools-bsl --version 2>&1)"
Write-Host "Endpoint: $mcpUrl"
Write-Host "Health:   $url"
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
Write-Host "Logs: ~/.config/rlm-tools-bsl/logs/server.log"
Write-Host ""
Write-Host "Re-run this script after `git pull` to upgrade." -ForegroundColor Cyan
