param([Parameter(Mandatory=$true)][string]$Approval)
$ErrorActionPreference='Stop'
if(-not $env:SUPER_MARKET_API_KEY){throw 'Set SUPER_MARKET_API_KEY.'}
if($env:LIVE_TRADING -ne 'I_APPROVE_THIS_DRY_RUN'){throw 'Set LIVE_TRADING only after reviewing dry-run-plan.json.'}
$python=Join-Path $PSScriptRoot '.venv313\Scripts\python.exe'
if(-not (Test-Path -LiteralPath $python)){throw 'Project environment missing.'}
& $python "$PSScriptRoot\trader.py" live --approval $Approval
