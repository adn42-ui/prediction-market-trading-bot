$ErrorActionPreference='Stop'
$python=Join-Path $PSScriptRoot '.venv313\Scripts\python.exe'
if(-not (Test-Path -LiteralPath $python)){throw 'Project environment missing. Run: py -3.13 -m venv .venv313; .\.venv313\Scripts\python.exe -m pip install -r requirements.txt'}
if(-not $env:SUPER_MARKET_API_KEY){throw 'SUPER_MARKET_API_KEY is missing from this PowerShell process.'}
$env:PYTHONUTF8='1'
& $python (Join-Path $PSScriptRoot 'election_forecast.py') @args
exit $LASTEXITCODE
