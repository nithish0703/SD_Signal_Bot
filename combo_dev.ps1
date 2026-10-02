# Combo check (sweep <=1 ATR + sweep-candle reclaim, year by year) added to smc_wf.py -> dev only.
$ErrorActionPreference = "Stop"
function Run($cmd) {
    Write-Host ">> git $cmd" -ForegroundColor Cyan
    & git @($cmd -split ' ')
    if ($LASTEXITCODE -ne 0) { Write-Host "STOPPED: 'git $cmd' failed. Send me a screenshot." -ForegroundColor Red; exit 1 }
}
Set-Location $PSScriptRoot
Move-Item smc_wf_combo.py "$env:TEMP\smc_wf_combo.py" -Force
Run "checkout dev"
Run "pull"
Move-Item "$env:TEMP\smc_wf_combo.py" smc_wf.py -Force
Run "add smc_wf.py"
Run "commit -m smc_wf:-combo-check-by-year-and-quarter"
Run "push"
Write-Host "DONE. Actions -> Run workflow -> Branch: dev -> mode: smc_wf" -ForegroundColor Green
