<#
.SYNOPSIS
    Registers (or updates) the per-user Task Scheduler entry that runs the step-8 bulk download.
.DESCRIPTION
    Standard-user task (no admin, no stored password), independent of any Claude Code session.
    Starts once ~1 minute after registration and repeats every $RepeatMinutes minutes for $Days days:
    a run that stopped (resource/thermal gate exit 3, failed files exit 4, crash) resumes on the next
    tick from the catalog. MultipleInstances=IgnoreNew plus the runner's own lock (run/bulk.lock)
    prevent duplicates. After a complete run the runner writes bulk/complete-<universe>.json and every
    later tick exits at once. Priority 7 (below normal) keeps the T0 collectors ahead of it.
    Remove when done: Unregister-ScheduledTask -TaskName qr-bulk-download -Confirm:$false
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateNotNullOrEmpty()][string]$TaskName = 'qr-bulk-download',
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Container })][string]$Project = 'C:\dev\quant-research',
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })][string]$Universe = 'C:\dev\qr-data\bulk\universe-20261109.json',
    [ValidateNotNullOrEmpty()][string]$LogDir = 'C:\dev\qr-data\logs',
    [ValidateRange(5, 1440)][int]$RepeatMinutes = 30,
    [ValidateRange(1, 30)][int]$Days = 7,
    [ValidateRange(1, 8)][int]$PolarsThreads = 2
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$python = Join-Path $Project '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "pythonw.exe not found: $python (run uv sync first)" }

$user = "$env:USERDOMAIN\$env:USERNAME"
$logFile = Join-Path $LogDir 'bulk-download-task.log'
$arguments = "tools\bulk_download.py --universe `"$Universe`" --log-file `"$logFile`" --polars-threads $PolarsThreads"
$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $Project
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $RepeatMinutes) -RepetitionDuration (New-TimeSpan -Days $Days)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Priority 7 `
    -ExecutionTimeLimit (New-TimeSpan -Days 2)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

if ($PSCmdlet.ShouldProcess($TaskName, 'Register per-user scheduled task')) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Description 'quant-research step 8 bulk download (resumable, gated)' -Force |
        Select-Object TaskName, State
}
