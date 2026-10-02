# Experiment D (smc_expd.py + workflow mode "smc_expd") -> dev only.
$ErrorActionPreference = "Stop"
function Run($cmd) {
    Write-Host ">> git $cmd" -ForegroundColor Cyan
    & git @($cmd -split ' ')
    if ($LASTEXITCODE -ne 0) { Write-Host "STOPPED: 'git $cmd' failed. Send me a screenshot." -ForegroundColor Red; exit 1 }
}
Set-Location $PSScriptRoot
foreach ($f in "smc_expd.py","bot.yml.new") { Move-Item $f "$env:TEMP\$f" -Force }
Run "checkout dev"
Run "pull"
Move-Item "$env:TEMP\smc_expd.py" smc_expd.py -Force
Move-Item "$env:TEMP\bot.yml.new" .github\workflows\bot.yml -Force
Run "add smc_expd.py .github/workflows/bot.yml"
Run "commit -m experiment-D:-C1-+-MSS/FVG/fill-combos-(smc_expd)"
Run "push"
Write-Host "DONE. Actions -> Run workflow -> Branch: dev -> mode: smc_expd" -ForegroundColor Green
