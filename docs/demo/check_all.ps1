# C-group demo self-check runner.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File docs/demo/check_all.ps1
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $root

Write-Host "== check_pages.py ==" -ForegroundColor Cyan
python docs/demo/check_pages.py
if ($LASTEXITCODE -ne 0) { Write-Host "PAGES: FAIL" -ForegroundColor Red; exit 1 }

Write-Host "== check_live.js ==" -ForegroundColor Cyan
node docs/demo/check_live.js
if ($LASTEXITCODE -ne 0) { Write-Host "LIVE: FAIL" -ForegroundColor Red; exit 1 }

Write-Host "ALL CHECKS PASS" -ForegroundColor Green
exit 0
