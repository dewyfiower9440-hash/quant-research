<#
.SYNOPSIS
    Registers (or updates) the per-user Task Scheduler entry that runs the T1 extras bulk download.
.DESCRIPTION
    Same pattern as register_bulk_task.ps1 (ADR-014): standard user, no admin, no stored password,
    priority 7 (below normal), repeats every $RepeatMinutes minutes for $Days days, IgnoreNew.
    tools/bulk_extras.py shares run/bulk.lock with the klines download, so a tick that finds the klines
    run (or another extras run) active exits 2 and the next tick tries again. A finished run writes
    bulk/complete-extras-<universe>.json and every later tick exits at once.
    Remove when done: Unregister-ScheduledTask -TaskName qr-bulk-extras -Confirm:$false
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateNotNullOrEmpty()][string]$TaskName = 'qr-bulk-extras',
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Container })][string]$Project = 'C:\dev\quant-research',
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })][string]$Universe = 'C:\dev\qr-data\bulk\universe-20261109.json',
    [ValidateNotNullOrEmpty()][string]$LogDir = 'C:\dev\qr-data\logs',
    [ValidateRange(5, 1440)][int]$RepeatMinutes = 30,
    [ValidateRange(1, 30)][int]$Days = 10,
    [ValidateRange(1, 8)][int]$PolarsThreads = 2
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$python = Join-Path $Project '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "pythonw.exe not found: $python (run uv sync first)" }

$user = "$env:USERDOMAIN\$env:USERNAME"
$logFile = Join-Path $LogDir 'bulk-extras-task.log'
$arguments = "tools\bulk_extras.py --universe `"$Universe`" --log-file `"$logFile`" --polars-threads $PolarsThreads"
$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $Project
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $RepeatMinutes) -RepetitionDuration (New-TimeSpan -Days $Days)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Priority 7 `
    -ExecutionTimeLimit (New-TimeSpan -Days 3)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

if ($PSCmdlet.ShouldProcess($TaskName, 'Register per-user scheduled task')) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Description 'quant-research T1 extras bulk (funding, bookDepth, metrics, spot klines)' -Force |
        Select-Object TaskName, State
}
