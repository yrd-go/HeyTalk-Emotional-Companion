[CmdletBinding()]
param(
    [int]$Port = 8088,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Showcase = Join-Path $ProjectRoot "showcase"
$Index = Join-Path $Showcase "index.html"

if (-not (Test-Path -LiteralPath $Index)) {
    throw "找不到展示页：$Index"
}

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    throw "端口 $Port 已被占用。请换一个端口，或先关闭占用它的程序。"
}

$python = (Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $python) { throw "找不到 python。" }

$server = Start-Process -FilePath $python `
    -ArgumentList @("-m", "http.server", "$Port", "--bind", "127.0.0.1", "--directory", $Showcase) `
    -PassThru -WindowStyle Hidden

$url = "http://127.0.0.1:$Port/"
try {
    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 300
        try {
            $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -eq 200) { $ready = $true; break }
        } catch { }
    }
    if (-not $ready) { throw "展示页启动超时。" }

    Write-Host ""
    Write-Host "HeyTalk 展示页已启动：" -ForegroundColor Green
    Write-Host "  $url" -ForegroundColor White
    Write-Host ""
    if (-not $NoBrowser) { Start-Process $url }
    Write-Host "按 Ctrl+C 停止本地预览。"

    while (-not $server.HasExited) { Start-Sleep -Seconds 2 }
} finally {
    if ($server -and -not $server.HasExited) {
        try { Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue } catch { }
    }
}
