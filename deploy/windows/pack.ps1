<#
  在本机（开发机）执行：把要上传到 Windows 服务器的东西打成一个 zip。

  用法（在项目根目录，或任意位置）：
      powershell -ExecutionPolicy Bypass -File deploy\windows\pack.ps1

  产物：dist\cooking-agent-windows.zip

  为什么要有这个脚本
  ------------------
  1) 「上传了旧代码」是本项目真实踩过的坑：`publish/` 是**拷贝**不是链接，
     改了源码不同步过去，线上就还是旧版本，而本地完全看不出来。所以第一步
     **强制**跑 sync_publish，不一致就直接中止。
  2) 手工压缩很容易把 `.venv`（200MB+）、`.cache`、`ppt_slides`、`*.docx`
     一起打进去 —— 上传慢、解压慢，而且服务器上根本用不到。

  打包内容 = `publish/`（运行期真正需要的东西）+ `deploy/`（部署脚本）。
  不含 tests/、docs/、start.bat —— 服务器上跑服务用不到它们。
#>
[CmdletBinding()]
param(
  [string]$Out = ""
)

$ErrorActionPreference = "Stop"

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path      # deploy\windows
$RootDir = (Resolve-Path (Join-Path $Here "..\..")).Path      # 项目根目录

function Say  ($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Ok   ($m) { Write-Host "  [OK] $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "  [!]  $m" -ForegroundColor Yellow }
function Die  ($m) { Write-Host "  [X]  $m" -ForegroundColor Red; exit 1 }

Say "项目根目录：$RootDir"
if (-not (Test-Path (Join-Path $RootDir "requirements.txt"))) {
  Die "这里不像项目根目录（找不到 requirements.txt）"
}

# ---------------------------------------------------- 挑一个能用的 python
$py = ""
foreach ($c in @(".venv\Scripts\python.exe", ".venv/bin/python")) {
  $p = Join-Path $RootDir $c
  if (Test-Path $p) { $py = $p; break }
}
if (-not $py) {
  $g = Get-Command python -ErrorAction SilentlyContinue
  if ($g) { $py = $g.Source }
}
if (-not $py) { Die "找不到 python，请先在项目根目录建好 .venv（跑一次 start.bat 即可）" }

# ------------------------------------------- 1. 同步 publish/（防止发旧代码）
Say "同步 publish/ 与源码"
& $py -m scripts.sync_publish
if ($LASTEXITCODE -ne 0) { Die "sync_publish 失败" }

Say "复核 publish/ 是否与源码一致"
& $py -m scripts.sync_publish --check
if ($LASTEXITCODE -ne 0) { Die "publish/ 与源码仍不一致，已中止打包" }

# ---------------------------------------------------------------- 2. 打包
$Stage = Join-Path $RootDir ".cache\pack-windows"
if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage }
New-Item -ItemType Directory -Path $Stage -Force | Out-Null

Say "复制 publish/ 与 deploy/"
Copy-Item -Path (Join-Path $RootDir "publish\*") -Destination $Stage -Recurse -Force
Copy-Item -Path (Join-Path $RootDir "deploy")     -Destination $Stage -Recurse -Force

# 打包目录里不需要 Windows 专用脚本之外的东西？留着无妨（只有 30KB）。
# 但 publish/ 里可能残留 __pycache__，清掉让包更干净。
Get-ChildItem -Path $Stage -Recurse -Directory -Filter "__pycache__" |
  Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

if (-not $Out) { $Out = Join-Path $RootDir "dist\cooking-agent-windows.zip" }
$OutDir = Split-Path -Parent $Out
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
if (Test-Path $Out) { Remove-Item -Force $Out }

Say "压缩 → $Out"

# ⚠ 这里**刻意不用** `Compress-Archive`，也**不用** `ZipFile::CreateFromDirectory`。
#   两个都会把 zip 条目名写成**反斜杠**（`app\api\main.py`），而 ZIP 规范要求正斜杠。
#   实测（直接看 zip 里的原始字节）：
#     · Windows PowerShell 5.1 的 Compress-Archive          → 反斜杠
#     · .NET Framework 的 ZipFile::CreateFromDirectory      → 反斜杠
#     · ZipFile::CreateFromDirectory 直到 .NET Core 3.0 才修好，而 5.1 跑在 .NET Framework 上
#   反斜杠条目名用 Windows 资源管理器解开多半没事，但换 7-Zip / 宝塔 / Linux unzip
#   就可能解成一堆叫 "app\api\main.py" 的平铺怪文件 —— 目录结构是错的，而且要到
#   服务器上才发现。所以这里**手工逐条写**，条目名自己指定成正斜杠。
$zipOk = $false
try {
  # 两个程序集都要加载：ZipFile/ZipFileExtensions 在 .FileSystem.dll，
  # 而 ZipArchive / ZipArchiveMode / CompressionLevel 在 .dll —— 少一个就报
  # 「找不到类型 [IO.Compression.ZipArchiveMode]」。
  Add-Type -AssemblyName System.IO.Compression -ErrorAction Stop
  Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop
  $zip = [IO.Compression.ZipFile]::Open($Out, [IO.Compression.ZipArchiveMode]::Create)
  try {
    $prefixLen = $Stage.Length + 1        # 去掉 "$Stage\" 前缀，得到相对路径
    foreach ($f in (Get-ChildItem -LiteralPath $Stage -Recurse -File -Force)) {
      # 只在这一行做分隔符转换 —— 这是整个打包里最关键的一步
      $rel = $f.FullName.Substring($prefixLen).Replace('\', '/')
      $null = [IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
        $zip, $f.FullName, $rel, [IO.Compression.CompressionLevel]::Optimal)
    }
  } finally { $zip.Dispose() }
  $zipOk = $true
} catch {
  Warn "手工打包失败（$($_.Exception.Message)），退回 Compress-Archive"
}
if (-not $zipOk) {
  # 兜底路径：能出包，但条目名会是反斜杠。下面的复核会把它标出来。
  if (Test-Path $Out) { Remove-Item -Force $Out }
  Warn "注意：兜底路径产出的 zip 条目名是反斜杠，解压后请确认目录结构正常"
  Compress-Archive -Path (Join-Path $Stage "*") -DestinationPath $Out -CompressionLevel Optimal
}

$mb = [math]::Round((Get-Item $Out).Length / 1MB, 2)
Ok "完成：$Out（$mb MB）"

# ---------------------------------------------------------------- 3. 复核
# 两个都容易出错、而且错了要到服务器上才发现的地方：
#   (a) 目录结构 —— Compress-Archive 写成 -Path "$Stage" 会多套一层目录
#   (b) 分隔符   —— 反斜杠条目名在部分解压工具下会把结构解平
# 所以这里把条目名**归一化**后再比对，既不会因为分隔符差异假报警，
# 又能真的发现"文件压根没打进去"。
$zip = [IO.Compression.ZipFile]::OpenRead($Out)
try {
  $rawNames = @($zip.Entries | ForEach-Object { $_.FullName })     # 原始名（用来查分隔符）
} finally { $zip.Dispose() }
$names = @($rawNames | ForEach-Object { $_ -replace '\\', '/' })   # 归一化（用来比对路径）

$missing = @()
foreach ($need in @("app/api/main.py", "app/web/index.html", "data/recipes_official_100.csv",
                    # 出厂默认配置（随包走的"开箱即用"档）。少了它，接收方解压后
                    # 只会拿到本地 Ollama 档，必须自己配一遍才能用上模型 ——
                    # 而且**不会有任何报错**，看起来就是"AI 没反应"。
                    "app/agent/llm_defaults.json",
                    "requirements.txt", "requirements.lock.txt", "deploy/windows/setup.ps1")) {
  if ($names -contains $need) { Ok "包内含 $need" }
  else { $missing += $need }
}
if ($missing.Count -gt 0) {
  Die ("包内**缺少**：" + ($missing -join "、") + " —— 解压到服务器后会跑不起来")
}

# 反向确认：**凭据文件不许进包**。
#   开箱配置写在 app/agent/llm_defaults.json（随包走，是设计）；
#   而 `.env.local` 是"本机凭据 / 单独轮换"的那一份，进包等于把开发机的
#   环境一起发出去，之后谁改了都说不清是哪一份在生效。
$forbidden = @($names | Where-Object { $_ -match '(^|/)\.env($|\.)' })
if ($forbidden.Count -gt 0) {
  Die ("包内含凭据/环境文件：" + ($forbidden -join "、") + " —— 必须从打包范围里排除")
}
Ok "包内无 .env / .env.local"

# 反向确认：条目名不能含反斜杠。
#   主路径下出现反斜杠 = 打包方式没生效（PS 行为变了），必须查，不能放行；
#   兜底路径下出现反斜杠 = 已知的固有问题，只能提醒。
$badSep = @($rawNames | Where-Object { $_ -match '\\' })
if ($badSep.Count -gt 0) {
  if ($zipOk) {
    Die "手工打包路径仍产出 $($badSep.Count) 个反斜杠条目名（如 $($badSep[0])）—— 打包逻辑需要复核"
  }
  Warn "有 $($badSep.Count) 个条目名含反斜杠，个别解压工具可能把目录解平"
} else {
  Ok "条目名分隔符规范（正斜杠）"
}
Ok "共 $($names.Count) 个文件"
Write-Host ""
Write-Host "下一步：把上面这个 zip 传到服务器（宝塔面板 → 文件 → 上传），解压到 C:\cooking-agent" -ForegroundColor Yellow
