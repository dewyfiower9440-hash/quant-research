<#
.SYNOPSIS
    Registers (or updates) the per-user Task Scheduler entry that runs the step-9 Upbit candle backfill.
.DESCRIPTION
    Same pattern as register_bulk_task.ps1 (ADR-014): standard user, no admin, independent of any Claude
    Code session, priority 7 (below normal). Starts ~1 minute after registration and repeats every
    $RepeatMinutes minutes for $Days days; a run stopped by a gate (exit 3), an Upbit 418 (exit 5) or a
    crash resumes on the next tick from the catalog. MultipleInstances=IgnoreNew plus the runner's lock
    (run/upbit-backfill.lock) prevent duplicates. After completion every tick exits at once.
    Remove when done: Unregister-ScheduledTask -TaskName qr-upbit-backfill -Confirm:$false
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateNotNullOrEmpty()][string]$TaskName = 'qr-upbit-backfill',
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Container })][string]$Project = 'C:\dev\auto\code',
    [ValidateNotNullOrEmpty()][string]$LogDir = 'C:\dev\qr-data\logs',
    [ValidateRange(5, 1440)][int]$RepeatMinutes = 30,
    [ValidateRange(1, 30)][int]$Days = 7,
    [ValidateRange(1, 9)][double]$Rate = 6
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$python = Join-Path $Project '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "pythonw.exe not found: $python (run uv sync first)" }

$user = "$env:USERDOMAIN\$env:USERNAME"
$logFile = Join-Path $LogDir 'upbit-backfill-task.log'
$arguments = "tools\upbit_candles_backfill.py --rate $Rate --log-file `"$logFile`""
$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $Project
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $RepeatMinutes) -RepetitionDuration (New-TimeSpan -Days $Days)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Priority 7 `
    -ExecutionTimeLimit (New-TimeSpan -Days 2)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

if ($PSCmdlet.ShouldProcess($TaskName, 'Register per-user scheduled task')) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Description 'quant-research step 9 Upbit 1m candle backfill (resumable, gated)' -Force |
        Select-Object TaskName, State
}
