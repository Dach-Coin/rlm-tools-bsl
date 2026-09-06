<#
.SYNOPSIS
    Diagnose rlm-tools-bsl Windows service install / start failures.

.DESCRIPTION
    Collects everything needed to debug "Service did not respond in time" (1053)
    and similar issues: Python/uv versions, uv tool env layout, service registry
    entries, ImagePath, DLL presence next to pythonservice.exe, import probe via
    uv tool Python, service.json, server.log tail, Event Log entries, and
    optionally a short debug-mode run of pythonservice.exe.

    Writes one directory per run under .\tmp\diagnose\diagnose-<timestamp>\
    plus a zip alongside it. Does NOT read .env files or API keys. server.log
    may contain project paths of the user -- review before sharing.

.PARAMETER OutDir
    Output directory (default: .\tmp\diagnose next to the script).

.PARAMETER EventLogDays
    How many days back to scan Application/System event logs (default: 3).

.PARAMETER RunDebug
    Additionally start pythonservice.exe in -debug mode for 5 seconds to capture
    import/startup errors. Requires Administrator. May briefly occupy the service port.

.EXAMPLE
    PowerShell -ExecutionPolicy Bypass -File .\diagnose-service-win.ps1

.EXAMPLE
    PowerShell -ExecutionPolicy Bypass -File .\diagnose-service-win.ps1 -RunDebug
#>

param(
    [string]$OutDir = "",
    [int]$EventLogDays = 3,
    [switch]$RunDebug
)

$ErrorActionPreference = "Continue"
Set-StrictMode -Off

# --- Output layout ---
if (-not $OutDir) {
    $OutDir = Join-Path $PSScriptRoot "tmp\diagnose"
}
New-Item -ItemType Directory -Path $OutDir -Force | Out-Null

$stamp   = Get-Date -Format "yyyyMMdd-HHmmss"
$workDir = Join-Path $OutDir "diagnose-$stamp"
New-Item -ItemType Directory -Path $workDir -Force | Out-Null

$summaryPath = Join-Path $workDir "00-summary.txt"
$summary     = New-Object System.Collections.Generic.List[string]

function Add-Summary { param([string]$s) $summary.Add($s); Write-Host $s }

function Save-Section {
    param([string]$Name, [scriptblock]$Body)
    $file = Join-Path $workDir "$Name.txt"
    $header = "=== $Name ==="
    Write-Host ""
    Write-Host $header -ForegroundColor Cyan
    $captured = try {
        & $Body 2>&1 | Out-String
    } catch {
        "ERROR: $($_.Exception.Message)`r`n$($_.ScriptStackTrace)"
    }
    ($header + "`r`n" + $captured) | Set-Content -Path $file -Encoding UTF8
    return $captured
}

# --- Header ---
Add-Summary "rlm-tools-bsl Windows service diagnostics"
Add-Summary "Timestamp:  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Add-Summary "Script dir: $PSScriptRoot"
Add-Summary "Output dir: $workDir"

$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin   = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Add-Summary "Admin:      $isAdmin"
if (-not $isAdmin) {
    Add-Summary "WARNING: not admin -- HKLM registry and some Event Log queries will be empty."
}
Add-Summary ""

# --- Config path actually in use ---
# The SERVICE reads the path from its own registry Environment value, so a diagnostic
# that always looks in %USERPROFILE% would collect a different service.json and a
# different server.log than the ones the service is using.
function Get-RlmConfigPath {
    $fromRegistry = $null
    try {
        $key = Get-Item "HKLM:\SYSTEM\CurrentControlSet\Services\rlm-tools-bsl" -ErrorAction Stop
        foreach ($entry in @($key.GetValue("Environment"))) {
            if ($entry -and $entry -match '^(?i)RLM_CONFIG_FILE=(.+)$') { $fromRegistry = $Matches[1] }
        }
    } catch {
        $fromRegistry = $null
    }
    if ($fromRegistry) {
        if ([System.IO.Path]::IsPathRooted($fromRegistry)) { return $fromRegistry }
        # Releases before 1.35.0 could store the override verbatim. SCM starts the
        # service from the Windows system directory, so resolve a legacy relative value
        # from the same base instead of the diagnostic script's current directory.
        return (Join-Path ([System.Environment]::SystemDirectory) $fromRegistry)
    }
    if ($env:RLM_CONFIG_FILE) {
        if ([System.IO.Path]::IsPathRooted($env:RLM_CONFIG_FILE)) { return $env:RLM_CONFIG_FILE }
        return (Join-Path (Get-Location).Path $env:RLM_CONFIG_FILE)
    }
    return (Join-Path $env:USERPROFILE ".config" | Join-Path -ChildPath "rlm-tools-bsl" | Join-Path -ChildPath "service.json")
}

$rlmConfigPath = Get-RlmConfigPath
$rlmConfigDir = Split-Path -Parent $rlmConfigPath
$rlmLogPath = Join-Path $rlmConfigDir "logs" | Join-Path -ChildPath "server.log"
Add-Summary ("Config path in use:        " + $rlmConfigPath)

# --- 01 System info ---
Save-Section "01-system" {
    "PSVersion:      $($PSVersionTable.PSVersion)"
    "PSEdition:      $($PSVersionTable.PSEdition)"
    "CLR:            $($PSVersionTable.CLRVersion)"
    $os = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue
    if ($os) {
        "OS:             $($os.Caption)"
        "Version:        $($os.Version)  Build $($os.BuildNumber)"
    }
    "Architecture:   $env:PROCESSOR_ARCHITECTURE"
    "UserName:       $env:USERNAME"
    "UserProfile:    $env:USERPROFILE"
    "APPDATA:        $env:APPDATA"
    "LOCALAPPDATA:   $env:LOCALAPPDATA"
    "Admin:          $isAdmin"
} | Out-Null

# --- 02 Python + uv ---
$pythonPath = $null
Save-Section "02-python-uv" {
    $pc = Get-Command python -ErrorAction SilentlyContinue
    if ($pc) {
        "python (where):   $($pc.Source)"
        $script:pythonPath = $pc.Source
        & $pc.Source --version
        & $pc.Source -c "import sys; print('sys.executable:', sys.executable); print('sys.version:   ', sys.version); print('sys.prefix:    ', sys.prefix); print('sys.base_prefix:', sys.base_prefix)"
    } else {
        "python: NOT FOUND in PATH"
    }
    ""
    $uc = Get-Command uv -ErrorAction SilentlyContinue
    if ($uc) {
        "uv (where):       $($uc.Source)"
        & $uc.Source --version
        ""
        "=== uv tool list ==="
        & $uc.Source tool list
        ""
        "=== uv tool dir ==="
        & $uc.Source tool dir
        "=== uv tool dir --bin ==="
        & $uc.Source tool dir --bin
    } else {
        "uv: NOT FOUND in PATH"
    }
} | Out-Null

# --- 03 rlm-tools-bsl package + uv tool env layout ---
$rlmExePath  = $null
$rlmToolRoot = $null
Save-Section "03-rlm-tools-bsl" {
    $rc = Get-Command rlm-tools-bsl -ErrorAction SilentlyContinue
    if ($rc) {
        $script:rlmExePath = $rc.Source
        "rlm-tools-bsl (where): $($rc.Source)"
        try { & $rc.Source --version } catch { "ERROR: $($_.Exception.Message)" }
    } else {
        "rlm-tools-bsl: NOT FOUND in PATH"
    }
    ""
    $uvToolDir = $null
    try { $uvToolDir = (& uv tool dir 2>$null) } catch {}
    if ($uvToolDir) {
        $toolRoot = Join-Path $uvToolDir "rlm-tools-bsl"
        $script:rlmToolRoot = $toolRoot
        "uv tool env root: $toolRoot"
        "exists:           $(Test-Path $toolRoot)"
        if (Test-Path $toolRoot) {
            ""
            "=== top-level listing ==="
            Get-ChildItem -Path $toolRoot -Force -ErrorAction SilentlyContinue |
                Select-Object Mode, LastWriteTime, Length, Name |
                Format-Table -AutoSize | Out-String
            ""
            "=== Scripts\ listing ==="
            $sc = Join-Path $toolRoot "Scripts"
            if (Test-Path $sc) {
                Get-ChildItem -Path $sc -Force -ErrorAction SilentlyContinue |
                    Select-Object Mode, LastWriteTime, Length, Name |
                    Format-Table -AutoSize | Out-String
            } else { "(no Scripts directory)" }
        }
    } else {
        "uv tool dir: unknown"
    }
} | Out-Null

# --- 04 Service query ---
Save-Section "04-service-sc" {
    "=== sc query ==="
    & sc.exe query rlm-tools-bsl
    ""
    "=== sc qc ==="
    & sc.exe qc rlm-tools-bsl
    ""
    "=== sc qdescription ==="
    & sc.exe qdescription rlm-tools-bsl 8192
    ""
    "=== sc qfailure ==="
    & sc.exe qfailure rlm-tools-bsl
    ""
    "=== Get-Service ==="
    $svc = Get-Service -Name "rlm-tools-bsl" -ErrorAction SilentlyContinue
    if ($svc) {
        $svc | Format-List * | Out-String
    } else {
        "service 'rlm-tools-bsl' not found via Get-Service"
    }
} | Out-Null

# --- 05 Registry: ImagePath, Environment ---
$imagePath = $null
Save-Section "05-registry" {
    $keyPath = "HKLM:\SYSTEM\CurrentControlSet\Services\rlm-tools-bsl"
    if (Test-Path $keyPath) {
        "Registry key: $keyPath"
        ""
        $item = Get-Item $keyPath
        foreach ($vn in $item.GetValueNames()) {
            $vk = $item.GetValueKind($vn)
            $vv = $item.GetValue($vn)
            if ($vk -eq 'MultiString' -and $vv) {
                "  $vn ($vk):"
                foreach ($line in $vv) { "    $line" }
            } else {
                "  $vn ($vk): $vv"
            }
            if ($vn -eq 'ImagePath') { $script:imagePath = $vv }
        }
    } else {
        "WARNING: key not found: $keyPath"
        if (-not $isAdmin) { "  (requires admin to read HKLM)" }
    }
} | Out-Null

# --- 06 File check: pythonservice.exe, required DLLs ---
Save-Section "06-files" {
    if ($imagePath) {
        "ImagePath raw: $imagePath"
        $exe = $imagePath
        if ($exe -match '^"([^"]+)"') { $exe = $matches[1] }
        else { $exe = ($exe -split ' ')[0] }
        "Parsed exe:    $exe"
        "Exists:        $(Test-Path $exe)"
        if (Test-Path $exe) {
            $exeDir = Split-Path -Parent $exe
            "Exe directory: $exeDir"
            ""
            "=== DLLs in exe directory ==="
            Get-ChildItem -Path $exeDir -Filter *.dll -ErrorAction SilentlyContinue |
                Select-Object Name, Length |
                Format-Table -AutoSize | Out-String
            ""
            "=== Required DLLs check (next to pythonservice.exe) ==="
            $required = @(
                'python3.dll',
                'python313.dll','python312.dll','python311.dll','python310.dll',
                'pywintypes313.dll','pywintypes312.dll','pywintypes311.dll','pywintypes310.dll',
                'pythoncom313.dll','pythoncom312.dll','pythoncom311.dll','pythoncom310.dll'
            )
            foreach ($d in $required) {
                $p = Join-Path $exeDir $d
                $e = Test-Path $p
                "  {0,-22} : {1}" -f $d, $(if ($e) { 'FOUND' } else { 'missing' })
            }
        }
    } else {
        "ImagePath unavailable (service not installed or not admin)"
    }
    ""
    if ($rlmToolRoot -and (Test-Path $rlmToolRoot)) {
        "=== DLLs at uv tool env root ($rlmToolRoot) ==="
        $rootDlls = Get-ChildItem -Path $rlmToolRoot -Filter *.dll -ErrorAction SilentlyContinue
        if ($rootDlls) {
            $rootDlls | Select-Object Name, Length | Format-Table -AutoSize | Out-String
        } else {
            "(no DLLs at env root)"
        }
        ""
        "=== site-packages\pywin32_system32\ ==="
        $sp = Join-Path $rlmToolRoot "Lib\site-packages\pywin32_system32"
        if (Test-Path $sp) {
            Get-ChildItem -Path $sp -ErrorAction SilentlyContinue |
                Select-Object Name, Length | Format-Table -AutoSize | Out-String
        } else {
            "Not found: $sp"
        }
        ""
        "=== site-packages\win32\pythonservice.exe ==="
        $ps = Join-Path $rlmToolRoot "Lib\site-packages\win32\pythonservice.exe"
        "Path:   $ps"
        "Exists: $(Test-Path $ps)"
        if (Test-Path $ps) {
            $fi = Get-Item $ps
            "Size:   $($fi.Length)"
            "Mtime:  $($fi.LastWriteTime)"
        }
    }
} | Out-Null

# --- 07 Import probe via uv tool Python ---
Save-Section "07-import-check" {
    if (-not $rlmToolRoot) { "skip: uv tool env root unknown"; return }
    $pyExe = Join-Path $rlmToolRoot "Scripts\python.exe"
    if (-not (Test-Path $pyExe)) { $pyExe = Join-Path $rlmToolRoot "python.exe" }
    "Python: $pyExe"
    "Exists: $(Test-Path $pyExe)"
    if (-not (Test-Path $pyExe)) { return }
    # Force UTF-8 for Python subprocess output (SERVICE_DESC contains Cyrillic)
    $env:PYTHONIOENCODING = "utf-8"
    ""
    "=== import rlm_tools_bsl ==="
    & $pyExe -c "import rlm_tools_bsl; print('module:', rlm_tools_bsl.__file__)"
    ""
    "=== import rlm_tools_bsl._service_win ==="
    & $pyExe -c "import rlm_tools_bsl._service_win as m; print('module:', m.__file__); print('SERVICE_NAME:', m.SERVICE_NAME)"
    ""
    "=== import win32service / win32serviceutil / win32event ==="
    & $pyExe -c "import win32service, win32serviceutil, win32event; print('pywin32 OK'); import pywintypes; print('pywintypes OK')"
    ""
    "=== importlib.metadata versions ==="
    & $pyExe -c @"
from importlib.metadata import version, PackageNotFoundError
for pkg in ('rlm-tools-bsl', 'pywin32', 'mcp', 'uvicorn', 'starlette', 'anthropic'):
    try:
        print(f'{pkg:20s} = {version(pkg)}')
    except PackageNotFoundError:
        print(f'{pkg:20s} = NOT INSTALLED')
"@
    ""
    "=== uv pip list (for uv tool env Python) ==="
    $uc = Get-Command uv -ErrorAction SilentlyContinue
    if ($uc) {
        & $uc.Source pip list --python $pyExe
    } else {
        "uv not in PATH, skipped"
    }
    ""
    "=== win32serviceutil.LocatePythonServiceExe() ==="
    & $pyExe -c "import win32serviceutil; print(win32serviceutil.LocatePythonServiceExe())"
    ""
    "=== ctypes load DLLs (python3*.dll, pywintypes*.dll, pythoncom*.dll) ==="
    & $pyExe -c @"
import ctypes, sys, os, glob
print('sys.version:', sys.version)
print('sys.prefix:', sys.prefix)
print('os.add_dll_directory supported:', hasattr(os, 'add_dll_directory'))
for name in ('python3.dll', f'python{sys.version_info.major}{sys.version_info.minor}.dll'):
    try:
        ctypes.CDLL(name)
        print('ctypes.CDLL OK:', name)
    except OSError as e:
        print('ctypes.CDLL FAIL:', name, '->', e)
"@
} | Out-Null

# --- 07b Import probe via the Python that actually runs the service ---
# (simple-install-from-pip.ps1 installs rlm-tools-bsl into BOTH uv tool env and
# the global Python; ImagePath points at the global Python. Versions and pywin32
# can diverge, so check the real runtime too.)
$servicePyExe = $null
$serviceDiffers = $false
Save-Section "07b-import-check-service-python" {
    if (-not $imagePath) { "skip: ImagePath unavailable (service not installed or not admin)"; return }
    $exe = $imagePath
    if ($exe -match '^"([^"]+)"') { $exe = $matches[1] } else { $exe = ($exe -split ' ')[0] }
    if (-not (Test-Path $exe)) { "skip: $exe not found"; return }
    $exeDir = Split-Path -Parent $exe
    $svcPy = Join-Path $exeDir "python.exe"
    if (-not (Test-Path $svcPy)) {
        "skip: python.exe not found next to pythonservice.exe at $exeDir"
        return
    }
    $script:servicePyExe = $svcPy
    "Service runtime Python: $svcPy"
    $uvPy = $null
    if ($rlmToolRoot) {
        $uvPy = Join-Path $rlmToolRoot "Scripts\python.exe"
        if (-not (Test-Path $uvPy)) { $uvPy = Join-Path $rlmToolRoot "python.exe" }
    }
    if ($uvPy -and (Test-Path $uvPy)) {
        try {
            if ((Resolve-Path $svcPy).Path -ieq (Resolve-Path $uvPy).Path) {
                "(same as uv tool env Python at section 07; no separate check needed)"
                return
            }
        } catch {}
    }
    $script:serviceDiffers = $true
    "(differs from uv tool env Python -- this is the interpreter the service ACTUALLY uses)"
    ""
    $env:PYTHONIOENCODING = "utf-8"
    "=== sys info (service Python) ==="
    & $svcPy -c "import sys; print('sys.version:', sys.version); print('sys.prefix:', sys.prefix); print('sys.base_prefix:', sys.base_prefix); print('sys.executable:', sys.executable)"
    ""
    "=== import rlm_tools_bsl ==="
    & $svcPy -c "import rlm_tools_bsl; print('module:', rlm_tools_bsl.__file__)"
    ""
    "=== import rlm_tools_bsl._service_win ==="
    & $svcPy -c "import rlm_tools_bsl._service_win as m; print('module:', m.__file__); print('SERVICE_NAME:', m.SERVICE_NAME)"
    ""
    "=== import win32service / win32serviceutil / win32event ==="
    & $svcPy -c "import win32service, win32serviceutil, win32event; print('pywin32 OK'); import pywintypes; print('pywintypes OK')"
    ""
    "=== importlib.metadata versions (service Python) ==="
    & $svcPy -c @"
from importlib.metadata import version, PackageNotFoundError
for pkg in ('rlm-tools-bsl', 'pywin32', 'mcp', 'uvicorn', 'starlette'):
    try:
        print(f'{pkg:20s} = {version(pkg)}')
    except PackageNotFoundError:
        print(f'{pkg:20s} = NOT INSTALLED')
"@
    ""
    "=== win32serviceutil.LocatePythonServiceExe() (service Python) ==="
    & $svcPy -c "import win32serviceutil; print(win32serviceutil.LocatePythonServiceExe())"
    ""
    "=== ctypes load DLLs ==="
    & $svcPy -c @"
import ctypes, sys
for name in ('python3.dll', f'python{sys.version_info.major}{sys.version_info.minor}.dll'):
    try:
        ctypes.CDLL(name); print('ctypes.CDLL OK:', name)
    except OSError as e:
        print('ctypes.CDLL FAIL:', name, '->', e)
"@
} | Out-Null

# --- 08 service.json ---
Save-Section "08-service-json" {
    $cfg = $rlmConfigPath
    "Path:   $cfg"
    "Exists: $(Test-Path $cfg)"
    if (Test-Path $cfg) {
        ""
        "=== content ==="
        Get-Content -Path $cfg -Raw
    }
} | Out-Null

# --- 09 server.log tail ---
Save-Section "09-server-log" {
    $logPath = $rlmLogPath
    "Path:   $logPath"
    "Exists: $(Test-Path $logPath)"
    if (Test-Path $logPath) {
        $fi = Get-Item $logPath
        "Size:   $($fi.Length) bytes"
        "Mtime:  $($fi.LastWriteTime)"
        ""
        "=== Last 500 lines ==="
        Get-Content -Path $logPath -Tail 500 -ErrorAction SilentlyContinue | Out-String
    }
} | Out-Null

# --- 10 Event Log: Application ---
Save-Section "10-eventlog-application" {
    if (-not $isAdmin) { "(may require admin)" }
    $start = (Get-Date).AddDays(-$EventLogDays)
    try {
        $events = Get-WinEvent -FilterHashtable @{ LogName='Application'; StartTime=$start } -ErrorAction Stop
        $hits = $events | Where-Object {
            $_.ProviderName -match 'pythonservice|rlm|Python|Service Control Manager|Application Error|\.NET Runtime' -or
            $_.Message -match 'rlm-tools-bsl|pythonservice|_service_win|python3\d+\.dll|pywintypes|pythoncom'
        }
        if (-not $hits) { "No matching events in last $EventLogDays days."; return }
        $hits | Select-Object TimeCreated, Id, LevelDisplayName, ProviderName, Message |
            Format-List | Out-String
    } catch {
        "Query error: $($_.Exception.Message)"
    }
} | Out-Null

# --- 11 Event Log: System (SCM) ---
Save-Section "11-eventlog-system" {
    if (-not $isAdmin) { "(may require admin)" }
    $start = (Get-Date).AddDays(-$EventLogDays)
    try {
        $events = Get-WinEvent -FilterHashtable @{ LogName='System'; StartTime=$start; ProviderName='Service Control Manager' } -ErrorAction Stop
        $hits = $events | Where-Object { $_.Message -match 'rlm-tools-bsl' }
        if (-not $hits) { "No SCM events for rlm-tools-bsl in last $EventLogDays days."; return }
        $hits | Select-Object TimeCreated, Id, LevelDisplayName, Message |
            Format-List | Out-String
    } catch {
        "Query error: $($_.Exception.Message)"
    }
} | Out-Null

# --- 12 (optional) pythonservice.exe -debug run for 5s ---
if ($RunDebug) {
    Save-Section "12-debug-run" {
        if (-not $isAdmin) {
            "skip: -RunDebug requires Administrator"
            return
        }
        if (-not $imagePath) { "skip: ImagePath unavailable (service not installed or not admin)"; return }
        $ps = $imagePath
        if ($ps -match '^"([^"]+)"') { $ps = $matches[1] } else { $ps = ($ps -split ' ')[0] }
        if (-not (Test-Path $ps)) { "skip: $ps not found"; return }
        # Derive site-packages from the *actual* pythonservice.exe directory
        # (not $rlmToolRoot, which comes from `uv tool dir` and may point to a
        # different env if the service was installed against global Python or
        # a different uv profile). This mirrors install-time logic in
        # _service_win.py: pythonservice.exe is copied to <env>\ and the
        # matching site-packages is at <env>\Lib\site-packages.
        $sp = Join-Path (Split-Path -Parent $ps) "Lib\site-packages"
        if (-not (Test-Path $sp)) { "skip: site-packages not found at $sp"; return }
        "Running: $ps -debug rlm-tools-bsl (5s)"
        $oldPP  = $env:PYTHONPATH
        $oldCFG = $env:RLM_CONFIG_FILE
        # Mirror build_service_pythonpath() in src/rlm_tools_bsl/_service_env.py:
        # pythonservice.exe in a uv tool env has no python.exe sibling, so site
        # processing / .pth files do not run and pywin32's win32 / Pythonwin
        # sub-dirs are not on sys.path. We bake them in explicitly so -RunDebug
        # reproduces the real installed-service environment.
        $env:PYTHONPATH = @(
            $sp,
            (Join-Path $sp "win32"),
            (Join-Path $sp "win32\lib"),
            (Join-Path $sp "Pythonwin")
        ) -join ";"
        $env:RLM_CONFIG_FILE = $rlmConfigPath
        $stdout = Join-Path $workDir "_debug-stdout.txt"
        $stderr = Join-Path $workDir "_debug-stderr.txt"
        $proc = Start-Process -FilePath $ps -ArgumentList "-debug","rlm-tools-bsl" `
            -PassThru -NoNewWindow `
            -RedirectStandardOutput $stdout -RedirectStandardError $stderr
        Start-Sleep -Seconds 5
        if (-not $proc.HasExited) {
            try { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } catch {}
        }
        $env:PYTHONPATH      = $oldPP
        $env:RLM_CONFIG_FILE = $oldCFG
        "ExitCode: $($proc.ExitCode)"
        "--- stdout ---"
        if (Test-Path $stdout) { Get-Content $stdout -Raw }
        "--- stderr ---"
        if (Test-Path $stderr) { Get-Content $stderr -Raw }
    } | Out-Null
} else {
    "Skipping pythonservice -debug run (pass -RunDebug to enable)" |
        Set-Content -Path (Join-Path $workDir "12-debug-run.txt") -Encoding UTF8
}

# --- Summary indicators ---
Add-Summary ""
Add-Summary "=== Quick indicators ==="

$svc = Get-Service -Name "rlm-tools-bsl" -ErrorAction SilentlyContinue
Add-Summary ("Service present:           " + ($svc -ne $null))
if ($svc) { Add-Summary ("Service status:            " + $svc.Status) }

Add-Summary ("rlm-tools-bsl in PATH:     " + ($rlmExePath -ne $null))
Add-Summary ("uv tool env root resolved: " + ($rlmToolRoot -ne $null))

if ($imagePath) {
    $exe = $imagePath
    if ($exe -match '^"([^"]+)"') { $exe = $matches[1] }
    else { $exe = ($exe -split ' ')[0] }
    Add-Summary ("ImagePath exists:          " + (Test-Path $exe))
    if (Test-Path $exe) {
        $exeDir = Split-Path -Parent $exe
        $pyDll  = Join-Path $exeDir "python3.dll"
        Add-Summary ("python3.dll next to exe:   " + (Test-Path $pyDll))
        $pywt = Get-ChildItem -Path $exeDir -Filter "pywintypes*.dll" -ErrorAction SilentlyContinue
        Add-Summary ("pywintypes*.dll next to exe: " + ($pywt.Count))
    }
}

$cfg = $rlmConfigPath
Add-Summary ("service.json exists:       " + (Test-Path $cfg))

# Version consistency: uv tool env Python vs service-runtime Python
function _PkgVersion([string]$py, [string]$pkg) {
    if (-not $py -or -not (Test-Path $py)) { return "?" }
    try {
        $env:PYTHONIOENCODING = "utf-8"
        $v = & $py -c "from importlib.metadata import version, PackageNotFoundError`ntry:`n print(version('$pkg'))`nexcept PackageNotFoundError:`n print('MISSING')" 2>$null
        if ($LASTEXITCODE -ne 0) { return "err" }
        return ($v | Select-Object -First 1).Trim()
    } catch { return "err" }
}
$uvPy = $null
if ($rlmToolRoot) {
    $uvPy = Join-Path $rlmToolRoot "Scripts\python.exe"
    if (-not (Test-Path $uvPy)) { $uvPy = Join-Path $rlmToolRoot "python.exe" }
}
$uvRlmVer  = _PkgVersion $uvPy "rlm-tools-bsl"
$uvPywVer  = _PkgVersion $uvPy "pywin32"
$svcRlmVer = _PkgVersion $servicePyExe "rlm-tools-bsl"
$svcPywVer = _PkgVersion $servicePyExe "pywin32"
Add-Summary ""
Add-Summary "=== Package versions ==="
Add-Summary ("uv tool env Python:        " + $(if ($uvPy) { $uvPy } else { "(none)" }))
Add-Summary ("  rlm-tools-bsl:           " + $uvRlmVer)
Add-Summary ("  pywin32:                 " + $uvPywVer)
if ($servicePyExe) {
    Add-Summary ("Service-runtime Python:    " + $servicePyExe)
    Add-Summary ("  rlm-tools-bsl:           " + $svcRlmVer)
    Add-Summary ("  pywin32:                 " + $svcPywVer)
    if ($serviceDiffers) {
        if ($uvRlmVer -ne $svcRlmVer) {
            Add-Summary ("  !! rlm-tools-bsl MISMATCH between uv tool env and service Python")
        }
        if ($uvPywVer -ne $svcPywVer) {
            Add-Summary ("  !! pywin32 MISMATCH between uv tool env and service Python")
        }
    }
} else {
    Add-Summary "Service-runtime Python:    (not resolved -- see section 07b)"
}

$logPath = $rlmLogPath
if (Test-Path $logPath) {
    $len = (Get-Item $logPath).Length
    Add-Summary ("server.log size (bytes):   " + $len)
} else {
    Add-Summary "server.log:                absent"
}

Add-Summary ""
Add-Summary "Full output: $workDir"

$summary -join "`r`n" | Set-Content -Path $summaryPath -Encoding UTF8

# --- Zip bundle ---
$zip = Join-Path $OutDir "diagnose-$stamp.zip"
try {
    Compress-Archive -Path (Join-Path $workDir "*") -DestinationPath $zip -Force
    Write-Host ""
    Write-Host "ZIP bundle: $zip" -ForegroundColor Green
} catch {
    Write-Host "ZIP failed: $($_.Exception.Message)" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host " Diagnostics complete" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host "Review before sharing:"
Write-Host "  - server.log may contain paths to your 1C source tree"
Write-Host "  - service.json contains env_file PATH (not content)"
Write-Host "  - Environment variables are NOT dumped, .env files are NOT read"
Write-Host ""
Write-Host "Attach the ZIP to GitHub issue or send to maintainers."
