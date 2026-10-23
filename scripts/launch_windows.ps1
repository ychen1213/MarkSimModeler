# Resolve the configured interpreter without relying on Python being on PATH.
# This script is ASCII so built-in Windows PowerShell 5.1 reads it reliably.
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$LauncherArgs)
$ErrorActionPreference = 'Stop'
$modelerRoot = Split-Path -Parent $PSScriptRoot
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONUTF8 = '1'
$logPath = $null

function Write-LaunchLine([string]$Message) {
    Write-Host $Message
    if ($logPath) {
        [System.IO.File]::AppendAllText($logPath, $Message + [Environment]::NewLine, $utf8)
    }
}

function Resolve-Python {
    $settings = Join-Path $modelerRoot 'config\local_runtime.json'
    if (Test-Path -LiteralPath $settings) {
        $config = Get-Content -LiteralPath $settings -Raw -Encoding UTF8 | ConvertFrom-Json
        if (-not ($config.python -is [string]) -or [string]::IsNullOrWhiteSpace($config.python)) {
            throw 'config/local_runtime.json must contain a nonempty python path.'
        }
        $configured = [Environment]::ExpandEnvironmentVariables($config.python)
        if ($configured.StartsWith('~\') -or $configured.StartsWith('~/')) {
            $configured = Join-Path ([Environment]::GetFolderPath('UserProfile')) $configured.Substring(2)
        }
        if (-not [System.IO.Path]::IsPathRooted($configured)) {
            $configured = Join-Path $modelerRoot $configured
        }
        if (-not (Test-Path -LiteralPath $configured -PathType Leaf)) {
            throw "Configured Python is missing: $configured. Edit config/local_runtime.json."
        }
        return (Get-Item -LiteralPath $configured).FullName
    }
    $localPython = Join-Path $modelerRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $localPython -PathType Leaf) { return $localPython }
    $pyLauncher = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        foreach ($version in @('-3.12', '-3.11')) {
            $oldPreference = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            $candidate = & $pyLauncher.Source $version -c 'import sys; print(sys.executable)' 2>$null
            $result = $LASTEXITCODE
            $ErrorActionPreference = $oldPreference
            if ($result -eq 0 -and $candidate -and (Test-Path -LiteralPath ([string]$candidate) -PathType Leaf)) {
                return ([string]$candidate).Trim()
            }
        }
    }
    foreach ($command in @(Get-Command python.exe -All -CommandType Application -ErrorAction SilentlyContinue)) {
        if ($command.Source -match '[\\/]Microsoft[\\/]WindowsApps[\\/]') { continue }
        return $command.Source
    }
    throw 'No usable Python found. The Windows Store alias is not an interpreter. Set config/local_runtime.json to an existing Python executable, or use setup.bat with installed Python 3.11/3.12.'
}

try {
    $logFolder = Join-Path $modelerRoot 'logs'
    [System.IO.Directory]::CreateDirectory($logFolder) | Out-Null
    $stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfff')
    $logPath = Join-Path $logFolder "startup_${stamp}_${PID}.log"
    Write-LaunchLine "Startup log: $logPath"
    $pythonExe = Resolve-Python
    Write-LaunchLine "Python: $pythonExe"
    Write-LaunchLine "Project: $modelerRoot"
    Set-Location -LiteralPath $modelerRoot
    # Windows PowerShell turns native stderr into ErrorRecords. Collect them
    # without hiding native exit codes or making ordinary stderr a PS exception.
    $ErrorActionPreference = 'Continue'
    & $pythonExe -X utf8 (Join-Path $PSScriptRoot 'launch_modeler.py') @LauncherArgs 2>&1 |
        ForEach-Object { Write-LaunchLine ([string]$_) }
    $result = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($null -eq $result) { $result = 1 }
    Write-LaunchLine "Launcher exit code: $result"
    if ($result -ne 0) { Write-LaunchLine "Startup failed. Full output is saved in: $logPath" }
    exit $result
} catch {
    Write-LaunchLine ('Startup error: ' + $_.Exception.Message)
    Write-LaunchLine 'No environment or dependency was installed or replaced.'
    exit 1
}
