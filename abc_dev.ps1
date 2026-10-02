# A+B+C on dev: C1 + MSS <=6 candles + fresh FVG (fill <=3) and the live fill-window off-by-one fix. Main untouched.
$ErrorActionPreference = "Stop"
function Run($cmd) {
    Write-Host ">> git $cmd" -ForegroundColor Cyan
    & git @($cmd -split ' ')
    if ($LASTEXITCODE -ne 0) { Write-Host "STOPPED: 'git $cmd' failed. Send me a screenshot." -ForegroundColor Red; exit 1 }
}
Set-Location $PSScriptRoot
foreach ($f in "bot_abc.py","smc_filters_abc.json") { Move-Item $f "$env:TEMP\$f" -Force }
Run "checkout dev"
Run "pull"
Move-Item "$env:TEMP\bot_abc.py" bot.py -Force
Move-Item "$env:TEMP\smc_filters_abc.json" smc_filters.json -Force
Run "add bot.py smc_filters.json"
Run "commit -m dev:-A+B+C-(C1-+-MSS-6-+-fresh-FVG)-and-fill-window-fix"
Run "push"
Write-Host "DONE (dev only). Now run account_futures on dev, days 365." -ForegroundColor Green
