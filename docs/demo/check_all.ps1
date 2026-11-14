# C-group demo self-check runner.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File docs/demo/check_all.ps1
$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
try { chcp 65001 > $null } catch { }
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $root

Write-Host "== check_pages.py ==" -ForegroundColor Cyan
python docs/demo/check_pages.py
if ($LASTEXITCODE -ne 0) { Write-Host "PAGES: FAIL" -ForegroundColor Red; exit 1 }

Write-Host "== check_live.js ==" -ForegroundColor Cyan
node docs/demo/check_live.js
if ($LASTEXITCODE -ne 0) { Write-Host "LIVE: FAIL" -ForegroundColor Red; exit 1 }

Write-Host "== check_compare.js ==" -ForegroundColor Cyan
node docs/demo/check_compare.js
if ($LASTEXITCODE -ne 0) { Write-Host "COMPARE: FAIL" -ForegroundColor Red; exit 1 }

Write-Host "== check_live_http.js ==" -ForegroundColor Cyan
node docs/demo/check_live_http.js
if ($LASTEXITCODE -ne 0) { Write-Host "LIVE_HTTP: FAIL" -ForegroundColor Red; exit 1 }

# Fifth suite: real Chrome/Edge via CDP (layout / clicks / path / navigation).
# Self-hosted static server, no external deps; SKIPs gracefully if no browser found.
Write-Host "== check_browser.js ==" -ForegroundColor Cyan
node docs/demo/check_browser.js
if ($LASTEXITCODE -ne 0) { Write-Host "BROWSER: FAIL" -ForegroundColor Red; exit 1 }

Write-Host "ALL CHECKS PASS" -ForegroundColor Green
exit 0
