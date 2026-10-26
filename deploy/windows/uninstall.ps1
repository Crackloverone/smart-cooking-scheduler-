<#
.SYNOPSIS
  智能烹饪调度 Agent —— Windows 服务器卸载（停服务 + 注销开机自启 + 删防火墙规则）

.DESCRIPTION
  **在服务器上、以管理员身份**运行。与 setup.ps1 对称，做四件事：
    1) 停掉正在跑的进程
    2) 注销计划任务「CookingAgent」
    3) 删掉防火墙规则
    4) 打印「还剩什么没删」（默认**不动**代码目录、虚拟环境、日志、数据）

  默认是**保守卸载**：只把「服务」摘掉，不碰你的数据。这样想重装时直接重跑
  setup.ps1 即可，菜谱 / 设备配置 / 排程历史都还在。

  想连代码和数据一起清掉，加 -Purge（会二次确认，删完不可恢复）。

.PARAMETER Root
  项目所在目录，默认 C:\cooking-agent。只有 -Purge 时才会用到它。

.PARAMETER Port
  监听端口，默认 8137。用来定位防火墙规则名。

.PARAMETER Purge
  连同项目目录（含 .venv、data、logs）一起删除。**不可恢复**，会要求再次确认。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File C:\cooking-agent\deploy\windows\uninstall.ps1

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File C:\cooking-agent\deploy\windows\uninstall.ps1 -Purge
#>
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
  [string]$Root = "C:\cooking-agent",
  [int]$Port = 8137,
  [switch]$Purge
)

$ErrorActionPreference = "Continue"     # 卸载要「尽力而为」，某一步失败不该中断后续步骤
$TaskName = "CookingAgent"
$RuleName = "Cooking Agent TCP $Port"

function Say  ($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Ok   ($m) { Write-Host "  [OK] $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "  [!]  $m" -ForegroundColor Yellow }
function Miss ($m) { Write-Host "  [-]  $m" -ForegroundColor DarkGray }

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  智能烹饪调度 Agent  ·  Windows 服务器卸载" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# =============================================================== 1. 停进程
Say "1/4 停止正在运行的进程"
$killed = 0
# 先停计划任务里那个 cmd.exe（它会拉起 python），再单独找 uvicorn 进程。
# 只杀 python 不管 cmd 的话，cmd 的 :loop 会在 10 秒后把服务又拉起来。
try {
  $procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
           Where-Object { $_.CommandLine -and $_.CommandLine -match "uvicorn" -and $_.CommandLine -match "app\.api\.main" }
  foreach ($p in $procs) {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    $killed++
  }
} catch { }
if ($killed -gt 0) { Ok "已结束 $killed 个 uvicorn 进程" } else { Miss "没有找到在跑的 uvicorn 进程" }

# 计划任务若还挂着，先停它，否则它的 :loop 会立刻把服务拉回来
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task -and $task.State -eq "Running") {
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 2
  Ok "已停止计划任务"
}

# =============================================================== 2. 注销计划任务
Say "2/4 注销计划任务"
if ($task) {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Warn "注销失败，请手动删：任务计划程序 → 任务计划库 → $TaskName"
  } else {
    Ok "计划任务「$TaskName」已注销（开机不会再自启）"
  }
} else {
  Miss "计划任务「$TaskName」不存在，跳过"
}

# =============================================================== 3. 删防火墙规则
Say "3/4 删除防火墙规则"
$rule = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
if ($rule) {
  try {
    Remove-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
  } catch { }
  if (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue) {
    Warn "Get-NetFirewallRule 删不掉，改用 netsh"
    netsh advfirewall firewall delete rule name="$RuleName" | Out-Null
  }
  if (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue) {
    Warn "规则「$RuleName」仍存在，请到「高级安全 Windows Defender 防火墙」里手动删"
  } else {
    Ok "防火墙规则「$RuleName」已删除"
  }
} else {
  Miss "防火墙规则「$RuleName」不存在，跳过"
}

# =============================================================== 4. 收尾
Say "4/4 收尾"
$portOwner = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($portOwner) {
  Warn "端口 $Port 仍被占用（PID $($portOwner.OwningProcess -join ', ')），可能是残留进程"
} else {
  Ok "端口 $Port 已释放"
}

Write-Host ""
Write-Host "  ⚠ 注意：腾讯云控制台的防火墙规则**不在本机**，本脚本删不掉。" -ForegroundColor Yellow
Write-Host "     如果不再使用，请到：轻量应用服务器 → 服务器详情 → 防火墙 → 删掉 TCP $Port 那条。" -ForegroundColor Yellow
Write-Host ""

if ($Purge) {
  $root = $Root
  if (-not (Test-Path $root)) { Miss "$root 不存在，无需清理" }
  else {
    Write-Host ""
    Write-Host "  ⚠ -Purge 会**永久删除**整个 $root" -ForegroundColor Red
    Write-Host "     包含：源码、.venv（几百 MB）、logs、data（菜谱改动 / 设备配置 / 排程历史）" -ForegroundColor Red
    Write-Host "     删掉之后无法恢复。若只想重装，现在按 Ctrl+C 退出，直接重跑 setup.ps1 即可。" -ForegroundColor Red
    Write-Host ""
    $ans = Read-Host "  确认删除请输入 DELETE（其它任何输入都视为取消）"
    if ($ans -ceq "DELETE") {
      # 只删本脚本自己知道的这一个目录，绝不用通配符 / 上级目录
      Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
      if (Test-Path $root) { Warn "$root 未能完全删除（可能有文件被占用），请手动检查" }
      else { Ok "$root 已删除" }
    } else {
      Miss "已取消删除，代码与数据保持原样"
    }
  }
} else {
  Ok "已保留代码与数据（默认不删）"
  Write-Host "     代码目录  $Root" -ForegroundColor DarkGray
  Write-Host "     日志      $Root\logs\server.log" -ForegroundColor DarkGray
  Write-Host "     数据      $Root\data\" -ForegroundColor DarkGray
  Write-Host "     想连数据一起清掉： 加 -Purge 重跑本脚本" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "  卸载完成。" -ForegroundColor Green
Write-Host ""
