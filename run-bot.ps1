$ErrorActionPreference='Stop'
# Refreshes the forecast (writes fair_values.json), then starts sigbot.
#   .\run-bot.ps1              dry run: logs intended orders to trades.jsonl
#   .\run-bot.ps1 --live       real orders (refused while the STOP file exists)
$python=Join-Path $PSScriptRoot '.venv313\Scripts\python.exe'
if(-not (Test-Path -LiteralPath $python)){throw 'Project environment missing. Run: py -3.13 -m venv .venv313; .\.venv313\Scripts\python.exe -m pip install -r requirements.txt'}
if(-not $env:SUPER_MARKET_API_KEY){throw 'SUPER_MARKET_API_KEY is missing from this PowerShell process.'}
$env:PYTHONUTF8='1'
Set-Location $PSScriptRoot
& $python (Join-Path $PSScriptRoot 'election_forecast.py')
if($LASTEXITCODE -ne 0){throw 'Forecast failed; not starting the bot.'}
& $python -m sigbot run @args
exit $LASTEXITCODE
