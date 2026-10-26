$ErrorActionPreference='Stop'
if(-not $env:SUPER_MARKET_API_KEY){throw 'Set SUPER_MARKET_API_KEY to an API key with read scope.'}
$python=Join-Path $PSScriptRoot '.venv313\Scripts\python.exe'
if(-not (Test-Path -LiteralPath $python)){throw 'Project environment missing.'}
& $python "$PSScriptRoot\election_forecast.py"
if($LASTEXITCODE -ne 0){exit $LASTEXITCODE}
& $python "$PSScriptRoot\trader.py" dry-run
