# C1 filter (sweep <=1 ATR + sweep candle reclaims) -> dev smc_filters.json only. Main is not touched.
$ErrorActionPreference = "Stop"
function Run($cmd) {
    Write-Host ">> git $cmd" -ForegroundColor Cyan
    & git @($cmd -split ' ')
    if ($LASTEXITCODE -ne 0) { Write-Host "STOPPED: 'git $cmd' failed. Send me a screenshot." -ForegroundColor Red; exit 1 }
}
Set-Location $PSScriptRoot
Move-Item smc_filters_c1.json "$env:TEMP\smc_filters_c1.json" -Force
Run "checkout dev"
Run "pull"
Move-Item "$env:TEMP\smc_filters_c1.json" smc_filters.json -Force
Run "add smc_filters.json"
Run "commit -m dev:-add-C1-filter-(sweep-1ATR-+-sweep-candle-reclaim)"
Run "push"
Write-Host "DONE (dev only). Now run the C1 account backtest on dev." -ForegroundColor Green
