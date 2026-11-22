<#
.SYNOPSIS
    Registers (or updates) the per-user Task Scheduler entry that consumes the campaign's data request queue
    (v2.3 step 10, ADR-049 decision 5).
.DESCRIPTION
    Same pattern as register_campaign_task.ps1: standard user, no admin, no stored password, priority 7 (below
    normal), repeats every $RepeatMinutes minutes for $Days days, IgnoreNew. Each tick runs tools/run_data_queue.py:
    one step for the first unfinished tier of qr-data\run\data-queue.json (a pilot with its size projection and the
    200 GB lake cap check, or the resumable full download, killed at $MaxHours and resumed next tick). A tier over
    the cap is left as skipped_cap for the user; tier states are in qr-data\run\data-queue-state.json. A full tier
    run shares run\bulk.lock with the other bulk downloads (a tick that meets one waits for the next tick); a pilot
    uses its own lock and may run beside them; the external sources have no lock (one tick at a time by IgnoreNew).
    Output is appended to $LogDir\data-queue.log. Dry run first:
        powershell -NoProfile -File C:/dev/auto/code/tools/register_data_queue_task.ps1 -WhatIf
    Remove: Unregister-ScheduledTask -TaskName qr-data-queue -Confirm:$false
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateNotNullOrEmpty()][string]$TaskName = 'qr-data-queue',
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Container })][string]$Project = 'C:\dev\auto\code',
    [ValidateNotNullOrEmpty()][string]$LogDir = 'C:\dev\qr-data\logs',
    [ValidateRange(30, 1440)][int]$RepeatMinutes = 120,
    [ValidateRange(1, 90)][int]$Days = 30,
    [ValidateRange(0.5, 12)][double]$MaxHours = 1.75
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$python = Join-Path $Project '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "pythonw.exe not found: $python (run uv sync first)" }
if ($MaxHours * 60 -ge $RepeatMinutes) { throw "-MaxHours ($MaxHours h) must end before the next tick ($RepeatMinutes min)" }

$user = "$env:USERDOMAIN\$env:USERNAME"
$logFile = Join-Path $LogDir 'data-queue.log'
$arguments = "tools\run_data_queue.py --max-hours $MaxHours --log-file `"$logFile`""
$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $Project
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $RepeatMinutes) -RepetitionDuration (New-TimeSpan -Days $Days)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Priority 7 `
    -ExecutionTimeLimit (New-TimeSpan -Hours ([Math]::Ceiling($MaxHours) + 1))
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

Write-Verbose "log: $logFile; tier states: qr-data\run\data-queue-state.json"
if ($PSCmdlet.ShouldProcess($TaskName, "Register per-user scheduled task: $arguments")) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Description 'quant-research data queue: one tier step per tick (v2.3)' -Force |
        Select-Object TaskName, State
}
