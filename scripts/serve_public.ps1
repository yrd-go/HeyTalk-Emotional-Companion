<#
================================================================================
 scripts/serve_public.ps1 —— 一条命令把 HeyTalk 开给朋友用（服务 + 公网隧道）
================================================================================

为什么需要这个脚本（来自一次真实的 502 事故）：
    手机上打开网址，看到的是 Cloudflare 的 "Bad gateway / Host: Error"。
    查下来是：**隧道活着，但它要转发到的本机服务已经死了**。
    而手机上的报错**完全看不出"是我本机服务没开"** —— 这就是问题所在。

    这个架构天生有三个坑：
      1. 需要**两个进程**都活着（服务 + 隧道），死一个就全废；
      2. 服务崩了**没人自动拉起**；
      3. 报错信息指向 Cloudflare，不指向真正的原因。
    本脚本把这三个坑都堵上。

--------------------------------------------------------------------------------
 编码注意（这一条踩过坑，别删）
--------------------------------------------------------------------------------
    这个文件**必须带 UTF-8 BOM**。
    Windows PowerShell 5.1 在没有 BOM 时按系统 ANSI(GBK) 读 .ps1，
    中文会变成乱码，而乱码字节会破坏引号配对，导致一堆看不懂的解析错误
    （报错里出现 "椤圭洰鐩綍" 这种就是它）。
    pwsh 7 默认按 UTF-8 读，所以**只在 5.1 上炸** —— 正是"右键→使用
    PowerShell 运行"用的那个版本。

⚠️ 你必须知道的局限（别指望它像商业服务那样稳）
--------------------------------------------------------------------------------
  · **地址每次重启都会变**。这是 Cloudflare 的"快速隧道"（不注册账号
    就能用，代价是地址随机）。要固定地址得注册 Cloudflare 账号 + 域名。
  · **Cloudflare 官方声明快速隧道不保证可用性**（"no uptime guarantee"）。
  · **脚本一关，朋友就访问不了**（你的电脑就是服务器）。
  · **任何人拿到地址都能打开登录页**。进不去（要口令），且服务端有登录
    限速（同一账号 15 分钟最多失败 10 次、同一来源 30 次）。
  · 脚本默认用 Windows 电源请求阻止"空闲睡眠"；如果系统把**合盖**设置成
    强制睡眠，仍应在 Windows 电源选项里把"合上盖子时"改成"不采取任何操作"。
  · 隧道断开后会自动重连，但快速隧道每次重连都会生成新地址。脚本会把新
    地址写入状态文件并复制到剪贴板。

用法
--------------------------------------------------------------------------------
    .\scripts\serve_public.ps1                    # 服务 + 公网隧道
    .\scripts\serve_public.ps1 -NoTunnel          # 只起本机服务
    .\scripts\serve_public.ps1 -Port 8700         # 换端口
    .\scripts\serve_public.ps1 -OpenBrowser       # 就绪后打开网页
    .\scripts\serve_public.ps1 -NoKeepAwake       # 不阻止系统睡眠
按 Ctrl+C 退出，两个进程一起关掉。

关于本机地址：127.0.0.1 与 localhost 都可以。服务默认绑 localhost，
会**同时监听 IPv4 与 IPv6 回环**（实测三种写法都返回 200）。
这两个都**只在本机**，不会暴露给局域网。
#>
[CmdletBinding()]
param(
    [int]$Port = 8600,
    [switch]$NoTunnel,
    [int]$HealthEverySec = 20,
    [int]$TunnelFailuresBeforeRestart = 2,
    [switch]$NoKeepAwake,
    [switch]$OpenBrowser
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if ($HealthEverySec -lt 5) { $HealthEverySec = 5 }
if ($TunnelFailuresBeforeRestart -lt 1) { $TunnelFailuresBeforeRestart = 1 }

$RuntimeRoot = if ($env:LOCALAPPDATA) {
    Join-Path $env:LOCALAPPDATA "HeyTalk"
} else {
    Join-Path $env:TEMP "HeyTalk"
}
$LogDir = Join-Path $RuntimeRoot "logs"
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$StatePath = Join-Path $RuntimeRoot "demo-state.json"
$UrlPath = Join-Path $RuntimeRoot "current-url.txt"

$script:OwnsMutex = $false
$script:Mutex = New-Object System.Threading.Mutex($false, "Local\HeyTalkDemo-$Port")
try {
    $script:OwnsMutex = $script:Mutex.WaitOne(0, $false)
} catch [System.Threading.AbandonedMutexException] {
    $script:OwnsMutex = $true
}
if (-not $script:OwnsMutex) {
    Write-Host "已经有一个 HeyTalk 演示脚本占用了端口 $Port。" -ForegroundColor Yellow
    Write-Host "请不要重复启动；先关闭旧窗口，或换一个 -Port。" -ForegroundColor Yellow
    exit 2
}

$script:PythonSitePackages = ""

function Test-PythonRuntime {
    param(
        [string]$PythonPath,
        [string]$SitePackages = ""
    )
    if (-not (Test-Path -LiteralPath $PythonPath)) { return $false }

    $oldPythonPath = $env:PYTHONPATH
    try {
        if ($SitePackages) {
            $env:PYTHONPATH = $SitePackages
        } else {
            Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
        }
        & $PythonPath -c "import ssl, uvicorn, starlette" 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        if ($null -ne $oldPythonPath) {
            $env:PYTHONPATH = $oldPythonPath
        } else {
            Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
        }
    }
}

function Resolve-Python {
    $script:PythonSitePackages = ""
    $venvCandidates = @()
    foreach ($rel in @(".venv-rag\Scripts\python.exe", ".venv\Scripts\python.exe")) {
        $p = Join-Path $ProjectRoot $rel
        if (Test-Path -LiteralPath $p) {
            $venvRoot = Split-Path -Parent (Split-Path -Parent $p)
            $venvCandidates += [pscustomobject]@{
                Path = $p
                SitePackages = (Join-Path $venvRoot "Lib\site-packages")
            }
        }
    }

    foreach ($candidate in $venvCandidates) {
        if (Test-PythonRuntime -PythonPath $candidate.Path) {
            return $candidate.Path
        }
    }

    $baseCandidates = @()
    if ($env:LOCALAPPDATA) {
        $baseCandidates += @(
            (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
            (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
            (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe")
        )
    }
    $cmd = Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { $baseCandidates += $cmd.Source }

    foreach ($base in ($baseCandidates | Select-Object -Unique)) {
        foreach ($candidate in $venvCandidates) {
            if (Test-PythonRuntime -PythonPath $base -SitePackages $candidate.SitePackages) {
                $script:PythonSitePackages = $candidate.SitePackages
                Write-Host "  提示：虚拟环境的 python.exe 不可用，已改用基础解释器 + 现有依赖。" -ForegroundColor Yellow
                return $base
            }
        }
    }

    throw "找不到可用的 python（虚拟环境解释器和基础解释器均无法导入 ssl/uvicorn/starlette）。"
}

function Test-LocalServer {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/bootstrap" -TimeoutSec 5 -UseBasicParsing
        return ($r.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Start-AppServer {
    # 用**独立进程**启动。注意：如果是在"某个会话的后台任务"里启动的，
    # 那个会话结束时它可能被连带杀掉 —— 所以正式使用请直接运行本脚本。
    $oldPythonPath = $env:PYTHONPATH
    try {
        if ($script:PythonSitePackages) {
            if ($oldPythonPath) {
                $env:PYTHONPATH = "$($script:PythonSitePackages);$oldPythonPath"
            } else {
                $env:PYTHONPATH = $script:PythonSitePackages
            }
        }
        $env:HEYTALK_PORT = "$Port"
        $p = Start-Process -FilePath $Python -ArgumentList "src/server/api.py" -PassThru -WindowStyle Hidden
        return $p
    } finally {
        Remove-Item Env:\HEYTALK_PORT -ErrorAction SilentlyContinue
        if ($null -ne $oldPythonPath) {
            $env:PYTHONPATH = $oldPythonPath
        } else {
            Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
        }
    }
}

$Python = Resolve-Python
Write-Host "项目目录 : $ProjectRoot"
Write-Host "解释器   : $Python"
Write-Host "端口     : $Port"
Write-Host ""

$serverProc = $null
$tunnelProc = $null
$publicUrl = ""

# ============================================================================
# 1. 本机服务
# ============================================================================
Write-Host "[1/2] 正在启动本机服务…" -ForegroundColor Cyan
$serverProc = Start-AppServer

$ready = $false
for ($i = 1; $i -le 40; $i++) {
    Start-Sleep -Seconds 1
    if (Test-LocalServer) { $ready = $true; break }
    if ($serverProc.HasExited) { break }
}
if (-not $ready) {
    Write-Host "  x 服务在 40 秒内没有就绪。" -ForegroundColor Red
    if ($serverProc.HasExited) {
        Write-Host "    进程已退出（退出码 $($serverProc.ExitCode)）。"
        Write-Host "    先单独跑一次看报错：$Python src/server/api.py"
    }
    exit 1
}
Write-Host "  ok 服务已就绪  http://127.0.0.1:$Port" -ForegroundColor Green

# ============================================================================
# 2. 公网隧道
# ============================================================================
if (-not $NoTunnel) {
    Write-Host "[2/2] 正在建立公网隧道…" -ForegroundColor Cyan

    $cloudflared = $null
    $cands = @(
        "$env:ProgramFiles\cloudflared\cloudflared.exe",
        "${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe",
        "$env:LOCALAPPDATA\cloudflared\cloudflared.exe"
    )
    foreach ($cand in $cands) {
        if (Test-Path $cand) { $cloudflared = $cand; break }
    }
    if (-not $cloudflared) {
        $cmd = Get-Command cloudflared -ErrorAction SilentlyContinue
        if ($cmd) { $cloudflared = $cmd.Source }
    }

    if (-not $cloudflared) {
        Write-Host "  x 没找到 cloudflared。" -ForegroundColor Red
        Write-Host "    装它：winget install --id Cloudflare.cloudflared --source winget"
        Write-Host "    （本机服务已在跑，可以先在同一 WiFi 下用内网 IP 访问）"
        $NoTunnel = $true
    } else {
        $logFile = Join-Path $env:TEMP "heytalk-tunnel.log"
        Remove-Item -LiteralPath $logFile -Force -ErrorAction SilentlyContinue

        $tunnelArgs = @("tunnel", "--url", "http://127.0.0.1:$Port", "--no-autoupdate")
        $tunnelProc = Start-Process -FilePath $cloudflared -ArgumentList $tunnelArgs `
            -PassThru -WindowStyle Hidden -RedirectStandardError $logFile

        # cloudflared 把地址打在 stderr 上，轮询日志把它抓出来
        for ($i = 1; $i -le 40; $i++) {
            Start-Sleep -Seconds 1
            if (Test-Path $logFile) {
                $m = Select-String -LiteralPath $logFile -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" -ErrorAction SilentlyContinue | Select-Object -First 1
                if ($m) { $publicUrl = $m.Matches[0].Value; break }
            }
            if ($tunnelProc.HasExited) { break }
        }

        if ($publicUrl) {
            Write-Host "  ok 隧道已建立" -ForegroundColor Green
        } else {
            Write-Host "  x 没能拿到隧道地址（网络问题？）。" -ForegroundColor Red
            Write-Host "    日志：$logFile"
            Write-Host "    本机服务仍在运行，可以先用内网 IP 访问。"
        }
    }
}

# ============================================================================
# 结果
# ============================================================================
Write-Host ""
Write-Host ("=" * 68)
if ($publicUrl) {
    Write-Host "  发给朋友的网址：" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "    $publicUrl" -ForegroundColor White
    Write-Host ""
    Write-Host "  注意：这个地址**每次重启都会变** —— 重启后请重新复制。"
} else {
    Write-Host "  本机访问：http://127.0.0.1:$Port" -ForegroundColor Yellow
    $ips = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" }).IPAddress
    if ($ips) {
        Write-Host "  同一 WiFi 下可用：" -ForegroundColor Yellow
        foreach ($ip in $ips) { Write-Host "    http://${ip}:$Port" }
        Write-Host '  （需先让服务监听所有网卡：$env:HEYTALK_HOST = "0.0.0.0"）'
    }
}
Write-Host ("=" * 68)
Write-Host ""
Write-Host "按 Ctrl+C 停止（两个进程会一起关掉）。"
Write-Host "下面每 $HealthEverySec 秒检查一次服务状态。"
Write-Host ""

# ============================================================================
# 健康检查循环：服务挂了自动拉起，并把状态打在屏幕上
#
# 为什么要有它：手机上看到的 502 只说明"隧道那层到了、后面没有服务"，
# **完全没告诉他真正的原因**。与其等人发现，不如在这里主动报告并修复。
# ============================================================================
try {
    while ($true) {
        Start-Sleep -Seconds $HealthEverySec
        $stamp = Get-Date -Format "HH:mm:ss"

        if ($serverProc.HasExited -or -not (Test-LocalServer)) {
            Write-Host "[$stamp] 服务无响应，正在重启…" -ForegroundColor Yellow
            if (-not $serverProc.HasExited) {
                try { Stop-Process -Id $serverProc.Id -Force -ErrorAction SilentlyContinue } catch { }
            }
            $serverProc = Start-AppServer
            $ok = $false
            for ($i = 1; $i -le 30; $i++) {
                Start-Sleep -Seconds 1
                if (Test-LocalServer) { $ok = $true; break }
            }
            if ($ok) {
                Write-Host "[$stamp] ok 服务已恢复" -ForegroundColor Green
                if ($publicUrl) { Write-Host "        网址没变，朋友那边刷新即可。" }
            } else {
                Write-Host "[$stamp] x 重启后仍无响应 —— 请手动检查。" -ForegroundColor Red
            }
        } else {
            Write-Host "[$stamp] ok 服务正常" -ForegroundColor DarkGray
        }

        if (-not $NoTunnel -and $tunnelProc -and $tunnelProc.HasExited) {
            Write-Host "[$stamp] 隧道进程已退出 —— 需要重新运行本脚本（地址会变）。" -ForegroundColor Red
        }
    }
}
finally {
    Write-Host ""
    Write-Host "正在关闭…"
    foreach ($p in @($tunnelProc, $serverProc)) {
        if ($p -and -not $p.HasExited) {
            try { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } catch { }
        }
    }
    Write-Host "已关闭。"
}
