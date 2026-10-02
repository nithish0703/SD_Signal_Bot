# Reset paper-trading results on main (closed trades + cancelled count). Keeps 'sent' (duplicate protection).
# FIRST cancel the running main scan in GitHub Actions, so it does not push state.json at the same time.
$ErrorActionPreference = "Stop"
function Run($cmd) {
    Write-Host ">> git $cmd" -ForegroundColor Cyan
    & git @($cmd -split ' ')
    if ($LASTEXITCODE -ne 0) { Write-Host "STOPPED: 'git $cmd' failed. Send me a screenshot." -ForegroundColor Red; exit 1 }
}
Set-Location $PSScriptRoot
Run "checkout main"
Run "pull"
$path = Join-Path $PSScriptRoot "state.json"
$s = Get-Content $path -Raw -Encoding UTF8 | ConvertFrom-Json
Write-Host ("Before: closed={0} open={1} cancelled={2}" -f @($s.closed).Count, @($s.open.PSObject.Properties).Count, $s.cancelled)
$s.closed = @()
$s.cancelled = 0
$s | Add-Member -NotePropertyName paper_since -NotePropertyValue "2026-10-01" -Force
$json = $s | ConvertTo-Json -Depth 30
[IO.File]::WriteAllText($path, $json, (New-Object System.Text.UTF8Encoding($false)))
$chk = Get-Content $path -Raw -Encoding UTF8 | ConvertFrom-Json
Write-Host ("After:  closed={0} cancelled={1} paper_since={2}" -f @($chk.closed).Count, $chk.cancelled, $chk.paper_since) -ForegroundColor Green
Run "add state.json"
Run "commit -m paper-results-reset-(start-2026-10-01)"
Run "push"
Run "checkout dev"
Write-Host "DONE. Now: Actions -> Run workflow -> Branch: main -> mode: scan" -ForegroundColor Green
