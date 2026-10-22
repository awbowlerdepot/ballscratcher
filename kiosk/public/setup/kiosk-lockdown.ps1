<#
  BowlerDepot kiosk lockdown (runbook 6ei) -- run ONCE on each kiosk PC, from
  an ADMIN account, in PowerShell opened with "Run as administrator".

  What it does (all reversible with kiosk-unlock.ps1):
    Microsoft Edge (machine policies, apply to every account incl. the kiosk):
      * Only https://kiosk.bowlerdepot.com can be opened as a page, plus the
        YouTube player the kiosk embeds for review videos. Every other site
        is blocked, so a link inside a video can't lead anywhere.
        (These are navigation rules; the kiosk's own images, data and fonts
        load normally.)
      * No first-run screens, sign-in, sync, password/address/card saving,
        translate pop-ups, shopping/rewards/sidebar extras, downloads,
        printing or developer tools.
    Windows:
      * Turns off edge swipes (swiping in from a screen edge), so a customer
        can't open Windows panels on the touchscreen.

  It does NOT set up the kiosk account itself -- that's the Windows "Set up
  a kiosk" wizard in the guide (docs/kiosk-setup-windows.md).
#>
#Requires -RunAsAdministrator
$ErrorActionPreference = 'Stop'

$edge = 'HKLM:\SOFTWARE\Policies\Microsoft\Edge'
New-Item -Path $edge -Force | Out-Null

function Set-Dword([string]$Path, [string]$Name, [int]$Value) {
  New-ItemProperty -Path $Path -Name $Name -Value $Value -PropertyType DWord -Force | Out-Null
}
function Set-List([string]$Path, [string[]]$Items) {
  if (Test-Path $Path) { Remove-Item -Path $Path -Recurse -Force }
  New-Item -Path $Path -Force | Out-Null
  $i = 1
  foreach ($item in $Items) {
    New-ItemProperty -Path $Path -Name "$i" -Value $item -PropertyType String -Force | Out-Null
    $i++
  }
}

# Only the kiosk (and the embedded YouTube player) may load as a page.
# The kiosk's data, image, font and video-stream hosts are listed too: Edge
# applies these rules to page and frame loads, but listing them means the
# kiosk keeps working even if a future Edge applies them more broadly. None
# of them is a page a customer could reach from the kiosk.
Set-List "$edge\URLBlocklist" @('*')
Set-List "$edge\URLAllowlist" @(
  'https://kiosk.bowlerdepot.com',
  'https://www.youtube-nocookie.com/embed',
  'https://api.bowleriq.io',
  'https://img.bowleriq.io',
  'https://bowling-scraper-images-563981859606.s3.amazonaws.com',
  'https://i.ytimg.com',
  'https://googlevideo.com',          # a plain domain covers its subdomains
  'https://fonts.googleapis.com',
  'https://fonts.gstatic.com'
)

# Quiet, single-purpose browser.
Set-Dword $edge 'HideFirstRunExperience' 1
Set-Dword $edge 'BrowserSignin' 0
Set-Dword $edge 'SyncDisabled' 1
Set-Dword $edge 'PasswordManagerEnabled' 0
Set-Dword $edge 'AutofillAddressEnabled' 0
Set-Dword $edge 'AutofillCreditCardEnabled' 0
Set-Dword $edge 'TranslateEnabled' 0
Set-Dword $edge 'EdgeShoppingAssistantEnabled' 0
Set-Dword $edge 'ShowMicrosoftRewards' 0
Set-Dword $edge 'HubsSidebarEnabled' 0
Set-Dword $edge 'EdgeCollectionsEnabled' 0
Set-Dword $edge 'DownloadRestrictions' 3        # block all downloads
Set-Dword $edge 'PrintingEnabled' 0
Set-Dword $edge 'DeveloperToolsAvailability' 2  # not allowed

# Windows: no edge swipes on the touchscreen.
$edgeUi = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\EdgeUI'
New-Item -Path $edgeUi -Force | Out-Null
Set-Dword $edgeUi 'AllowEdgeSwipe' 0

Write-Host ''
Write-Host 'Kiosk lockdown applied. Restart the PC for every setting to take effect.' -ForegroundColor Green
Write-Host 'Check it: open edge://policy in Edge (on this admin account) -- the policies above should be listed.'
