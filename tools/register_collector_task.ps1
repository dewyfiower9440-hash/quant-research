<#
.SYNOPSIS
    Registers (or updates) the per-user Task Scheduler entry that keeps the T0 collectors running.
.DESCRIPTION
    Standard-user task (no admin, no stored password): runs only while this user is logged on.
    Triggers: at logon, then repeated every 5 minutes so a crashed supervisor is restarted.
    MultipleInstances=IgnoreNew plus the collectors' own instance locks prevent duplicates.
    Runs pythonw.exe (no console window); logs go to C:\dev\qr-data\logs\<role>.log.
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'qr-collector-supervisor',
    [string]$Project = 'C:\dev\quant-research',
    [string]$LogDir = 'C:\dev\qr-data\logs'
)
$ErrorActionPreference = 'Stop'
$python = Join-Path $Project '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "pythonw.exe not found: $python (run uv sync first)" }

$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $python `
    -Argument "-m quant_research.capture.run supervisor --log-dir `"$LogDir`"" `
    -WorkingDirectory $Project
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)).Repetition
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'quant-research T0 raw capture supervisor (plan step 10, ADR-008)' -Force |
    Select-Object TaskName, State
