$ErrorActionPreference='Stop'
& "$PSScriptRoot\run-forecast.ps1" @args
exit $LASTEXITCODE
