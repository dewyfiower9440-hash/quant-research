<#
.SYNOPSIS
    Registers (or updates) the per-user Task Scheduler entry that judges the validator outbox (v2.3 step 9, B10).
.DESCRIPTION
    Same pattern as register_campaign_task.ps1: standard user, no admin, no stored password, priority 7 (below
    normal), repeats every $RepeatMinutes minutes for $Days days, IgnoreNew. Each tick runs tools/run_validator.py:
    it reads research.db read-only, judges the pending requests on the sealed holdouts (epoch 2 budget k = 20),
    writes only validator.db / validator-trials.db and holds run/validator.lock (a tick that finds another validator
    exits 2). A tick with nothing pending ends at once. Output (ack bits, refusals, tracebacks) is appended to
    $LogDir\validator.log. Dry run first:
        powershell -NoProfile -File C:/dev/auto/code/tools/register_validator_task.ps1 -WhatIf
    Remove: Unregister-ScheduledTask -TaskName qr-validator -Confirm:$false
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateNotNullOrEmpty()][string]$TaskName = 'qr-validator',
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Container })][string]$Project = 'C:\dev\auto\code',
    [ValidateNotNullOrEmpty()][string]$LogDir = 'C:\dev\qr-data\logs',
    [ValidateRange(10, 1440)][int]$RepeatMinutes = 60,
    [ValidateRange(1, 90)][int]$Days = 30,
    [ValidateRange(1, 12)][int]$MaxHours = 6  # a request cut by the limit stays registered and is retried (same seed)
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$python = Join-Path $Project '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "pythonw.exe not found: $python (run uv sync first)" }

$user = "$env:USERDOMAIN\$env:USERNAME"
$logFile = Join-Path $LogDir 'validator.log'
$arguments = "tools\run_validator.py --log-file `"$logFile`""
$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $Project
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $RepeatMinutes) -RepetitionDuration (New-TimeSpan -Days $Days)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Priority 7 `
    -ExecutionTimeLimit (New-TimeSpan -Hours $MaxHours)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

Write-Verbose "log: $logFile"
if ($PSCmdlet.ShouldProcess($TaskName, "Register per-user scheduled task: $arguments")) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Description 'quant-research validator: judge pending requests (v2.3)' -Force |
        Select-Object TaskName, State
}
