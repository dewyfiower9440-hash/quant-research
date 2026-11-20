<#
.SYNOPSIS
    Registers (or updates) the per-user Task Scheduler entry that keeps the paper-trading process running.
.DESCRIPTION
    Standard-user task (no admin, no stored password, no exchange keys): runs only while this user is logged on.
    Triggers: at logon, then repeated every $RepeatMinutes minute(s) so a crashed process is restarted.
    MultipleInstances=IgnoreNew plus the process's own instance lock prevent duplicates.
    Runs pythonw.exe (no console window); logs go to C:\dev\qr-data\logs\paper.log.
    Phase 6 (design v2.1 section 11): paper only - nothing here can reach an exchange account.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateNotNullOrEmpty()][string]$TaskName = 'qr-paper',
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Container })][string]$Project = 'C:\dev\auto\code',
    [ValidateNotNullOrEmpty()][string]$LogDir = 'C:\dev\qr-data\logs',
    [ValidateRange(1, 60)][int]$RepeatMinutes = 5,
    [ValidateRange(1, 1440)][double]$IntervalMinutes = 10
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$python = Join-Path $Project '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "pythonw.exe not found: $python (run uv sync first)" }
if (-not (Test-Path -LiteralPath $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

$user = "$env:USERDOMAIN\$env:USERNAME"
$log = Join-Path $LogDir 'paper.log'
$action = New-ScheduledTaskAction -Execute $python `
    -Argument "-m quant_research.paper --interval-minutes $IntervalMinutes" `
    -WorkingDirectory $Project
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $user
$repeating = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $RepeatMinutes)
$trigger = @($atLogon, $repeating)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

if ($PSCmdlet.ShouldProcess($TaskName, 'Register per-user scheduled task')) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Description "quant-research paper trading (Phase 6, log $log)" -Force |
        Select-Object TaskName, State
}
