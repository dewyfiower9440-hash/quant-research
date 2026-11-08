<#
.SYNOPSIS
    READ-ONLY SSD temperature monitor (2026-11-09 decision 3). Runs elevated every 10 minutes.
.DESCRIPTION
    Reads the system SSD reliability counters (needs admin; read-only), appends one JSON line to
    <DataRoot>\health\ssd_temp.jsonl and writes the current level to <DataRoot>\run\ssd_state.json.
    Levels must match quant_research.capture.thermal: warn >= 70 C, critical >= 75 C, back below 68 C.
    Changes nothing in the operating system.
#>
[CmdletBinding()]
param([ValidateNotNullOrEmpty()][string]$DataRoot = 'C:\dev\qr-data')
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$WarnC = 70; $CriticalC = 75; $ResumeBelowC = 68

function Get-NextLevel([string]$Previous, [int]$TempC) {
    if ($TempC -ge $CriticalC) { return 'critical' }
    if ($Previous -eq 'critical' -and $TempC -ge $ResumeBelowC) { return 'critical' }
    if ($TempC -ge $WarnC) { return 'warn' }
    if ($Previous -eq 'warn' -and $TempC -ge $ResumeBelowC) { return 'warn' }
    return 'ok'
}

$systemDisk = Get-Partition -DriveLetter C | Get-Disk
$physical = Get-PhysicalDisk | Where-Object { $_.DeviceId -eq [string]$systemDisk.Number }
if ($null -eq $physical) { throw "physical disk for C: (disk $($systemDisk.Number)) not found" }
$counter = $physical | Get-StorageReliabilityCounter -ErrorAction Stop  # access denied without elevation
if ($null -eq $counter -or $null -eq $counter.Temperature) { throw "no temperature reported by $($physical.FriendlyName)" }

$statePath = Join-Path $DataRoot 'run\ssd_state.json'
$previous = 'ok'
if (Test-Path -LiteralPath $statePath) { $previous = (Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json).level }
$temp = [int]$counter.Temperature
$level = Get-NextLevel -Previous $previous -TempC $temp
$atNs = ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) * 1000000

$utf8 = [Text.UTF8Encoding]::new($false)
$healthDir = Join-Path $DataRoot 'health'; New-Item -ItemType Directory -Force -Path $healthDir | Out-Null
$line = [pscustomobject]@{ at_ns = $atNs; disk = $physical.FriendlyName; temp_c = $temp; temp_max_c = $counter.TemperatureMax; wear = $counter.Wear; level = $level } | ConvertTo-Json -Compress
[IO.File]::AppendAllText((Join-Path $healthDir 'ssd_temp.jsonl'), $line + "`n", $utf8)

$runDir = Split-Path -Parent $statePath; New-Item -ItemType Directory -Force -Path $runDir | Out-Null
$tmp = "$statePath.tmp"
[IO.File]::WriteAllText($tmp, ([pscustomobject]@{ at_ns = $atNs; temp_c = $temp; level = $level } | ConvertTo-Json -Compress), $utf8)
Move-Item -LiteralPath $tmp -Destination $statePath -Force
Write-Output $line
