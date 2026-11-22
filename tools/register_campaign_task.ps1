<#
.SYNOPSIS
    Registers (or updates) the per-user Task Scheduler entry that runs an autonomous research campaign (v2.2 B9/B10).
.DESCRIPTION
    Same pattern as register_bulk_extras_task.ps1 (ADR-014): standard user, no admin, no stored password,
    priority 7 (below normal), repeats every $RepeatMinutes minutes for $Days days, IgnoreNew.
    tools/run_campaign.py saves its state after every batch (qr-data/run/campaign-<run>.json), so each tick resumes
    the same --run-id where the last one stopped: after the research gate (exit 3), a reboot or the session's memory
    reaper. A tick that finds another research process (run/research.lock) exits 2 and the next tick tries again.
    Once the campaign is exhausted, every later tick ends at once with the same report.
    Each tick runs at most $MaxHours hours and appends its output (refusals, gate stops, tracebacks) to
    $LogDir\campaign-<run>.log. Stop early: create qr-data\run\STOP-campaign.
    Lists are plain text, comma or space separated, because `powershell -File` does not parse arrays:
        powershell -NoProfile -File C:/dev/auto/code/tools/register_campaign_task.ps1 -RunId c2 -Timeframes "1h,4h"
    Every value is checked here, at registration, not later inside a silent pythonw run.
    Remove when done: Unregister-ScheduledTask -TaskName qr-campaign -Confirm:$false
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateNotNullOrEmpty()][string]$TaskName = 'qr-campaign',
    [ValidatePattern('^[A-Za-z0-9_-]{1,32}$')][string]$RunId = 'c2',
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Container })][string]$Project = 'C:\dev\auto\code',
    [ValidateNotNullOrEmpty()][string]$Timeframes = '4h,1d',
    [ValidateNotNullOrEmpty()][string]$TopM = '100',
    [ValidateNotNullOrEmpty()][string]$Windows = 'w1,w2',
    [ValidateNotNullOrEmpty()][string]$LogDir = 'C:\dev\qr-data\logs',
    [ValidateRange(10, 1440)][int]$RepeatMinutes = 60,
    [ValidateRange(1, 30)][int]$Days = 7,
    [ValidateRange(0.5, 24)][double]$MaxHours = 6,
    [ValidateRange(1, 8)][int]$PolarsThreads = 2
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$python = Join-Path $Project '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "pythonw.exe not found: $python (run uv sync first)" }

function Split-List {
    [OutputType([string[]])]
    param([Parameter(Mandatory)][string]$Text, [Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][string]$Pattern)
    $items = @($Text -split '[,\s]+' | Where-Object { $_ -ne '' })
    if ($items.Count -eq 0) { throw "-$Name is empty" }
    foreach ($item in $items) {
        if ($item -notmatch $Pattern) { throw "-$Name has an invalid value '$item' (allowed: $Pattern)" }
    }
    return , $items
}

$tfList = Split-List -Text $Timeframes -Name 'Timeframes' -Pattern '^(1h|4h|1d)$'
$topList = Split-List -Text $TopM -Name 'TopM' -Pattern '^[1-9][0-9]{0,3}$'
$winList = Split-List -Text $Windows -Name 'Windows' -Pattern '^(w1|w2)$'

$user = "$env:USERDOMAIN\$env:USERNAME"
$report = Join-Path $Project "docs\measurements\campaign-$RunId.md"
$logFile = Join-Path $LogDir "campaign-$RunId.log"
$arguments = "tools\run_campaign.py --run-id $RunId --timeframes $($tfList -join ' ') " +
    "--top-m $($topList -join ' ') --windows $($winList -join ' ') --max-hours $MaxHours " +
    "--polars-threads $PolarsThreads --report `"$report`" --log-file `"$logFile`""
$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $Project
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $RepeatMinutes) -RepetitionDuration (New-TimeSpan -Days $Days)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Priority 7 `
    -ExecutionTimeLimit (New-TimeSpan -Hours ([Math]::Ceiling($MaxHours) + 1))
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

Write-Verbose "log: $logFile; resumable state: qr-data\run\campaign-$RunId.json"
if ($PSCmdlet.ShouldProcess($TaskName, "Register per-user scheduled task: $arguments")) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Description "quant-research autonomous campaign $RunId (v2.2)" -Force |
        Select-Object TaskName, State
}
