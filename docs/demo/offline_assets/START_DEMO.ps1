$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$candidates = @()
$launcher = Get-Command py.exe -ErrorAction SilentlyContinue
if ($launcher) { $candidates += @{File=$launcher.Source; Args=@('-3')} }
$python = Get-Command python.exe -ErrorAction SilentlyContinue
if ($python) { $candidates += @{File=$python.Source; Args=@()} }
if (Get-Command Get-AppxPackage -ErrorAction SilentlyContinue) {
    Get-AppxPackage -Name 'OpenAI.CodexPrimaryRuntime*' -ErrorAction SilentlyContinue | ForEach-Object {
        $candidate = Join-Path $_.InstallLocation 'dependencies\python\python.exe'
        if (Test-Path -LiteralPath $candidate) { $candidates += @{File=$candidate; Args=@()} }
    }
}
$selected = $null
foreach ($candidate in $candidates) {
    try {
        $probeArgs = @($candidate.Args) + @('-c', 'import sys; assert sys.version_info >= (3,11)')
        & $candidate.File @probeArgs 2>$null
        if ($LASTEXITCODE -eq 0) { $selected=$candidate; break }
    } catch { }
}
if (-not $selected) { throw '没有找到可用的 Python 3.11+。请安装 Python 后重试；也可直接打开 web\cover.html 浏览离线内容。' }
$port = $null
foreach ($candidatePort in 8124..8134) {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $candidatePort)
    try { $listener.Start(); $port=$candidatePort; break } catch { } finally { $listener.Stop() }
}
if ($null -eq $port) { throw '8124–8134 端口均不可用，请关闭其他演示服务后重试。' }
$url = "http://127.0.0.1:$port/cover.html"
Write-Host "日新 · 智慧考研助手  |  $url" -ForegroundColor Green
Write-Host '正在启动本地服务。关掉窗口即可停止；无需联网或 API key。'
# Browser opens on user invocation of this launcher; server remains in this window.
Start-Process $url
$serveArgs = @($selected.Args) + @('-X','utf8','-B','-u','app/serve.py','--port',"$port",'--allow-tutor')
& $selected.File @serveArgs
