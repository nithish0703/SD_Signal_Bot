# Copy the new-format bot.py from dev to main (live bot). Run only after the dev test looked good.
$ErrorActionPreference = "Stop"
function Run($cmd) {
    Write-Host ">> git $cmd" -ForegroundColor Cyan
    & git @($cmd -split ' ')
    if ($LASTEXITCODE -ne 0) { Write-Host "STOPPED: 'git $cmd' failed. Send me a screenshot." -ForegroundColor Red; exit 1 }
}
Set-Location $PSScriptRoot
Run "checkout main"
Run "pull"
Run "checkout origin/dev -- bot.py"
Run "commit -m telegram:-new-message-format"
Run "push"
Run "checkout dev"
Write-Host "DONE (main). The running scan picks up the new bot.py at its next check (within 15 min)." -ForegroundColor Green
