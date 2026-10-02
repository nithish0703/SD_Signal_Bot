# Make dev exactly the same as main (normal commit, no force push; old study files stay in git history).
$ErrorActionPreference = "Stop"
function Run($cmd) {
    Write-Host ">> git $cmd" -ForegroundColor Cyan
    & git @($cmd -split ' ')
    if ($LASTEXITCODE -ne 0) { Write-Host "STOPPED: 'git $cmd' failed. Send me a screenshot." -ForegroundColor Red; exit 1 }
}
Set-Location $PSScriptRoot
Run "fetch origin"
Run "checkout dev"
Run "reset --hard origin/dev"
Run "rm -r -q ."
Run "checkout origin/main -- ."
Run "add -A"
Run "commit -m dev:-same-as-main"
Run "push origin dev"
Run "diff --stat origin/main origin/dev"
Write-Host "DONE. dev = main (the line above should be empty)." -ForegroundColor Green
