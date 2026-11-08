<#
.SYNOPSIS
    READ-ONLY elevated probe: SSD reliability counters and Windows Time status/configuration.
.DESCRIPTION
    Makes no changes. Needs elevation only because Get-StorageReliabilityCounter and
    w32tm /query are denied to standard users (verified 2026-11-08). Writes one JSON file.
#>
[CmdletBinding()]
param([ValidateNotNullOrEmpty()][string]$OutDir = 'C:\dev\qr-data\measurements')
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$disks = Get-PhysicalDisk | ForEach-Object {
    $c = $_ | Get-StorageReliabilityCounter
    [pscustomobject]@{
        Disk = $_.FriendlyName; Temperature = $c.Temperature; TemperatureMax = $c.TemperatureMax
        Wear = $c.Wear; PowerOnHours = $c.PowerOnHours; ReadErrorsTotal = $c.ReadErrorsTotal
        WriteErrorsTotal = $c.WriteErrorsTotal; ReadLatencyMax = $c.ReadLatencyMax
    }
}
$status = (& w32tm.exe /query /status /verbose) -join "`n"
$statusCode = $LASTEXITCODE
$config = (& w32tm.exe /query /configuration) -join "`n"
$configCode = $LASTEXITCODE

$report = [pscustomobject]@{
    At = (Get-Date).ToUniversalTime().ToString('o'); Elevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    Disks = @($disks); W32tmStatusExit = $statusCode; W32tmStatus = $status; W32tmConfigExit = $configCode; W32tmConfig = $config
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$path = Join-Path $OutDir ("admin-probe-{0}.json" -f (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ'))
[IO.File]::WriteAllText($path, ($report | ConvertTo-Json -Depth 4), [Text.UTF8Encoding]::new($false))
Write-Output $path
