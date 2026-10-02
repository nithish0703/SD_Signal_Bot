# Full reset on main: closed trades, cancelled count, sent-signal list and open trades. Fresh start from today.
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
Write-Host ("Before: closed={0} open={1} cancelled={2} sent={3}" -f @($s.closed).Count, @($s.open.PSObject.Properties).Count, $s.cancelled, @($s.sent.PSObject.Properties).Count)
$s.closed = @()
$s.cancelled = 0
$s.sent = New-Object PSObject
$s.open = New-Object PSObject
$s | Add-Member -NotePropertyName paper_since -NotePropertyValue "2026-10-01" -Force
$json = $s | ConvertTo-Json -Depth 30
[IO.File]::WriteAllText($path, $json, (New-Object System.Text.UTF8Encoding($false)))
$chk = Get-Content $path -Raw -Encoding UTF8 | ConvertFrom-Json
Write-Host ("After:  closed={0} open={1} cancelled={2} sent={3} paper_since={4}" -f @($chk.closed).Count, @($chk.open.PSObject.Properties).Count, $chk.cancelled, @($chk.sent.PSObject.Properties).Count, $chk.paper_since) -ForegroundColor Green
Run "add state.json"
Run "commit -m full-paper-reset-(start-2026-10-01)"
Run "push"
Run "checkout dev"
Write-Host "DONE. Now: Actions -> Run workflow -> Branch: main -> mode: scan" -ForegroundColor Green
