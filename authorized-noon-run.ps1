param([Parameter(Mandatory=$true)][string]$RunId)
$ErrorActionPreference='Stop'
if(-not $env:SUPER_MARKET_API_KEY){throw 'SUPER_MARKET_API_KEY is unavailable to this waiter process.'}
$python=Join-Path $PSScriptRoot '.venv313\Scripts\python.exe'
if(-not (Test-Path -LiteralPath $python)){throw 'Project environment missing.'}
& $python "$PSScriptRoot\preflight.py"
if($LASTEXITCODE -ne 0){throw 'Read-only GET /account authentication failed; waiter not started.'}
$env:MAX_ORDER_SIZE='1000';$env:MAX_EXPOSURE_PER_MARKET='2000';$env:MAX_TOTAL_EXPOSURE='10000'
$env:PYTHONUTF8='1'
$tz=[TimeZoneInfo]::FindSystemTimeZoneById('Eastern Standard Time')
$targetLocal=[datetime]::SpecifyKind([datetime]'2026-10-01 12:00:00',[DateTimeKind]::Unspecified)
$targetUtc=[TimeZoneInfo]::ConvertTimeToUtc($targetLocal,$tz)
"Waiting for $targetLocal Eastern. Run .\stop.ps1 to stop." | Tee-Object -FilePath "$PSScriptRoot\noon-run.log" -Append
while([datetime]::UtcNow -lt $targetUtc){
    if(Test-Path -LiteralPath "$PSScriptRoot\STOP"){'STOP detected; exiting.' | Tee-Object -FilePath "$PSScriptRoot\noon-run.log" -Append;exit 0}
    Start-Sleep -Seconds 5
}
if(Test-Path -LiteralPath "$PSScriptRoot\STOP"){'STOP detected; exiting.' | Tee-Object -FilePath "$PSScriptRoot\noon-run.log" -Append;exit 0}
$startInfo=New-Object System.Diagnostics.ProcessStartInfo
$startInfo.FileName=$python
$startInfo.Arguments=('"{0}" authorized-once --run-id "{1}"' -f "$PSScriptRoot\trader.py",$RunId)
$startInfo.WorkingDirectory=$PSScriptRoot
$startInfo.UseShellExecute=$false
$startInfo.CreateNoWindow=$true
$startInfo.RedirectStandardOutput=$true
$startInfo.RedirectStandardError=$true
$process=New-Object System.Diagnostics.Process
$process.StartInfo=$startInfo
if(-not $process.Start()){throw 'Failed to start trader.py.'}
$stdoutTask=$process.StandardOutput.ReadToEndAsync()
$stderrTask=$process.StandardError.ReadToEndAsync()
$process.WaitForExit()
$stdout=$stdoutTask.GetAwaiter().GetResult()
$stderr=$stderrTask.GetAwaiter().GetResult()
if($stdout){Add-Content -LiteralPath "$PSScriptRoot\noon-run.log" -Value $stdout}
if($stderr){Add-Content -LiteralPath "$PSScriptRoot\noon-run.log" -Value $stderr}
$traderExitCode=$process.ExitCode
if($traderExitCode -ne 0){
    "trader.py exited with code $traderExitCode; authorization state was not reset." | Add-Content -LiteralPath "$PSScriptRoot\noon-run.log"
    throw "trader.py exited with code $traderExitCode. See noon-run.log."
}
