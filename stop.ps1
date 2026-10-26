New-Item -ItemType File -Force -Path "$PSScriptRoot\STOP" | Out-Null
Write-Host 'STOP file created. No further live orders will be submitted.' -ForegroundColor Red
