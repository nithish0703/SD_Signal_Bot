# Time-filter test (smc_time.py + workflow mode "smc_time") -> dev only. Live main untouched.
$ErrorActionPreference = "Stop"
function Run($cmd) {
    Write-Host ">> git $cmd" -ForegroundColor Cyan
    & git @($cmd -split ' ')
    if ($LASTEXITCODE -ne 0) { Write-Host "STOPPED: 'git $cmd' failed. Send me a screenshot." -ForegroundColor Red; exit 1 }
}
Set-Location $PSScriptRoot
foreach ($f in "smc_time.py","bot.yml.new") { Move-Item $f "$env:TEMP\$f" -Force }
Run "checkout dev"
Run "pull"
Move-Item "$env:TEMP\smc_time.py" smc_time.py -Force
Move-Item "$env:TEMP\bot.yml.new" .github\workflows\bot.yml -Force
Run "add smc_time.py .github/workflows/bot.yml"
Run "commit -m time-filter-test-(smc_time)"
Run "push"
Write-Host "DONE. Actions -> Run workflow -> Branch: dev -> mode: smc_time" -ForegroundColor Green
