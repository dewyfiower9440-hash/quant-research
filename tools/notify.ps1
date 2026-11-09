<#
.SYNOPSIS
    Shows a Windows toast for quant-research monitor incidents (plan step 10).
.DESCRIPTION
    Started hidden by the rest worker (quant_research.adapters.notify) when incidents open. Reads the
    title and lines from -AlertFile (JSON written by the worker) so no free text passes through the
    command line. Uses the Windows PowerShell AppUserModelID, which can show toasts without registration.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })][string]$AlertFile
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$alert = Get-Content -LiteralPath $AlertFile -Raw -Encoding UTF8 | ConvertFrom-Json
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
    [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$texts = $template.GetElementsByTagName('text')
$null = $texts.Item(0).AppendChild($template.CreateTextNode([string]$alert.title))
$null = $texts.Item(1).AppendChild($template.CreateTextNode(($alert.lines -join "`n")))
$toast = [Windows.UI.Notifications.ToastNotification]::new($template)
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
