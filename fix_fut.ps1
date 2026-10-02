# Futures backtest data fix (daily files for the last 45 days) -> dev branch only.
$ErrorActionPreference = "Stop"
function Run($cmd) {
    Write-Host ">> git $cmd" -ForegroundColor Cyan
    & git @($cmd -split ' ')
    if ($LASTEXITCODE -ne 0) { Write-Host "STOPPED: 'git $cmd' failed. Send me a screenshot." -ForegroundColor Red; exit 1 }
}
Set-Location $PSScriptRoot
Run "checkout dev"
Run "pull"
Copy-Item bot_fix.py bot.py -Force
Remove-Item bot_fix.py
Run "add bot.py"
Run "commit -m futures-data:-daily-files-for-last-45-days-(previous-month-not-published-yet)"
Run "push"
Write-Host "DONE. Actions -> Run workflow -> dev -> account_futures, days 30" -ForegroundColor Green
