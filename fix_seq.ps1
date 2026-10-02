# Backtests use cooldown/breaker only when live does (smc_wf.py + spot_smc.py) -> dev only.
$ErrorActionPreference = "Stop"
function Run($cmd) {
    Write-Host ">> git $cmd" -ForegroundColor Cyan
    & git @($cmd -split ' ')
    if ($LASTEXITCODE -ne 0) { Write-Host "STOPPED: 'git $cmd' failed. Send me a screenshot." -ForegroundColor Red; exit 1 }
}
Set-Location $PSScriptRoot
foreach ($f in "smc_wf_fix.py","spot_smc_fix.py") { Move-Item $f "$env:TEMP\$f" -Force }
Run "checkout dev"
Run "pull"
Move-Item "$env:TEMP\smc_wf_fix.py" smc_wf.py -Force
Move-Item "$env:TEMP\spot_smc_fix.py" spot_smc.py -Force
Run "add smc_wf.py spot_smc.py"
Run "commit -m backtests:-sequence-rules-only-when-active-(same-as-live)"
Run "push"
Write-Host "DONE. Actions -> Run workflow -> Branch: dev -> mode: smc_wf" -ForegroundColor Green
