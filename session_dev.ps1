# Session diagnostic (new smc_time.py, same workflow mode "smc_time") -> dev only. Live main untouched.
$ErrorActionPreference = "Stop"
function Run($cmd) {
    Write-Host ">> git $cmd" -ForegroundColor Cyan
    & git @($cmd -split ' ')
    if ($LASTEXITCODE -ne 0) { Write-Host "STOPPED: 'git $cmd' failed. Send me a screenshot." -ForegroundColor Red; exit 1 }
}
Set-Location $PSScriptRoot
Move-Item smc_time.py "$env:TEMP\smc_time.py" -Force
Run "checkout dev"
Run "pull"
Move-Item "$env:TEMP\smc_time.py" smc_time.py -Force
Run "add smc_time.py"
Run "commit -m session-diagnostic-(A+B+C-frozen,-8-time-windows)"
Run "push"
Write-Host "DONE. Actions -> Run workflow -> Branch: dev -> mode: smc_time" -ForegroundColor Green
