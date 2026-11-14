param([switch]$Strict)
$ErrorActionPreference='Stop'
Set-Location -LiteralPath (Resolve-Path (Join-Path $PSScriptRoot '..\..'))
$python=$env:PYTHON
if(-not $python){$python='python'}
& $python -X utf8 -B tools/check_web_contract.py --require-compare
if($LASTEXITCODE -ne 0){exit $LASTEXITCODE}
node --check web/app.js
if($LASTEXITCODE -ne 0){exit $LASTEXITCODE}
node docs/demo/check_frontend.cjs
exit $LASTEXITCODE
