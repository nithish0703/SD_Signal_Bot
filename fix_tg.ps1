# Telegram fix: escape "<" in study messages (smc_expd.py, smc_wf.py) -> dev only.
$ErrorActionPreference = "Stop"
function Run($cmd) {
    Write-Host ">> git $cmd" -ForegroundColor Cyan
    & git @($cmd -split ' ')
    if ($LASTEXITCODE -ne 0) { Write-Host "STOPPED: 'git $cmd' failed. Send me a screenshot." -ForegroundColor Red; exit 1 }
}
Set-Location $PSScriptRoot
foreach ($f in "smc_expd_fix.py","smc_wf_fix2.py") { Move-Item $f "$env:TEMP\$f" -Force }
Run "checkout dev"
Run "pull"
Move-Item "$env:TEMP\smc_expd_fix.py" smc_expd.py -Force
Move-Item "$env:TEMP\smc_wf_fix2.py" smc_wf.py -Force
Run "add smc_expd.py smc_wf.py"
Run "commit -m studies:-escape-HTML-in-Telegram-text"
Run "push"
Write-Host "DONE. Actions -> Run workflow -> Branch: dev -> mode: smc_expd" -ForegroundColor Green
