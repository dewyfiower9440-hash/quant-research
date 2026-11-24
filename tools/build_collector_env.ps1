<#
.SYNOPSIS
    Builds (or rebuilds) the Python environment the T0 collectors run in, apart from the development .venv.
.DESCRIPTION
    v2.5 N01: on 2026-11-09 the collectors ran from the project's .venv while a dependency was added to it; every
    request then failed with an import error for 53 hours although the processes stayed alive.

    The collector environment holds the dependencies pinned by uv.lock (no dev tools) and an editable install of
    the project, so `uv add` / `uv sync` in the repository no longer touch the packages of a running collector.
    The project's own source is still read from the repository: stop the collectors (run\STOP), rebuild, start
    them again when the lock file or the collector code changes (docs/operations.md).

    The environment is built beside the target and swapped in at the end; a build or swap that fails puts the
    old one back (a failed new one is left as <EnvDir>.failed for inspection).
    Refuses to run while a collector lock is held unless -Force is given (the files would be in use). Disable
    the scheduled task before stopping the collectors: it starts them again every minute.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateScript({ Test-Path -LiteralPath (Join-Path $_ 'uv.lock') -PathType Leaf })][string]$Project = 'C:\dev\auto\code',
    [ValidateNotNullOrEmpty()][string]$EnvDir = 'C:\dev\qr-collector-env',
    [ValidateNotNullOrEmpty()][string]$DataRoot = 'C:\dev\qr-data',
    [switch]$Force
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Test-FileLocked {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    try {
        $stream = [System.IO.File]::Open($Path, 'Open', 'ReadWrite', 'None')
        $stream.Close()
        return $false
    } catch [System.IO.IOException] {
        return $true
    }
}

$running = @('supervisor', 'rest', 'ws') | Where-Object { Test-FileLocked (Join-Path $DataRoot "run\$_.lock") }
if ($running -and -not $Force) {
    throw ("collectors are running ($($running -join ', ')). Stop them first: Disable-ScheduledTask " +
        "qr-collector-supervisor (it restarts them every minute and removes the STOP file), then create " +
        "$DataRoot\run\STOP and wait until the locks are free. Or pass -Force.")
}

# not Get-FileHash: under -WhatIf it returns nothing in Windows PowerShell 5.1
$sha = [System.Security.Cryptography.SHA256]::Create()
try {
    $bytes = [System.IO.File]::ReadAllBytes((Join-Path $Project 'uv.lock'))
    $lockHash = -join ($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') })
} finally {
    $sha.Dispose()
}
$staging = "$EnvDir.new"
$requirements = Join-Path ([System.IO.Path]::GetTempPath()) "qr-collector-requirements-$PID.txt"
Write-Output "collector environment: $EnvDir (uv.lock sha256 $lockHash)"

if ($PSCmdlet.ShouldProcess($EnvDir, 'Build collector environment')) {
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
    try {
        & uv venv $staging --python 3.13
        if ($LASTEXITCODE -ne 0) { throw "uv venv failed ($LASTEXITCODE)" }
        & uv export --project $Project --frozen --no-dev --no-emit-project --no-hashes --output-file $requirements
        if ($LASTEXITCODE -ne 0) { throw "uv export failed ($LASTEXITCODE)" }
        $python = Join-Path $staging 'Scripts\python.exe'
        & uv pip install --python $python --requirement $requirements
        if ($LASTEXITCODE -ne 0) { throw "installing the locked dependencies failed ($LASTEXITCODE)" }
        & uv pip install --python $python --no-deps --editable $Project
        if ($LASTEXITCODE -ne 0) { throw "installing the project failed ($LASTEXITCODE)" }
        & $python -c 'import quant_research.capture.run'
        if ($LASTEXITCODE -ne 0) { throw 'the new environment cannot import the collector' }
        $stamp = [ordered]@{
            built_utc      = (Get-Date).ToUniversalTime().ToString('s') + 'Z'
            uv_lock_sha256 = $lockHash
            project        = $Project
        }
        [System.IO.File]::WriteAllText(
            (Join-Path $staging 'qr-env.json'), ($stamp | ConvertTo-Json), [System.Text.UTF8Encoding]::new($false))
    } finally {
        Remove-Item -LiteralPath $requirements -ErrorAction SilentlyContinue
    }
    # Built beside the target and swapped in. pyvenv.cfg points at the base interpreter, not at this folder, so
    # `pythonw -m ...` (how the task starts the collectors) works after the rename; console-script launchers
    # inside Scripts may keep the staging path and are not used.
    $old = "$EnvDir.old"
    if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Recurse -Force }
    $hadOld = Test-Path -LiteralPath $EnvDir
    if ($hadOld) { Rename-Item -LiteralPath $EnvDir -NewName (Split-Path -Leaf $old) }
    try {
        Rename-Item -LiteralPath $staging -NewName (Split-Path -Leaf $EnvDir)
        & (Join-Path $EnvDir 'Scripts\python.exe') -c 'import quant_research.capture.run'
        if ($LASTEXITCODE -ne 0) { throw "the environment stopped importing the collector after the rename: $EnvDir" }
    } catch {
        # put the previous environment back: the task would otherwise find no interpreter every minute
        $failed = "$EnvDir.failed"
        if (Test-Path -LiteralPath $failed) { Remove-Item -LiteralPath $failed -Recurse -Force }
        if (Test-Path -LiteralPath $EnvDir) { Rename-Item -LiteralPath $EnvDir -NewName (Split-Path -Leaf $failed) }
        if ($hadOld) { Rename-Item -LiteralPath $old -NewName (Split-Path -Leaf $EnvDir) }
        throw
    }
    if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Recurse -Force }
    Get-Content -LiteralPath (Join-Path $EnvDir 'qr-env.json') -Raw
}
