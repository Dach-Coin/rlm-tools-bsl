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
    Host to bind the HTTP server. If omitted: $env:RLM_HOST, then the value of the
    existing installation, then 127.0.0.1 on a first install.

.PARAMETER Port
    Port for the HTTP server. If omitted: $env:RLM_PORT, then the value of the existing
    installation, then 9000 on a first install. Use only 1..65535; do not pass 0,
    which is reserved internally to mean that this parameter was omitted.

.PARAMETER EnvFile
    Path to .env file. If omitted: the path saved by the existing installation. Only on a
    FIRST install does an .env sitting next to this script get picked up.

.PARAMETER NoEnv
    Start the service without any .env file (also $env:RLM_NO_ENV=1). Needed when the
    existing config cannot be read: the .env path is then unknown too, and the server
    asks for an explicit decision.

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
    [string]$BindHost = "",
    [int]$Port = 0,
    [string]$EnvFile = "",
    [switch]$NoEnv,
    [switch]$NativeTls
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($PSBoundParameters.ContainsKey("EnvFile") -and [string]::IsNullOrWhiteSpace($EnvFile)) {
    Write-Error "Empty -EnvFile value; omit it to preserve settings or use -NoEnv."
    exit 1
}

# --- Settings of an existing installation ---
# Explicit user values and saved values are passed to service install; an omitted
# value is left for the server to resolve. Passing the saved values also protects an
# upgrade that unexpectedly resolves an older package without server-side memory.
if (-not $BindHost -and $env:RLM_HOST) { $BindHost = $env:RLM_HOST }
# The port given by the HUMAN is validated below, once Test-ValidPort exists -- and in
# any case before anything is stopped or uninstalled.

function Get-InstalledServiceConfigPath {
    # The service registry is the durable source of a custom config path. A later
    # installer run often comes from a fresh shell where RLM_CONFIG_FILE is no longer
    # exported; falling straight back to the default would reset that installation.
    try {
        $key = Get-Item "HKLM:\SYSTEM\CurrentControlSet\Services\rlm-tools-bsl" -ErrorAction Stop
        foreach ($entry in @($key.GetValue("Environment"))) {
            if ($entry -isnot [string]) { continue }
            $separator = $entry.IndexOf("=")
            if ($separator -le 0 -or $separator -eq $entry.Length - 1) { continue }
            $name = $entry.Substring(0, $separator)
            if ([string]::Equals($name, "RLM_CONFIG_FILE", [System.StringComparison]::OrdinalIgnoreCase)) {
                return $entry.Substring($separator + 1)
            }
        }
    } catch {
        return $null
    }
    return $null
}

$configWasExplicit = -not [string]::IsNullOrEmpty($env:RLM_CONFIG_FILE)
$installedConfigFile = if ($configWasExplicit) { $null } else { Get-InstalledServiceConfigPath }
if ($configWasExplicit) {
    $configFile = $env:RLM_CONFIG_FILE
} elseif ($installedConfigFile) {
    $configFile = $installedConfigFile
} else {
    $configFile = Join-Path $env:USERPROFILE ".config" |
        Join-Path -ChildPath "rlm-tools-bsl" |
        Join-Path -ChildPath "service.json"
}

if (-not [System.IO.Path]::IsPathRooted($configFile)) {
    # Pre-1.35.0 installs could persist a relative registry value. SCM resolves it from
    # the Windows system directory, not from the directory of this installer.
    $configBase = if ($installedConfigFile) { [System.Environment]::SystemDirectory } else { (Get-Location).Path }
    $configFile = Join-Path $configBase $configFile
}
if ($configWasExplicit -or $installedConfigFile) {
    # service install is a child process; give it the same path selected above.
    $env:RLM_CONFIG_FILE = $configFile
}
$configLogFile = Join-Path (Join-Path (Split-Path -Parent $configFile) "logs") "server.log"

function Get-JsonProperty {
    # Looks the key up the way Python does: byte for byte. PSObject.Properties[...] is
    # case-insensitive and -ceq, while case-sensitive, still compares linguistically --
    # under PS 5.1 the ligature "ho<U+FB06>" equals "host". Either way the scripts would
    # accept a config the server rejects, after the service had been unregistered.
    #
    # The type check also has to happen HERE. Returning an array from a PowerShell
    # function unrolls it, so `"host": ["bad host"]` would reach the caller as a plain
    # String and no guard outside could tell it from a real host any more.
    param($Data, [string]$Name)
    if ($null -eq $Data) { return $null }
    foreach ($property in $Data.PSObject.Properties) {
        if ([string]::Equals($property.Name, $Name, [System.StringComparison]::Ordinal)) {
            $value = $property.Value
            if ($value -is [string] -or $value -is [ValueType]) { return $value }
            return $null
        }
    }
    return $null
}

function Get-SavedSetting {
    param([string]$Name)
    return (Get-JsonProperty (Read-ConfigObject $configFile) $Name)
}

function Read-ConfigObject {
    # Parses as a JSON object -- says nothing about what is IN it.
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try {
        # Match Python's encoding="utf-8" exactly. Windows PowerShell 5.1 accepts a
        # UTF-8 BOM and replaces malformed bytes, while the service rejects both; a
        # looser preflight would discover that only after unregistering the service.
        $bytes = [System.IO.File]::ReadAllBytes($Path)
        if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
            return $null
        }
        $strictUtf8 = [System.Text.UTF8Encoding]::new($false, $true)
        $data = $strictUtf8.GetString($bytes) | ConvertFrom-Json
    } catch {
        return $null
    }
    if ($null -ne $data -and $data -is [PSCustomObject]) { return $data }
    return $null
}

function Test-ConfigIsReadable {
    param([string]$Path)
    return ($null -ne (Read-ConfigObject $Path))
}

function Test-ConfigIsValid {
    # USABLE, not merely well-formed: `{}` and `"port": "oops"` are valid JSON objects
    # the server rightly refuses. Judging them "valid" here is what let a semantically
    # broken file be copied over the last good backup.
    param([string]$Path)
    $data = Read-ConfigObject $Path
    if ($null -eq $data) { return $false }
    $configHost = Get-JsonProperty $data "host"
    if (-not ($configHost -is [string]) -or [string]::IsNullOrWhiteSpace($configHost)) { return $false }
    return ($null -ne (ConvertTo-PortNumber (Get-JsonProperty $data "port")))
}

function Assert-ConfigReplaceable {
    # save_config() commits through os.replace(). On Windows that operation fails when
    # another process opened service.json without FILE_SHARE_DELETE. Detect that before
    # the updater stops and unregisters the working service. Also prove that a sibling
    # staging file can be created: a missing config or an existing rescue copy may mean
    # the backup path below never exercises the destination directory.
    param([string]$Path)
    $configExists = Test-Path -LiteralPath $Path
    if ($configExists) {
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
        if ($item.IsReadOnly) {
            throw "Service config '$Path' is read-only and cannot be safely replaced. Remove the read-only attribute and run the installer again; the service was left untouched."
        }

        if (-not ("RlmToolsConfigReplaceProbe" -as [type])) {
            Add-Type -Language CSharp -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

public static class RlmToolsConfigReplaceProbe
{
    private const uint DeleteAccess = 0x00010000;
    private const uint ShareRead = 0x00000001;
    private const uint ShareWrite = 0x00000002;
    private const uint ShareDelete = 0x00000004;
    private const uint OpenExisting = 3;
    private const uint NormalAttributes = 0x00000080;

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFile(
        string fileName,
        uint desiredAccess,
        uint shareMode,
        IntPtr securityAttributes,
        uint creationDisposition,
        uint flagsAndAttributes,
        IntPtr templateFile);

    public static int TryOpenForDelete(string path)
    {
        SafeFileHandle handle = CreateFile(
            path,
            DeleteAccess,
            ShareRead | ShareWrite | ShareDelete,
            IntPtr.Zero,
            OpenExisting,
            NormalAttributes,
            IntPtr.Zero);
        if (handle.IsInvalid)
        {
            int error = Marshal.GetLastWin32Error();
            handle.Dispose();
            return error;
        }
        handle.Dispose();
        return 0;
    }
}
'@
        }

        $replaceError = [RlmToolsConfigReplaceProbe]::TryOpenForDelete($Path)
        if ($replaceError -ne 0) {
            $reason = (New-Object System.ComponentModel.Win32Exception($replaceError)).Message
            throw "Service config '$Path' cannot be safely replaced ($reason, Win32 error $replaceError). Close any editor or other program using this file, check its permissions, and run the installer again; the service was left untouched."
        }
    }

    $configDir = Split-Path -Parent $Path
    $writeProbe = "$Path.partial.$PID"
    $probeStream = $null
    try {
        if (-not (Test-Path -LiteralPath $configDir)) {
            [System.IO.Directory]::CreateDirectory($configDir) | Out-Null
        }
        $probeStream = [System.IO.File]::Open(
            $writeProbe,
            [System.IO.FileMode]::CreateNew,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None
        )
        $probeStream.WriteByte(0)
        $probeStream.Dispose()
        $probeStream = $null
        [System.IO.File]::Delete($writeProbe)
    } catch {
        if ($null -ne $probeStream) { $probeStream.Dispose() }
        Remove-Item -LiteralPath $writeProbe -Force -ErrorAction SilentlyContinue
        throw "Cannot prepare an atomic update for service config '$Path': $($_.Exception.Message). Check the directory permissions and free space, then run the installer again; the service was left untouched."
    }
}

function Copy-FileWithDacl {
    # Copy-Item creates a new file with the destination directory's inherited DACL.
    # Preserve the source ACL explicitly: the backup may later become service.json, and
    # losing an ACE for LocalSystem here would make the restored service unable to read it.
    param([string]$Source, [string]$Destination)
    $accessSection = [System.Security.AccessControl.AccessControlSections]::Access
    $sourceDacl = (Get-Acl -LiteralPath $Source -ErrorAction Stop).GetSecurityDescriptorSddlForm($accessSection)
    Copy-Item -LiteralPath $Source -Destination $Destination -Force
    try {
        $destinationAcl = Get-Acl -LiteralPath $Destination -ErrorAction Stop
        $destinationAcl.SetSecurityDescriptorSddlForm($sourceDacl, $accessSection)
        Set-Acl -LiteralPath $Destination -AclObject $destinationAcl -ErrorAction Stop
    } catch {
        Remove-Item -LiteralPath $Destination -Force -ErrorAction SilentlyContinue
        throw
    }
}

function Copy-Atomic {
    # Copy via a private staging file, so a half-written copy is never visible under the
    # real name. Copy-FileWithDacl also keeps a hardened config's DACL.
    param([string]$Source, [string]$Destination)
    $partial = "$Destination.partial.$PID"
    Copy-FileWithDacl $Source $partial
    Move-Item -LiteralPath $partial -Destination $Destination -Force
}

function ConvertTo-PortNumber {
    # Normalises a port exactly the way the server does, which is Python's int() for a
    # string and "integral value" for a number. The distinction matters: as a STRING
    # "3000.0" and "3e3" are not ints in Python (the server refuses them) while "3_000"
    # is; as a JSON NUMBER 3000.0 is accepted. A .NET Float parse would have said yes to
    # all three and let the scripts push a config the server rejects -- after the service
    # had already been unregistered.
    param($Value)
    if ($null -eq $Value -or $Value -is [bool]) { return $null }
    $number = 0L
    if ($Value -is [string]) {
        # Python's int(): optional sign, decimal digits, underscores only BETWEEN digits.
        # The digits are Unicode Nd, not just ASCII (Python accepts Arabic-Indic ones
        # and int() reads them as the same number), so each character is converted
        # individually instead of being matched by a [0-9] pattern.
        $text = $Value.Trim()
        $sign = 1
        if ($text.StartsWith("+") -or $text.StartsWith("-")) {
            if ($text.StartsWith("-")) { $sign = -1 }
            $text = $text.Substring(1)
        }
        if ($text -eq "" -or $text.StartsWith("_") -or $text.EndsWith("_") -or $text.Contains("__")) { return $null }
        # Walked by CODE POINT: ToCharArray() would split a digit outside the BMP into
        # two surrogates, neither of which is a digit on its own, while Python reads it
        # as the number it is.
        $ascii = ""
        $i = 0
        while ($i -lt $text.Length) {
            if ($text[$i] -eq "_") { $i++; continue }
            $digit = [System.Globalization.CharUnicodeInfo]::GetDecimalDigitValue($text, $i)
            if ($digit -lt 0) { return $null }
            $ascii += [string]$digit
            if ([char]::IsSurrogatePair($text, $i)) { $i += 2 } else { $i++ }
        }
        if (-not [long]::TryParse($ascii, [ref]$number)) { return $null }
        $number = $number * $sign
    } else {
        $asDouble = 0.0
        try {
            $asDouble = [double]$Value
        } catch {
            return $null
        }
        if ($asDouble -ne [math]::Floor($asDouble)) { return $null }
        if ([math]::Abs($asDouble) -gt 2147483647) { return $null }
        $number = [long]$asDouble
    }
    if ($number -lt 1 -or $number -gt 65535) { return $null }
    return [int]$number
}

function Test-ValidPort {
    param($Value)
    return ($null -ne (ConvertTo-PortNumber $Value))
}

function Restore-ConfigFromBackup {
    # A symlinked config was deleted BY THE LINK by the legacy uninstall. Put the content
    # back into the original target and recreate the link: writing a plain file here
    # would silently detach the service from the shared file it was pointing at.
    if ($configLinkTarget -and -not (Test-Path -LiteralPath $configFile)) {
        try {
            $linkDir = Split-Path -Parent $configLinkTarget
            if ($linkDir -and -not (Test-Path -LiteralPath $linkDir)) {
                New-Item -ItemType Directory -Path $linkDir -Force | Out-Null
            }
            # The target keeps whatever it holds: it is a real settings file of its own,
            # and somebody may well have updated it while the link was missing. Only a
            # target that does not exist at all is filled from the backup.
            if (-not (Test-Path -LiteralPath $configLinkTarget)) {
                Copy-Atomic $configBackupFile $configLinkTarget
            }
            New-Item -ItemType SymbolicLink -Path $configFile -Target $configLinkTarget -Force | Out-Null
            return $true
        } catch {
            Write-Warning "Could not recreate the symlink $configFile -> $configLinkTarget"
        }
    }

    # Atomic create-if-absent: fill a private temp file, then File.Move it into place.
    # Move FAILS when the destination exists, so no half-written config can ever be
    # observed under the real name.
    $configDir = Split-Path -Parent $configFile
    if (-not (Test-Path -LiteralPath $configDir)) {
        New-Item -ItemType Directory -Path $configDir -Force | Out-Null
    }
    $partial = "$configFile.partial.$PID"
    Copy-FileWithDacl $configBackupFile $partial
    try {
        [System.IO.File]::Move($partial, $configFile)
        return $true
    } catch {
        Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
        return $false
    }
}

# A run interrupted between `service uninstall` and `service install` used to leave the
# machine with no config at all. The backup is a file next to the config (not in the
# temp dir) precisely so the NEXT run can still find it.
# A port is only worth passing on as an explicit flag if it IS a port: the server rejects
# an explicit nonsense value (rightly -- it would hide a typo), and by the time it does,
# the service would already be unregistered. Checked here, before anything is touched.
if ($Port -ne 0 -and -not (Test-ValidPort $Port)) {
    Write-Error "-Port $Port is not a port number (1-65535)."
    exit 1
}
if ($Port -le 0 -and $env:RLM_PORT) {
    $envPort = ConvertTo-PortNumber $env:RLM_PORT
    if ($null -ne $envPort) {
        $Port = $envPort
    } else {
        Write-Error "RLM_PORT='$($env:RLM_PORT)' is not a port number (1-65535)."
        exit 1
    }
}

$configBackupFile = "$configFile.rlm-backup"
$configRescueFile = "$configFile.rlm-unreadable"
$configLinkFile = "$configFile.rlm-linktarget"
$configLinkTarget = $null

# Staging files are named per PID. PowerShell has no reliable exit hook, so clear ours
# and anything an interrupted earlier run left behind, up front. Only names ending in a
# PID are ours -- a user's own `service.json.partial.manual-copy` is not.
$configDirForSweep = Split-Path -Parent $configFile
if (Test-Path -LiteralPath $configDirForSweep) {
    $sweepLeaves = @(
        (Split-Path -Leaf $configFile),
        (Split-Path -Leaf $configBackupFile),
        (Split-Path -Leaf $configRescueFile),
        (Split-Path -Leaf $configLinkFile)
    )
    foreach ($leaf in $sweepLeaves) {
        Get-ChildItem -LiteralPath $configDirForSweep -Filter "$leaf.partial.*" -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '\.partial\.[0-9]+$' } |
            Remove-Item -Force -ErrorAction SilentlyContinue
    }
}
# The link target has to be known BEFORE anything is restored: an earlier run may have
# been interrupted after the legacy uninstall removed the LINK, and by then there is
# nothing left to read it from -- hence the note next to the config.
$configItem = Get-Item -LiteralPath $configFile -Force -ErrorAction SilentlyContinue
if ($configItem -and $configItem.LinkType -eq "SymbolicLink") {
    $configLinkTarget = @($configItem.Target)[0]
    if ($configLinkTarget) {
        Set-Content -LiteralPath $configLinkFile -Value $configLinkTarget -Encoding UTF8 -Force
    }
} elseif ((-not (Test-Path -LiteralPath $configFile)) -and (Test-Path -LiteralPath $configLinkFile)) {
    $configLinkTarget = (Get-Content -LiteralPath $configLinkFile -TotalCount 1 -ErrorAction SilentlyContinue)
}

if ((-not (Test-Path -LiteralPath $configFile)) -and (Test-Path -LiteralPath $configBackupFile)) {
    if (Restore-ConfigFromBackup) {
        Write-Host "Recovered the service settings left behind by an interrupted run."
    }
}

$hasSavedConfig = Test-Path -LiteralPath $configFile
Assert-ConfigReplaceable $configFile
$savedHost = Get-SavedSetting "host"
$savedPort = Get-SavedSetting "port"
$savedEnvFile = Get-SavedSetting "env_file"
# Only the type each key is supposed to hold survives. `"host": ["bad host"]` stringifies
# to a perfectly non-empty "bad host", which the preflight below would wave through -- and
# the service would be unregistered before the server ever saw the value.
if ($savedHost -isnot [string]) { $savedHost = $null }
if ($savedEnvFile -isnot [string]) { $savedEnvFile = $null }
if ($savedEnvFile -and -not [System.IO.Path]::IsPathRooted([string]$savedEnvFile)) {
    # Legacy configs could contain `.env`. The service runs from another CWD, so give
    # the newly installed backend the same deterministic config-relative interpretation
    # used when service.json is read directly.
    $savedEnvFile = [System.IO.Path]::GetFullPath(
        (Join-Path (Split-Path -Parent $configFile) ([string]$savedEnvFile))
    )
}
$savedPort = ConvertTo-PortNumber $savedPort

# The saved values are passed EXPLICITLY rather than left to the server to remember:
# that also survives the case where the freshly installed package turns out to be an
# older one (a lagging PyPI mirror), whose `service install` has no memory at all.
# A host of spaces is not a host: the server strips it and refuses, and it would do so
# only AFTER the service has been unregistered.
if ([string]::IsNullOrWhiteSpace($BindHost)) { $BindHost = "" }
if ([string]::IsNullOrWhiteSpace([string]$savedHost)) { $savedHost = $null }
if (-not $BindHost -and $savedHost) { $BindHost = [string]$savedHost }
# A port is only worth passing as an explicit flag if it IS a port: the server rejects an
# explicit nonsense value (rightly -- it would hide a typo), and it would do so after the
# service has already been unregistered.
if ($Port -le 0 -and $savedPort) { $Port = [int]$savedPort }

# Versions up to 1.34.0 DELETED service.json on `service uninstall`, and that uninstall
# is still performed by the OLD binary. The copy is what puts the settings back.
if ($hasSavedConfig) {
    if (Test-ConfigIsValid $configFile) {
        Copy-Atomic $configFile $configBackupFile
    } else {
        # A broken file must NOT be copied over the transient backup: an earlier
        # interrupted run may have left a good one there, and that copy is worth more
        # than the bytes that replaced it. The rescue copy is written ONCE and never
        # refreshed -- it may be the only trace of the original.
        if (Test-Path -LiteralPath $configRescueFile) {
            Write-Warning "$configFile does not parse - an earlier copy is kept at $configRescueFile"
        } else {
            Copy-Atomic $configFile $configRescueFile
            Write-Warning "$configFile does not parse - copied it as-is to $configRescueFile"
        }
    }
}

# --- Decide about .env before anything is touched ---
# -NoEnv / $env:RLM_NO_ENV is the counterpart of `service install --no-env`. It exists
# because an unreadable config leaves the server unable to know the .env path either: it
# then asks for an explicit decision, and without a way to say "none" the script could
# ask the user for something they cannot give.
$envArgs = @()
$envDecided = $false
if ($EnvFile) {
    $envArgs = @("--env", $EnvFile)
    $envDecided = $true
} elseif ($NoEnv -or $env:RLM_NO_ENV -eq "1") {
    $envArgs = @("--no-env")
    $envDecided = $true
    Write-Host "NoEnv - the service will start without an .env file."
} elseif ($savedEnvFile) {
    $envArgs = @("--env", [string]$savedEnvFile)
    $envDecided = $true
    Write-Host "Keeping .env from the service config: $savedEnvFile"
} elseif ($hasSavedConfig -and (Test-ConfigIsReadable $configFile)) {
    # Keep the server's persisted no-env/fallback mode. A legacy env_file:null still
    # allowed user/CWD fallbacks, while a new explicit --no-env is stored separately.
    $envDecided = $true
    Write-Host "Existing installation has no explicit .env path - preserving its current mode."
} elseif (-not $hasSavedConfig) {
    if (Test-Path (Join-Path $PSScriptRoot ".env")) {
        $resolvedEnv = (Resolve-Path (Join-Path $PSScriptRoot ".env")).Path
        $envArgs = @("--env", $resolvedEnv)
        Write-Host "Found .env: $resolvedEnv"
    } else {
        Write-Host "No .env found next to the installer. No explicit path will be saved; normal user/CWD .env fallbacks remain enabled."
    }
    $envDecided = $true
}

# Nothing destructive has happened yet, and this is the last moment that is true. An
# installation whose settings cannot be read, with nothing given on the command line, has
# exactly one safe outcome: stop and leave the running service alone. Going on would stop
# and unregister it, and only THEN would `service install` refuse -- for exactly the same
# reason, which is why this check asks for precisely what the server will ask for.
if ($hasSavedConfig -and ((-not $BindHost) -or ($Port -le 0) -or (-not $envDecided))) {
    Write-Error @"
$configFile exists, but the settings in it could not be read, and they were not supplied.
The service is left untouched. Fix the file, or re-run with all of: -BindHost <host>
-Port <port> and either -EnvFile <path> or -NoEnv. Or drop the settings altogether:
rlm-tools-bsl service uninstall --purge
"@
    exit 1
}

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
# Recheck immediately before the first service command. The earlier probe prevented
# needless backup work for an already-locked file; this one narrows the unavoidable
# check/use window if an editor opened service.json while prerequisites were prepared.
Assert-ConfigReplaceable $configFile
Write-Host "=== Step 1: Stop & uninstall existing service (if any) ===" -ForegroundColor Cyan
try {
    & rlm-tools-bsl service stop 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Service stopped."
    } else {
        Write-Host "Service stop returned exit code $LASTEXITCODE; checking uninstall next."
    }
} catch {
    Write-Host "Service was not running (OK)."
}
$uninstallExit = 1
try {
    & rlm-tools-bsl service uninstall 2>$null
    $uninstallExit = $LASTEXITCODE
} catch {
    $uninstallExit = 1
}
if ($uninstallExit -eq 0) {
    Write-Host "Service uninstalled."
} elseif (Get-Service -Name "rlm-tools-bsl" -ErrorAction SilentlyContinue) {
    Write-Error "Could not uninstall service 'rlm-tools-bsl' (exit code $uninstallExit). The update is stopped."
    exit 1
} else {
    # Legacy versions returned 1 when no service was registered; that is still a safe
    # fresh-install state as long as SCM confirms the service is absent.
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

# Everything below was decided before the service was touched (see the settings block).
$installArgs = @("service", "install")
if ($BindHost) { $installArgs += @("--host", $BindHost) }
if ($Port -gt 0) { $installArgs += @("--port", "$Port") }
$installArgs += $envArgs

if ((Test-Path -LiteralPath $configBackupFile) -and -not (Test-Path -LiteralPath $configFile)) {
    if (Restore-ConfigFromBackup) {
        Write-Host "Restored the service settings that the previous version's uninstall removed."
    }
}

& rlm-tools-bsl @installArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error "Service registration failed."
    exit 1
}

# The transient copy is dropped once the install has put a parseable config in place.
# Any other outcome leaves it for the next run (every early exit above stops before this
# line). The rescue copy is NOT touched here: it outlives the upgrade on purpose.
if (Test-ConfigIsValid $configFile) {
    Remove-Item -LiteralPath $configBackupFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $configLinkFile -Force -ErrorAction SilentlyContinue
}
if (Test-Path -LiteralPath $configRescueFile) {
    Write-Host "NOTE: a copy of the earlier unreadable config is kept at $configRescueFile"
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

# Read back what `service install` actually saved -- this script deliberately does not
# pass host/port unless asked to, so it cannot assume the built-in defaults.
$effHost = Get-SavedSetting "host"
$effPort = Get-SavedSetting "port"
if (-not $effHost) { if ($BindHost) { $effHost = $BindHost } else { $effHost = "127.0.0.1" } }
if (-not $effPort) { if ($Port -gt 0) { $effPort = $Port } else { $effPort = 9000 } }

# A wildcard bind is not a connectable address, and an IPv6 literal has to be bracketed
# or the URL does not parse at all (http://2001:db8::1:3000/health is not a thing).
$checkHost = ([string]$effHost).Trim("[", "]")
if ($checkHost -eq "" -or $checkHost -eq "0.0.0.0" -or $checkHost -eq "*") {
    $checkHost = "127.0.0.1"
} elseif ($checkHost -eq "::" -or $checkHost -eq "0:0:0:0:0:0:0:0") {
    $checkHost = "[::1]"
} elseif ($checkHost.Contains(":")) {
    $checkHost = "[$checkHost]"
}

# Use /health (lightweight, does not create an MCP session) instead of /mcp.
$url = "http://${checkHost}:${effPort}/health"
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
    Write-Warning "Logs:         $configLogFile"
    exit 1
}

$mcpUrl = "http://${checkHost}:${effPort}/mcp"
Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host " Done! HTTP MCP server is running." -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Version:  $(cmd /c 'rlm-tools-bsl --version 2>nul')"  # via cmd: under ErrorActionPreference=Stop any stderr from a native exe (e.g. a dependency warning) aborts the script
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
Write-Host "Logs: $configLogFile"
Write-Host ""
Write-Host "Re-run this script after `git pull` to upgrade." -ForegroundColor Cyan
