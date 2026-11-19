<#
.SYNOPSIS
    Registers (or updates) the per-user Task Scheduler entry that keeps the T0 collectors running.
.DESCRIPTION
    Standard-user task (no admin, no stored password): runs only while this user is logged on.
    Triggers: at logon, then repeated every $RepeatMinutes minute(s) so a crashed supervisor is restarted.
    MultipleInstances=IgnoreNew plus the collectors' own instance locks prevent duplicates.
    Runs pythonw.exe (no console window); logs go to C:\dev\qr-data\logs\<role>.log.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateNotNullOrEmpty()][string]$TaskName = 'qr-collector-supervisor',
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Container })][string]$Project = 'C:\dev\auto\code',
    [ValidateNotNullOrEmpty()][string]$LogDir = 'C:\dev\qr-data\logs',
    # Supervisor-death detection latency. 1 min is the Task Scheduler minimum; IgnoreNew makes ticks
    # free while the instance runs (2026-11-08 decision 6).
    [ValidateRange(1, 60)][int]$RepeatMinutes = 1
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$python = Join-Path $Project '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "pythonw.exe not found: $python (run uv sync first)" }

$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $python `
    -Argument "-m quant_research.capture.run supervisor --log-dir `"$LogDir`"" `
    -WorkingDirectory $Project
# F-006: a repetition attached to an at-logon trigger only starts after the next logon, so a crash
# right after registration was never restarted. A time trigger repeating every $RepeatMinutes (no end)
# starts immediately and survives reboots; the at-logon trigger starts the collectors right after login.
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $user
$repeating = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $RepeatMinutes)
$trigger = @($atLogon, $repeating)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

if ($PSCmdlet.ShouldProcess($TaskName, 'Register per-user scheduled task')) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Description 'quant-research T0 raw capture supervisor (plan step 10, ADR-008)' -Force |
        Select-Object TaskName, State
}
