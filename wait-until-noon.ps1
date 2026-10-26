param([Parameter(Mandatory=$true)][string]$Approval)
$ErrorActionPreference='Stop'
if(-not $env:SUPER_MARKET_API_KEY){throw 'SUPER_MARKET_API_KEY is unavailable.'}
if($env:LIVE_TRADING -ne 'I_APPROVE_THIS_DRY_RUN'){throw 'LIVE_TRADING approval guard is absent.'}
$tz=[TimeZoneInfo]::FindSystemTimeZoneById('Eastern Standard Time')
$targetLocal=[datetime]::SpecifyKind([datetime]'2026-10-01 12:00:00',[DateTimeKind]::Unspecified)
$targetUtc=[TimeZoneInfo]::ConvertTimeToUtc($targetLocal,$tz)
Write-Host "Waiting until $targetLocal Eastern. Run .\stop.ps1 to stop immediately."
while([datetime]::UtcNow -lt $targetUtc){
  if(Test-Path -LiteralPath "$PSScriptRoot\STOP"){Write-Host 'STOP detected; exiting without trading.';exit 0}
  Start-Sleep -Seconds 5
}
if(Test-Path -LiteralPath "$PSScriptRoot\STOP"){Write-Host 'STOP detected; exiting without trading.';exit 0}
& "$PSScriptRoot\live.ps1" -Approval $Approval
