<#
.SYNOPSIS
  智能烹饪调度 Agent —— Windows 服务器一键部署（虚拟环境 + 依赖 + 防火墙 + 开机自启 + 自检）

.DESCRIPTION
  适用于：腾讯云 / 阿里云 轻量应用服务器 · Windows Server + 宝塔面板。
  **在服务器上、以管理员身份**运行。

  做六件事（全部幂等，可以反复执行，不会重复堆配置）：
    1) 找到 Python 并建 .venv、装依赖
    2) 写 .env.local（访问令牌 / 离线开关）—— 用标记块包裹，**不动你手写的内容**
    3) 生成 run-server.cmd（崩溃自动拉起 + 日志落盘）
    4) 放行 Windows 防火墙端口
    5) 注册计划任务（开机自启、SYSTEM 身份、最高权限）
    6) 自检 /health /ready /api/plan，并打印对外链接

  为什么用「计划任务」而不是 NSSM / WinSW：
    后两者都要先从网上下载 exe，国内服务器经常拉不动；计划任务是 Windows 自带的，
    零依赖。开机自启 + SYSTEM 身份 + 无限时长，行为和一个 Windows 服务等价。

.PARAMETER Root
  项目所在目录，默认 C:\cooking-agent。脚本会自动往下找一层（解压多套一层目录也能认）。

.PARAMETER Port
  监听端口，默认 8137。

.PARAMETER Token
  访问令牌。留空 = 自动生成一个随机串（会打印出来）。

.PARAMETER NoAuth
  关闭鉴权（不写 FT_AUTH_TOKEN）。**只适合本机/内网，公网千万别这么干。**
  用这个开关而不是 `-Token -`：用减号当哨兵值在 PowerShell 里绑定行为不明确。

.PARAMETER Offline
  完全离线档（FT_LLM=off）。排程质量与耗时不变，只是 AI 对话降级成规则提示。
  2 核 2G 的机器推荐加上。

.PARAMETER TrustProxy
  加了 Nginx 反向代理之后再带上它，否则限流会把所有访客算成同一个 IP。

.PARAMETER RateLimit
  形如 600/60 = 每个 IP 每 60 秒最多 600 次。留空 = 不限流。

.PARAMETER ReadOnly
  关闭菜谱库的增删改（FT_ALLOW_RECIPE_WRITE=off）。把带令牌的链接发给很多人时建议加。

.PARAMETER Python
  指定 python.exe 完整路径；不给就自动从 PATH 和 py 启动器里找。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File C:\cooking-agent\deploy\windows\setup.ps1 -Offline

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File C:\cooking-agent\deploy\windows\setup.ps1 -TrustProxy -RateLimit 600/60
#>
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
  [string]$Root = "C:\cooking-agent",
  [int]$Port = 8137,
  [string]$Token = "",
  [switch]$NoAuth,
  [switch]$Offline,
  [switch]$TrustProxy,
  [string]$RateLimit = "",
  [switch]$ReadOnly,
  [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$TaskName = "CookingAgent"
$RuleName = "Cooking Agent TCP $Port"

function Say  ($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Ok   ($m) { Write-Host "  [OK] $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "  [!]  $m" -ForegroundColor Yellow }
function Die  ($m) { Write-Host "  [X]  $m" -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  智能烹饪调度 Agent  ·  Windows 服务器部署" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# =============================================================== 0. 定位项目
Say "0/6 定位项目目录"
$root = $Root
if (-not (Test-Path (Join-Path $root "app\api\main.py"))) {
  # 常见情形：zip 解压出来多套了一层（C:\cooking-agent\cooking-agent\app\...）
  $found = $null
  foreach ($sub in (Get-ChildItem -Path $root -Directory -ErrorAction SilentlyContinue)) {
    if (Test-Path (Join-Path $sub.FullName "app\api\main.py")) { $found = $sub.FullName; break }
  }
  if ($found) { $root = $found; Warn "在 $Root 下多套了一层，已自动改用 $root" }
}
if (-not (Test-Path (Join-Path $root "app\api\main.py"))) {
  Die "在 $root 下找不到 app\api\main.py —— 请确认代码已解压到 $Root（解压后应能看到 app\ data\ scripts\ requirements.txt）"
}
Ok "项目目录：$root"

# =============================================================== 1. Python
Say "1/6 查找 Python（需要 3.10 以上）"
$basePy = ""
if ($Python) {
  if (-not (Test-Path $Python)) { Die "指定的 Python 不存在：$Python" }
  $basePy = $Python
} else {
  $cands = @()
  $g = Get-Command python -ErrorAction SilentlyContinue
  if ($g) { $cands += $g.Source }
  if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($v in @("-3.14", "-3.13", "-3.12", "-3.11", "-3")) {
      $p = (& py $v -c "import sys;print(sys.executable)" 2>$null)
      if ($LASTEXITCODE -eq 0 -and $p) { $cands += "$p".Trim(); break }
    }
  }
  foreach ($d in @("C:\Python314\python.exe", "C:\Python313\python.exe",
                   "C:\Python312\python.exe", "C:\Python311\python.exe",
                   "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe",
                   "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe")) {
    if (Test-Path $d) { $cands += $d }
  }
  $basePy = $cands | Where-Object { $_ } | Select-Object -First 1
}
if (-not $basePy) {
  Die @"
找不到 python.exe。请先安装 Python 3.14（64 位），安装时**务必勾选
     Add python.exe to PATH
下载：https://www.python.org/downloads/windows/  （选 Windows installer 64-bit）
装完**新开一个** PowerShell 再重跑本脚本；或直接指定路径：
     -Python C:\Python314\python.exe
"@
}
$verStr = (& $basePy -c "import sys;print('%d.%d.%d' % sys.version_info[:3])").Trim()
$verOk  = (& $basePy -c "import sys;print(1 if sys.version_info >= (3,10) else 0)").Trim()
if ($verOk -ne "1") { Die "Python 版本过低：$verStr（需要 3.10 以上）" }
Ok "Python $verStr  ($basePy)"

# =============================================================== 2. 虚拟环境
Say "2/6 创建虚拟环境并安装依赖"
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
  & $basePy -m venv (Join-Path $root ".venv")
  if (-not (Test-Path $venvPy)) { Die "创建虚拟环境失败（$root\.venv）" }
  Ok "已创建 $root\.venv"
} else {
  Ok ".venv 已存在，跳过创建"
}

& $venvPy -c "import fastapi, uvicorn, pydantic, ortools" 2>$null
if ($LASTEXITCODE -ne 0) {
  $lockPath = Join-Path $root "requirements.lock.txt"
  $reqPath  = Join-Path $root "requirements.txt"
  # 锁文件是在 CPython 3.14 / Windows 上生成的；换了 Python 版本硬套会装不上
  # （numpy / pandas 这类包对 Python 小版本敏感），所以先判版本再决定用哪份。
  $is314 = (& $venvPy -c "import sys;print(1 if sys.version_info[:2]==(3,14) else 0)").Trim()
  $useLock = ($is314 -eq "1") -and (Test-Path $lockPath)

  & $venvPy -m pip install --upgrade pip --quiet
  Say "  安装依赖（含 ortools，约 100MB，慢是正常的，请耐心等）"
  if ($useLock) {
    Ok "Python 3.14 + requirements.lock.txt ⇒ 可以逐字复刻开发环境"
    & $venvPy -m pip install -r $lockPath
    if ($LASTEXITCODE -ne 0) {
      Warn "锁文件装不上，改用 requirements.txt 重试"
      & $venvPy -m pip install -r $reqPath
    }
  } else {
    if ($is314 -ne "1") { Warn "Python 不是 3.14，跳过锁文件，改用 requirements.txt" }
    & $venvPy -m pip install -r $reqPath
  }
  if ($LASTEXITCODE -ne 0) {
    Die "依赖安装失败。多半是网络问题，可换国内镜像重试：
     $venvPy -m pip install -r requirements.txt -i https://mirrors.cloud.tencent.com/pypi/simple"
  }
  & $venvPy -c "import fastapi, uvicorn, pydantic, ortools"
  if ($LASTEXITCODE -ne 0) { Die "依赖装完了但 import 失败，请看上面的报错" }
  Ok "依赖安装完成"
} else {
  Ok "依赖已满足，跳过安装"
}

# =============================================================== 3. .env.local
Say "3/6 写运行配置（.env.local）"
if ($NoAuth -or $Token -eq "-") {
  $Token = ""
  Warn "已按你的要求**关闭鉴权** —— 只有本机/内网能这么干，公网暴露会被任意改配置"
} elseif (-not $Token) {
  $Token = [guid]::NewGuid().ToString("N")
  Ok "已自动生成访问令牌（下面会打印，请复制保存）"
}

$lines = New-Object System.Collections.Generic.List[string]
$lines.Add("# >>> cooking-agent setup.ps1 自动生成（不要手改这一段，重跑脚本会覆盖）>>>")
if ($Token) { $lines.Add("FT_AUTH_TOKEN=$Token") }
if ($Offline) {
  $lines.Add("FT_LLM=off")
  $lines.Add("FT_AGENT_LLM=off")
  $lines.Add("FT_NOTIFY_LLM=off")
  $lines.Add("FT_WARM_LLM=off")
  $lines.Add("# 离线档：排程质量与耗时不变（走规则 + 离线语义先验缓存），只有 AI 对话降级")
  # ⚠ FT_LLM=off 是**全局总闸**，只在 llm.enabled() 里读，**设置页改不动它**。
  #   不把这一点喊出来，用户会在设置页里把「接入方式」切成远程、看到"保存成功"，
  #   然后 AI 对话永远回"离线模式"，去翻模型配置、翻网络，怎么都想不到是这一行。
  Warn "已启用完全离线档：写入 FT_LLM=off —— **大模型被整体关闭，接远程模型也不会生效**"
  Warn "  排程不受影响。若之后要用远程模型（如土豆中转站）：删掉 .env.local 里的 FT_LLM=off，"
  Warn "  再重跑本脚本（**不要**加 -Offline）。"
} else {
  # 出厂层（app/agent/llm_defaults.json）已预置可用的远程配置并随包一起走 ——
  # 解压启动即可用，这里**不再**引导用户去设置页填地址/密钥。
  # 关掉启动探测：探测预算按 provider 分档，远端档只有 1.5s，而首次调用要建
  # TLS 连接（实测冷启动常落在 1.5s 上下）。一旦超时，闸门只会把「在线闭环」
  # 关掉、排程退回规则兜底 —— 等于白少一路 AI 策略。关掉探测则闭环每轮都真试。
  $lines.Add("FT_WARM_LLM=off")
  $lines.Add("# 关掉启动探测（出厂层已带远程配置，开箱即用，无需手填）")
  $lines.Add("# 想换成自己的地址/密钥：页面 → 设置 → 模型接入 → 选「远程」→ 填好保存")
}
if ($TrustProxy) { $lines.Add("FT_TRUST_PROXY=1") }
if ($RateLimit)  { $lines.Add("FT_RATE_LIMIT=$RateLimit") }
if ($ReadOnly)   { $lines.Add("FT_ALLOW_RECIPE_WRITE=off") }
$lines.Add("# <<< cooking-agent setup.ps1 自动生成 <<<")

$envLocal = Join-Path $root ".env.local"
$old = ""
if (Test-Path $envLocal) { $old = Get-Content -Raw -Encoding UTF8 $envLocal }
if ($null -eq $old) { $old = "" }
$kept = New-Object System.Collections.Generic.List[string]
$skipping = $false
foreach ($l in ($old -split "`r?`n")) {
  if ($l.StartsWith("# >>> cooking-agent")) { $skipping = $true;  continue }
  if ($l.StartsWith("# <<< cooking-agent")) { $skipping = $false; continue }
  if (-not $skipping) { $kept.Add($l) }
}
$newText = (($kept -join "`r`n").TrimEnd()) + "`r`n`r`n" + ($lines -join "`r`n") + "`r`n"
# ⚠ 必须**不带 BOM**：llm.py 的 _parse_dotenv 用 encoding="utf-8" 逐行 split("=")，
#   带 BOM 时第一个键会变成 "\uFEFFFT_AUTH_TOKEN"，静默读不到，鉴权就失效了。
[IO.File]::WriteAllText($envLocal, $newText, (New-Object Text.UTF8Encoding $false))
Ok ".env.local 已更新（UTF-8 无 BOM）"

# =============================================================== 4. 启动器
Say "4/6 生成启动器 run-server.cmd"
$logDir = Join-Path $root "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

# ⚠ 这个 .cmd 必须**纯 ASCII**且**CRLF 换行**（cmd.exe 按字节偏移读文件，
#   多字节字符会让它错位，把半行当命令执行）。项目根目录也是 ASCII，安全。
$cmdText = @"
@echo off
rem Auto-generated by deploy\windows\setup.ps1 -- keep this file pure ASCII + CRLF.
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
:loop
echo [%date% %time%] starting uvicorn on port $Port >> "logs\server.log"
".venv\Scripts\python.exe" -m uvicorn app.api.main:app --host 0.0.0.0 --port $Port --workers 1 >> "logs\server.log" 2>&1
echo [%date% %time%] uvicorn exited with code %ERRORLEVEL%, restarting in 10s >> "logs\server.log"
timeout /t 10 /nobreak >nul
goto loop
"@
$cmdText = ($cmdText -replace "`r?`n", "`r`n")
$cmdPath = Join-Path $root "run-server.cmd"
[IO.File]::WriteAllText($cmdPath, $cmdText, [Text.Encoding]::ASCII)
Ok "已写入 $cmdPath（崩溃后 10 秒自动重启，日志落 logs\server.log）"

# ⚠ 必须单 worker：设备配置、排程历史、熔断器、在线闸门全是**进程内状态**。
#   多 worker 会出现"改了设备配置，下一次排程又变回去了"。

# =============================================================== 5. 防火墙 + 计划任务
Say "5/6 放行防火墙并注册开机自启"
$rule = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
if ($rule) {
  Ok "Windows 防火墙规则已存在（TCP $Port）"
} else {
  try {
    New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow `
      -Protocol TCP -LocalPort $Port -Profile Any | Out-Null
    Ok "已放行 Windows 防火墙 TCP $Port"
  } catch {
    Warn "New-NetFirewallRule 不可用，改用 netsh"
    netsh advfirewall firewall add rule name="$RuleName" dir=in action=allow protocol=TCP localport=$Port | Out-Null
    Ok "已放行 Windows 防火墙 TCP $Port（netsh）"
  }
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 2
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
  Ok "已停掉并注销旧的计划任务"
}

$action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\cmd.exe" `
  -Argument "/c `"$cmdPath`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
  -Settings $settings -Principal $principal | Out-Null
Ok "计划任务「$TaskName」已注册（开机自启 / SYSTEM / 最高权限）"

Start-ScheduledTask -TaskName $TaskName
Ok "已启动"

# =============================================================== 6. 自检
Say "6/6 自检（最多等 60 秒）"
$headers = @{}
if ($Token) { $headers["X-Auth-Token"] = $Token }

$healthy = $false
for ($i = 0; $i -lt 30; $i++) {
  Start-Sleep -Seconds 2
  try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health" -UseBasicParsing -TimeoutSec 5
    if ($r.StatusCode -eq 200) { $healthy = $true; break }
  } catch { }
}
if (-not $healthy) {
  Write-Host ""
  Warn "60 秒内 /health 没有响应。请按顺序排查："
  Write-Host "    1) 看日志： Get-Content -Tail 40 -Encoding UTF8 `"$root\logs\server.log`"" -ForegroundColor Yellow
  Write-Host "    2) 看任务： Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo" -ForegroundColor Yellow
  Write-Host "    3) 常见原因：端口被占用 / 依赖没装全 / 解压目录不对" -ForegroundColor Yellow
  exit 1
}
Ok "/health 200"

try {
  $ready = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/ready" -Headers $headers -TimeoutSec 20
  if ($ready.ready) { Ok "/ready 就绪（菜库 $($ready.recipes) 道）" }
  else { Warn "/ready 未就绪：$($ready | ConvertTo-Json -Compress)" }
} catch { Warn "/ready 探测失败：$($_.Exception.Message)" }

try {
  $t0 = Get-Date
  # ⚠ 这两个菜名**必须在官方菜库里**。曾经写成「番茄炒蛋」——菜库里根本没有这道菜
  #   （只有「芝士番茄土豆泥」），于是排程静默少菜、recipeCount 报 1，
  #   看起来像部署失败，实际是自检自己写错了菜名。
  #   这里选的是分属两种设备、且都能稳定排出来的：家常鲈鱼（灶具）+ 豉汁蒸草鱼（蒸箱）。
  $probeBody = '[{"id":1,"name":"家常鲈鱼"},{"id":2,"name":"豉汁蒸草鱼"}]'
  $plan = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/plan" -Method Post `
    -Headers $headers -ContentType "application/json; charset=utf-8" `
    -Body ([Text.Encoding]::UTF8.GetBytes($probeBody)) `
    -TimeoutSec 60
  $ms = [math]::Round(((Get-Date) - $t0).TotalMilliseconds)
  if (-not $plan.overview) {
    Warn "/api/plan 返回了但没有 overview 字段"
  } elseif ([int]$plan.overview.recipeCount -lt 2) {
    # 请求 2 道、只排 1 道 = 有菜名没命中菜库（后端只在日志里留痕，响应里看不出来）
    Warn "/api/plan 请求 2 道菜但只排了 $($plan.overview.recipeCount) 道 —— 多半是菜名没命中菜库"
    Warn "    看后端日志确认： Get-Content -Tail 40 -Encoding UTF8 `"$root\logs\server.log`""
  } else {
    Ok "/api/plan 通过（${ms}ms，$($plan.overview.recipeCount) 道菜，$($plan.overview.finishTime) 上桌）"
  }
} catch { Warn "/api/plan 探测失败：$($_.Exception.Message)" }

# =============================================================== 打印链接
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  部署完成" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green

$pubIp = "<你的公网IP>"
try {
  # 腾讯云的内网 metadata 服务，免外网、秒回；拿不到就退回占位符
  $pubIp = (Invoke-RestMethod -Uri "http://metadata.tencentyun.com/latest/meta-data/public-ipv4" `
            -TimeoutSec 3).Trim()
} catch { }

Write-Host ""
Write-Host "  本机自测： http://127.0.0.1:$Port/app" -ForegroundColor White
if ($Token) {
  Write-Host "  对外链接： http://${pubIp}:$Port/app?token=$Token" -ForegroundColor Yellow
  Write-Host ""
  Write-Host "  访问令牌： $Token" -ForegroundColor Yellow
  Write-Host "  ⚠ 上面这条链接就是别人访问用的，令牌就在里面，别发到公开群。" -ForegroundColor DarkYellow
  Write-Host "  ⚠ 若还想让别人「不用带令牌」直接开，请改用 Nginx 反代 + 在 Nginx 层做限制。" -ForegroundColor DarkYellow
} else {
  Write-Host "  对外链接： http://${pubIp}:$Port/app   （**未开启鉴权**）" -ForegroundColor Yellow
  Write-Host "  ⚠ 任何知道这个地址的人都能改你的模型配置、删你的菜谱。" -ForegroundColor Red
}
Write-Host ""
Write-Host "  ⚠ 还有一步不能忘：**腾讯云控制台的防火墙也要放行 TCP $Port**" -ForegroundColor Red
Write-Host "     轻量应用服务器 → 服务器详情 → 防火墙 → 添加规则 → TCP / $Port / 0.0.0.0/0" -ForegroundColor Red
Write-Host ""
Write-Host "  常用运维命令：" -ForegroundColor Cyan
Write-Host "    看日志   Get-Content -Tail 60 -Encoding UTF8 `"$root\logs\server.log`""
Write-Host "    重启     Restart-ScheduledTask -TaskName $TaskName"
Write-Host "    停止     Stop-ScheduledTask -TaskName $TaskName"
Write-Host "    改配置   编辑 $envLocal 后重启（Restart-ScheduledTask）"
Write-Host "    卸载     powershell -ExecutionPolicy Bypass -File `"$root\deploy\windows\uninstall.ps1`""
Write-Host ""
