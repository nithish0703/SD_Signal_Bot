# Put A+B+C live on main: copy bot.py + smc_filters.json from dev (nothing else). Retries the push because the
# live loop pushes state.json every 15 minutes.
$ErrorActionPreference = "Stop"
function Run($cmd) {
    Write-Host ">> git $cmd" -ForegroundColor Cyan
    & git @($cmd -split ' ')
    if ($LASTEXITCODE -ne 0) { Write-Host "STOPPED: 'git $cmd' failed. Send me a screenshot." -ForegroundColor Red; exit 1 }
}
Set-Location $PSScriptRoot
Run "fetch origin"
Run "checkout main"
Run "pull"
Run "checkout origin/dev -- bot.py smc_filters.json"
Run "commit -m live:-A+B+C-filters-(C1-+-MSS-6-+-fresh-FVG)-+-fill-window-fix"
$ok = $false
foreach ($i in 1..3) {
    & git push
    if ($LASTEXITCODE -eq 0) { $ok = $true; break }
    Write-Host "Push rejected (bot just saved state). Pulling and retrying ($i/3)..." -ForegroundColor Yellow
    & git pull --rebase
    if ($LASTEXITCODE -ne 0) { Write-Host "STOPPED: pull --rebase failed. Send me a screenshot." -ForegroundColor Red; exit 1 }
}
if (-not $ok) { Write-Host "STOPPED: push failed 3 times. Send me a screenshot." -ForegroundColor Red; exit 1 }
Run "checkout dev"
Write-Host "DONE (main). The live scan picks up A+B+C at its next check (within 15 min)." -ForegroundColor Green
