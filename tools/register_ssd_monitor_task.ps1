<#
.SYNOPSIS
    Registers the 10-minute read-only SSD temperature monitor (run from an ELEVATED PowerShell once).
.DESCRIPTION
    Least privilege for the job: this user's own elevated token (RunLevel Highest), only while logged
    on (no stored password, not SYSTEM). The monitor reads disk counters and writes two files under
    C:\dev\qr-data; it changes no OS setting. conhost --headless avoids a console flash every run.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateNotNullOrEmpty()][string]$TaskName = 'qr-ssd-temp-monitor',
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })][string]$Script = 'C:\dev\quant-research\tools\ssd_temp_monitor.ps1',
    [ValidateRange(1, 60)][int]$EveryMinutes = 10
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\conhost.exe" `
    -Argument "--headless powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$Script`""
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $user
$repeating = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest

if ($PSCmdlet.ShouldProcess($TaskName, 'Register elevated read-only SSD monitor task')) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($atLogon, $repeating) -Settings $settings `
        -Principal $principal -Description 'quant-research read-only SSD temperature monitor (decision 3, 2026-11-09)' -Force |
        Select-Object TaskName, State
}
