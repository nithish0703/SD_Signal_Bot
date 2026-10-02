# Walk-forward study (smc_wf.py + workflow mode "smc_wf" + FVG/OB overlap feature in bot.py) -> dev only.
$ErrorActionPreference = "Stop"
function Run($cmd) {
    Write-Host ">> git $cmd" -ForegroundColor Cyan
    & git @($cmd -split ' ')
    if ($LASTEXITCODE -ne 0) { Write-Host "STOPPED: 'git $cmd' failed. Send me a screenshot." -ForegroundColor Red; exit 1 }
}
Set-Location $PSScriptRoot
foreach ($f in "bot_wf.py","smc_wf.py","bot.yml.new") { Move-Item $f "$env:TEMP\$f" -Force }
Run "checkout dev"
Run "pull"
Move-Item "$env:TEMP\bot_wf.py" bot.py -Force
Move-Item "$env:TEMP\smc_wf.py" smc_wf.py -Force
Move-Item "$env:TEMP\bot.yml.new" .github\workflows\bot.yml -Force
Run "add bot.py smc_wf.py .github/workflows/bot.yml"
Run "commit -m walk-forward-entry-quality-study-(smc_wf)"
Run "push"
Write-Host "DONE. Actions -> Run workflow -> Branch: dev -> mode: smc_wf (coins 50)" -ForegroundColor Green
