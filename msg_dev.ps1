# New Telegram message format -> dev branch (bot.py only). Stops on the first error.
$ErrorActionPreference = "Stop"
function Run($cmd) {
    Write-Host ">> git $cmd" -ForegroundColor Cyan
    & git @($cmd -split ' ')
    if ($LASTEXITCODE -ne 0) { Write-Host "STOPPED: 'git $cmd' failed. Send me a screenshot." -ForegroundColor Red; exit 1 }
}
Set-Location $PSScriptRoot
foreach ($f in "dev_spot.ps1","restore.ps1") { if (Test-Path $f) { Remove-Item $f } }
Run "checkout dev"
Run "pull"
Copy-Item bot_new.py bot.py -Force
Remove-Item bot_new.py
Run "add bot.py"
Run "commit -m telegram:-new-message-format"
Run "push"
Write-Host "DONE (dev). Actions -> Run workflow -> Branch: dev -> mode: test" -ForegroundColor Green
