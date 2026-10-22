<#
  BowlerDepot kiosk: undo kiosk-lockdown.ps1 (runbook 6ei). Run from an
  ADMIN account in PowerShell opened with "Run as administrator", then
  restart. Removes the Edge policies and the edge-swipe setting that the
  lockdown script added. It does not remove the Windows kiosk account --
  do that in Settings > Accounts > Other users > Kiosk > Remove kiosk.
#>
#Requires -RunAsAdministrator
$edge = 'HKLM:\SOFTWARE\Policies\Microsoft\Edge'
if (Test-Path $edge) { Remove-Item -Path $edge -Recurse -Force }
$edgeUi = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\EdgeUI'
if (Test-Path $edgeUi) { Remove-ItemProperty -Path $edgeUi -Name 'AllowEdgeSwipe' -ErrorAction SilentlyContinue }
Write-Host 'Kiosk lockdown removed. Restart the PC.' -ForegroundColor Yellow
