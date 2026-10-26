# 部署到腾讯云 Windows 服务器（宝塔面板）· 详细教程

> 面向的机器：**腾讯云轻量应用服务器 · Windows Server · 2 核 2G · 广州 · 公网 IP `111.230.129.109`**
> 目标：把这个智能烹饪调度 Agent 跑起来，别人点一个链接就能用。
>
> ⚠ 项目里原有的《部署指南》写的是 **Linux + systemd + Nginx** 那条路
> （`deploy/server-setup.sh`、`cooking-agent.service`、`nginx-cooking-agent.conf`），
> **在这台 Windows 机器上一条都用不了**。本教程是专门为 Windows 写的，
> 配套脚本在 `deploy/windows/` 下。

---

## 目录

| 步骤 | 做什么 | 在哪台机器上 |
|---|---|---|
| 0 | 先确认三件事 | 心里有数 |
| 1 | 打包 | **你的开发机** |
| 2 | 把包传上去并解压（**可用宝塔面板的「文件」上传+解压**） | 服务器 |
| 3 | 装 Python 3.14 | 服务器 |
| 4 | 一键部署（跑 `setup.ps1`） | 服务器 |
| 5 | **放行端口：腾讯云 + 宝塔 + Windows 三层**（最容易漏） | 腾讯云网页 / 宝塔面板 |
| 6 | 验收 | 浏览器 |
| 7 | 把链接发给别人 | — |
| 附 A | 接远程模型（土豆中转站） | 服务器 / 浏览器 |
| 附 B | 用 Nginx 反代去掉端口号（可选） | 服务器 |
| 附 C | 日常运维命令 | 服务器 |
| 附 D | 排错表 | 服务器 |
| 附 E | 卸载 | 服务器 |

**全程大约 20 分钟**，其中 10 分钟是等 pip 下载依赖。

> 📋 **想知道"这套东西到底能不能搬过去、搬过去还灵不灵"？**
> 见同目录 **《迁移验收报告》**（`docs/迁移验收报告.md`）—— 里面是实测数据，
> 还列了两个真会卡住部署的坑（`pip` 换源、中转站限流）。

---

## 0. 先确认三件事

打开腾讯云控制台，进「轻量应用服务器 → 你的实例」，抄下这几个值：

| 要确认的 | 在哪看 | 你这台的值 |
|---|---|---|
| 公网 IP | 实例列表 / 详情页 | `111.230.129.109` |
| 操作系统 | 详情页 | Windows Server（✅ 本教程适用） |
| 地域 | 详情页 | 广州（✅ 国内访问快） |

**再想清楚一件事：要不要开鉴权？**

这个项目的 Web 界面**没有账号登录**。谁打开链接，谁就能改模型配置、删菜谱。
所以脚本默认会**自动生成一个访问令牌**，链接形如：

```
http://111.230.129.109:8137/app?token=自动生成的32位串
```

- 想安全 → **保持默认**（自动生成令牌），把带令牌的链接发给你想给的人；
- 只在自己电脑上试 → 可以加 `-NoAuth` 关掉鉴权，但**公网千万别这么干**。

> 💡 顺带说清楚：**用 `8137` 这种非标端口，可以直接用 IP 访问，不需要域名备案。**
> 只有当你以后想绑域名、走 `80`/`443` 端口时，才需要走备案流程。

---

## 1. 打包（在**你的开发机**上做）

服务器上不需要 `tests/`、`docs/`、`.venv`（200MB+）这些东西。
项目里带了一个打包脚本，它会**强制先同步 `publish/`**，再打出一个干净的 zip。

> ⚠ 为什么必须"强制同步"：`publish/` 是**拷贝**不是软链接。
> 改了源码却忘了同步，线上跑的就还是旧版本 —— 而本地完全看不出来。
> 这是这个项目真实踩过的坑，所以脚本第一步就跑 `sync_publish`，不一致直接中止。

在项目根目录打开 PowerShell，执行：

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\pack.ps1
```

看到这样的输出就是成功了：

```
==> 同步 publish/ 与源码
==> 复核 publish/ 是否与源码一致
==> 复制 publish/ 与 deploy/
==> 压缩 → D:\Xuzj\ky\Project\ft\dist\cooking-agent-windows.zip
  [OK] 完成：...（0.37 MB）
  [OK] 包内含 app/api/main.py
  [OK] 包内含 app/web/index.html
  [OK] 包内含 data/recipes_official_100.csv
  [OK] 包内含 requirements.txt
  [OK] 包内含 requirements.lock.txt
  [OK] 包内含 deploy/windows/setup.ps1
  [OK] 条目名分隔符规范（正斜杠）
  [OK] 共 68 个文件
```

**产物：`dist\cooking-agent-windows.zip`（约 0.37 MB）** —— 就这么小，一会儿传上去很快。

> 如果脚本报 `包内缺少 xxx`，**不要继续往下走**。那说明打包出了问题，
> 传上去也是白折腾。把报错原文发我。

---

## 2. 把包传上去

先登录服务器。两种方式，随便挑：

- **方式一（推荐）**：本机按 `Win + R`，输入 `mstsc`，回车，地址填 `111.230.129.109`，
  用户名 `Administrator`，密码是你在腾讯云设置的实例密码。
- **方式二**：腾讯云控制台 → 实例 → 「登录」，用网页版远程桌面。

登录进去之后，把 zip 传上去。**两条路都行**：

### 路线 A：全程用宝塔面板（推荐 —— 你这台装了）

> 本节按 **宝塔 Windows 面板 8.5.2 / Windows Server 2022** 实测编写。
> 左侧菜单：首页 / 网站 / FTP / 数据库 / 监控 / **安全** / **文件** / 日志 / **终端** / 计划任务 / 软件商店 / 设置。
> 本教程只会用到其中的 **文件**、**终端**、**安全** 三个。

#### A-1　拿到宝塔面板的入口

- **腾讯云控制台** → 轻量应用服务器 → 点进你的实例 → 「**应用管理**」/「**宝塔面板**」，
  里面能看到面板地址（形如 `http://111.230.129.109:8888/xxxxx`）和初始账号密码；
- 或者直接登录服务器桌面，桌面上一般有「宝塔面板」的快捷方式，双击就能看到地址。

> 宝塔面板自己占 **8888** 端口，跟我们用的 **8137** 不冲突，不用改。

#### A-2　用「文件」上传并解压

1. 左侧菜单点「**文件**」；
2. 顶部地址栏（或盘符下拉）切到 **`C:`** 盘；
3. 点「**上传**」→ 选 `cooking-agent-windows.zip`，等它传完（约 0.4 MB，很快）；
4. 在列表里**右键**这个 zip → 「**解压**」；
5. **解压路径填 `C:/cooking-agent`**；
6. 解压完进 `C:\cooking-agent` 看一眼，**应该直接看到** `app\`、`data\`、`scripts\`、
   `deploy\`、`requirements.txt`、`requirements.lock.txt`。

> ⚠ 如果看到的是 `C:\cooking-agent\cooking-agent-windows\app\...`，说明**多套了一层**。
> 要么把它移上来，要么重解一次并正确填写解压路径。

#### A-3　跑部署脚本

宝塔 Windows 面板左侧**有「终端」**（8.5.2 版实测确认），点进去就能执行 PowerShell。
但**先确认它是不是管理员身份** —— `setup.ps1` 要改防火墙规则、注册开机自启的计划任务，
权限不够会中途失败。在终端里敲：

```powershell
whoami
```

- 输出 `nt authority\system` 或 `...\administrator` → **权限够**，可以直接往下跑；
- 输出是普通用户名 → 别在这儿跑，改走**远程桌面 + 管理员 PowerShell**（见第 4 步）。

**保险做法：直接用远程桌面的「以管理员身份运行 PowerShell」**，见第 4 步。

> 面板顶部如果提示「面板SSL / HTTP 访问风险」，那是**面板自身**的安全提醒，
> 跟本项目的部署无关，部署完再处理也不迟。

### 路线 B：不用宝塔，走远程桌面复制粘贴

1. 在远程桌面窗口里，打开「文件资源管理器」，进 `C:\`；
2. 新建文件夹 `cooking-agent`；
3. 从**本机**把 `cooking-agent-windows.zip` 拖进远程桌面窗口（或直接 `Ctrl+C` / `Ctrl+V`）；
4. 在服务器上右键这个 zip → 「全部解压缩」→ 目标填 `C:\cooking-agent`。

### 传完先自查

打开 `C:\cooking-agent`，**应该直接看到**这几个东西：

```
app\          data\        scripts\      deploy\
requirements.txt     requirements.lock.txt
```

> ⚠ 如果你看到的是 `C:\cooking-agent\cooking-agent\app\...`（多套了一层），
> **不用管** —— 下一步的脚本会自动往下找一层并告诉你。
> 但如果你看到的是 `C:\cooking-agent\app\api\main.py` 这种被"解平"的怪结构，
> 说明解压工具没处理好，换个工具重解（用 Windows 自带的"全部解压缩"最稳）。

---

## 3. 装 Python 3.14（64 位）

项目需要 **Python 3.10 以上**。推荐 **3.14 64 位** —— 因为 `requirements.lock.txt`
就是在 CPython 3.14 / Windows 上生成的，装这个版本可以**逐字复刻开发环境**。

> 装 3.12 / 3.13 也能跑，脚本会自动改用 `requirements.txt`（不套锁文件）。
> 只是版本组合没被验证过，出问题的概率略高一点。

### 下载

服务器上打开浏览器，去 <https://www.python.org/downloads/windows/>，
选 **Windows installer (64-bit)** 的最新 3.14。

> 国内下载 python.org 有时很慢。备选：到 <https://mirrors.huaweicloud.com/python/>
> 找对应的 `python-3.14.x-amd64.exe` 下载。

### 安装

双击安装包，**最关键的一步**：

> ✅ **务必勾选最下面那个 `Add python.exe to PATH`**
> （不勾的话，命令行里敲 `python` 会说"不是内部或外部命令"）

然后点 `Install Now`，等它装完。

### 验证

装完**新开一个** PowerShell 窗口（旧窗口读不到新的 PATH），执行：

```powershell
python --version
```

应该输出 `Python 3.14.x`。

> 如果提示找不到 `python`：
> - 关掉所有 PowerShell 窗口重新开一个试试；
> - 还不行就是装的时候没勾 PATH，重新跑一遍安装包，选 `Modify` → 勾上 PATH。

---

## 4. 一键部署

### 4.1 以管理员身份打开 PowerShell

开始菜单搜 `PowerShell` → 在「Windows PowerShell」上**右键** → **以管理员身份运行**。

> ⚠ 必须是管理员。脚本要改防火墙规则、注册开机自启的计划任务，权限不够会失败。

### 4.2 跑脚本

```powershell
cd C:\cooking-agent
powershell -ExecutionPolicy Bypass -File .\deploy\windows\setup.ps1
```

**默认就这么跑，不加任何开关。** 这台机器是 2 核 2G、装不了本地大模型，
脚本会自动写 `FT_WARM_LLM=off` —— 效果是：

- ✅ 排程质量和耗时**完全不变**（这是这个项目最核心的能力，且它**根本不依赖大模型**）；
- ✅ 不会每次开机白等 60 秒去探测一个不存在的本地模型；
- ✅ **保留了接远程大模型的能力**（右下角「AI 对话」可以接土豆中转站，见附 A）。

> ### ⚠ 别加 `-Offline`（除非你确定不要 AI 对话）
>
> `-Offline` 写的是 **`FT_LLM=off`** —— 那是**把大模型整个关掉的总闸**，
> 不是"关掉本地 Ollama"的意思。后果很坑：
>
> - 你去设置页把「接入方式」切成「远程」、填好地址和 Key，界面会显示**保存成功**；
> - 但 AI 对话**永远**回"已切换为离线模式" —— 因为总闸在 `.env.local` 里，
>   **设置页改不动它**（`FT_LLM` 只在 `enabled()` 里读，没有任何界面能清掉）。
>
> 什么时候才用 `-Offline`：**纯离线演示机 / 完全无外网**，压根不要 AI 对话的场景。
> 万一已经踩了：删掉 `.env.local` 里 `FT_LLM=off` 那一行，重跑一遍 `setup.ps1` 即可。


### 4.3 脚本会做这六件事

| 步骤 | 做什么 |
|---|---|
| 1/6 | 找到 Python → 建 `.venv` → 装依赖（含 ortools，约 100MB，**慢是正常的**） |
| 2/6 | 写 `.env.local`（访问令牌 / 离线开关）—— 用标记块包裹，**不动你手写的内容** |
| 3/6 | 生成 `run-server.cmd`（崩溃后 10 秒自动拉起，日志写 `logs\server.log`） |
| 4/6 | 放行 Windows 防火墙的 `8137` 端口 |
| 5/6 | 注册计划任务「CookingAgent」：**开机自启 / SYSTEM 身份 / 最高权限 / 无限时长** |
| 6/6 | 自检 `/health`、`/ready`、`POST /api/plan`，并打印对外链接 |

**六步全是幂等的** —— 跑第二遍不会重复堆配置，放心重跑。

> 💡 **为什么用"计划任务"而不是 NSSM / WinSW？**
> 后两个都要先从网上下载 exe，国内服务器经常拉不动。
> 计划任务是 Windows 自带的，零依赖；配上「开机自启 + SYSTEM + 无限时长」，
> 行为和一个 Windows 服务等价。

### 4.4 成功的输出长这样

```
  [OK] 项目目录：C:\cooking-agent
  [OK] Python 3.14.x  (C:\Python314\python.exe)
  [OK] 已创建 C:\cooking-agent\.venv
  [OK] 依赖安装完成
  [OK] 已自动生成访问令牌（下面会打印，请复制保存）
  [OK] .env.local 已更新（UTF-8 无 BOM）
  [OK] 已写入 C:\cooking-agent\run-server.cmd
  [OK] 已放行 Windows 防火墙 TCP 8137
  [OK] 计划任务「CookingAgent」已注册（开机自启 / SYSTEM / 最高权限）
  [OK] 已启动
  [OK] /health 200
  [OK] /ready 就绪（菜库 100 道）
  [OK] /api/plan 通过（xxxx ms，2 道菜，xx:xx 上桌）

============================================================
  部署完成
============================================================

  本机自测： http://127.0.0.1:8137/app
  对外链接： http://111.230.129.109:8137/app?token=xxxxxxxx
  访问令牌： xxxxxxxx
```

> 📌 **把「访问令牌」和「对外链接」复制下来存好。** 后面发链接要用。
> 忘了也没事，令牌就写在 `C:\cooking-agent\.env.local` 里，随时能看。

**如果最后自检没过**，先别急，直接跳到「附录 D：排错表」。

---

## 5. ⚠ 腾讯云控制台也要放行端口（最容易漏的一步）

**只开 Windows 防火墙是不够的。** 腾讯云在机器外面还有一层防火墙，
不开这一层，别人照样连不上，而你在服务器上 `curl 127.0.0.1` 却是通的 ——
**这是最容易卡住、又最难想明白的一步。**

1. 打开腾讯云控制台 → **轻量应用服务器** → 点进你的实例；
2. 上方切到「**防火墙**」页签；
3. 点「**添加规则**」；
4. 按这样填：

   | 字段 | 填什么 |
   |---|---|
   | 应用类型 | 自定义 |
   | 协议 | **TCP** |
   | 端口 | **8137** |
   | 来源 | `0.0.0.0/0`（表示允许所有 IP） |
   | 备注 | 烹饪调度 Agent |

5. 确定，保存。

> 如果你在第 4 步改过端口（`-Port 9000` 之类），这里要填你实际用的那个。

### 装了宝塔的机器上，端口是**三层**分别管的

任何一层没放行，外面都连不上 —— 而服务器上 `curl 127.0.0.1:8137` 却是通的，
这正是最难想明白的地方：

| 层 | 在哪 | 8137 由谁加 |
|---|---|---|
| ① 腾讯云控制台防火墙 | 腾讯云网页（就是上面这 5 步） | **你手动加**（必须） |
| ② 宝塔面板自己的防火墙 | 宝塔面板左侧「安全」页（若该版本有这一页） | **去看一眼**：如果那里维护的是一份**独立**规则列表，把 8137 也加上 |
| ③ Windows 防火墙 | 服务器本机 | 第 4 步的 `setup.ps1` **已自动加好**，不用管 |

> 有的宝塔 Windows 版本「安全」页只是 Windows 防火墙的前端 —— 那就已经覆盖了；
> 但如果它维护独立规则，就必须单独加，否则照样连不上。

---

## 6. 验收

在**你自己的电脑**上（不是服务器上），浏览器打开：

```
http://111.230.129.109:8137/app?token=你的令牌
```

按这个清单对一遍：

| 检查项 | 期望看到 |
|---|---|
| 页面能不能打开 | 出现排程界面（不是"无法访问此网站"） |
| 左侧菜单 | 首页 / 排程 / 菜谱 / 设置 / 历史，共 5 个 |
| 右下角 | 有个悬浮球「AI 对话」 |
| 菜谱页 | 能看到菜，数量约 100 道 |
| 排程页选 2 道菜 → 点排程 | 出甘特图 + 上桌时间 |
| 地址栏去掉 `?token=...` 再刷新 | **仍然能用**（令牌已被浏览器记住） |

最后一条能过，说明鉴权也在正常工作。

> **顺手测一下"从外网能不能进"**：用手机流量（**别连家里 WiFi**）打开同一个链接。
> 如果手机能开、电脑也能开，那就真的通了。

---

## 7. 把链接发给别人

直接把这条发出去就行：

```
http://111.230.129.109:8137/app?token=你的令牌
```

对方**第一次**打开要带 `?token=...`，之后这个浏览器就记住了，书签存裸链接也能用。

### 几个实用的发链接姿势

- **只给少数人**：直接把带令牌的链接发过去。
- **要给一批人、又不想逐个发**：可以把链接做成二维码，或者贴到内部群里。
- **想让链接"更好看"**：看「附录 B：用 Nginx 反代去掉端口号」，可以变成
  `http://你的域名/app?token=...`。

### ⚠ 三条安全提醒

1. **链接里的令牌就是密码。** 别发到公开群、别贴到公开网页。谁拿到谁就能改你的配置。
2. **想收紧**：重跑一次 `setup.ps1 -Token 新令牌`，旧链接立刻失效；
   或者加 `-ReadOnly` 关掉菜谱的增删改（见「附录 C：日常运维命令」）。
3. **微信/QQ 里发 `http://IP:端口` 的链接**，对方点开可能会被提示"非官方网页"或被拦截。
   这是平台行为，不是你的部署有问题 —— 让对方复制到浏览器打开即可。

---

## 附 A：接远程模型（可选）

**前提**：按第 4 步部署时**没有**加 `-Offline`（加了的话 `FT_LLM=off` 会把大模型总闸关掉，
这里怎么填都不会生效 —— 先回去删掉 `.env.local` 里那一行并重跑 `setup.ps1`）。

> ⚠ **部署包里刻意不含 API Key。** 所以换一台服务器部署完，AI 对话**一定**是"离线"状态，
> **必须在这一步重新填一次 Key**。这不是打包漏了 ——
> 密钥只存在两处：开发机的 `data\llm_settings.json`（**被 `sync_publish` 明确排除，不进包**）
> 和 `.env.local` 里的环境变量。包里搜不到 `sk-` 开头的字符串是**正常的、也是应该的**。
> 原因：包要传服务器、可能还要发人，明文密钥进去等于到处扩散；而且项目有硬规矩
> 「密钥只存不回传」—— 连设置页都不回填密钥框，更不会塞进包里。
> 想让密钥跟着走，见下面「三种补法」。

想接真模型（比如你那个土豆中转站）：

**不用改任何配置文件**，直接在网页上操作：

1. 打开 `http://111.230.129.109:8137/app?token=你的令牌`
2. 左侧「**设置**」→ 上面切到「**模型接入**」子页签
3. 「接入方式」选「**远程**」
4. 填：

   | 字段 | 填什么 |
   |---|---|
   | 接口地址 | `https://potatoya.cn/pro100x/v1` |
   | 模型名 | `gpt-5.6-sol` |
   | API Key | 你的 `sk-pro100x-...` |

5. 点「**保存**」，再点「**测试连接**」

看到"就绪"就说明通了。

> ⚠ **中转站有限流，别连着猛测。** 实测这个中转站在**短时间内连发请求**时会返回 `429`。
> 项目对 `429` 的处理是**熔断 30 分钟**（`FT_LLM_CB_QUOTA`，默认 1800 秒）——
> 期间 AI 对话会一直提示"已切换为离线模式"。正常聊天（几秒一句）不会触发，
> 但如果刚配好就一口气点十几次「测试连接」/ 发十几条消息，可能会撞上。
> **恢复办法**：等 30 分钟，或在「设置」里重新保存一次模型配置（会复位熔断器）。
>
> 另外单次调用超时是 **20 秒**，偶发卡顿时会等满 20 秒才回离线文案。
> 想放宽（代价是失败时等更久）可以在 `.env.local` 里加 `FT_LLM_TIMEOUT=45`，然后重跑 `setup.ps1`。

> 💡 **中转站不一定用你填的模型名。** 实测填 `gpt-5.6-sol`，中转站实际路由到
> `step-3.7-flash` 并在响应里如实回报 —— 这是正常的，只要**有回复**就说明链路通了。

> 🔒 **关于密钥安全**：密钥**只存在服务器的 `data\llm_settings.json` 里**。
> 页面**永远不会**把密钥回填到输入框，接口也**不会**返回密钥本身
> （只返回一个"是否已设置"的布尔值）—— 因为这个页面是公开链接，
> 谁打开都能看。所以：**密钥框留空 = 保持原样，不是清空**。

> ⚠ 注意：服务器上**没有**本地 Ollama。如果你在设置里切回「本地」，AI 对话会连不上，
> 但**排程功能不受影响**（它不依赖大模型）。

### 想让密钥跟着服务器走：三种补法

| 补法 | 怎么做 | 适合 |
|---|---|---|
| **① 网页上重填**（最简） | 就是上面那 5 步，填一次保存即可 | 只部署一两台 |
| **② 拷设置文件** | 把开发机 `data\llm_settings.json` 复制到服务器 `C:\cooking-agent\data\`，再重启服务 | 想连"选了哪个模型"一起带过去 |
| **③ 写进 `.env.local`**（会"钉死"） | 在 `C:\cooking-agent\.env.local` 里加三行，重启服务 | 想防止别人在页面上误改 |

第 ③ 种的写法：

```
FT_LLM_PROVIDER=openai
FT_LLM_BASE_URL=https://potatoya.cn/pro100x/v1
FT_LLM_API_KEY=sk-pro100x-你的密钥
```

> ⚠ 环境变量**优先级最高**：一旦这么写，设置页的「接入方式」下拉会被**禁用**
> （页面会提示"环境变量已指定，界面改动不会生效"）。想恢复可改，就把这三行删掉重跑 `setup.ps1`。
> ⚠ 编辑 `.env.local` 必须存成 **UTF-8 无 BOM**，否则第一行的键名会带上 BOM 前缀而**静默读不到**。

---

## 附 B：用 Nginx 反代去掉端口号（可选）

想让链接从 `http://111.230.129.109:8137/app` 变成 `http://你的域名/app`，可以用 Nginx 反代。

**前提**：
- 需要**域名**，并且走 `80`/`443` 端口的话**必须已完成备案**（大陆服务器强制）；
- 只是想去掉端口号、用 IP 也行，但收益不大，**不建议折腾**。

**如果确实要做**，大致是这样（宝塔 Windows 面板的「网站」里新建站点，加反向代理）：

```nginx
location / {
    proxy_pass http://127.0.0.1:8137;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 300s;      # 排程可能要跑十几秒，别用默认的 60s
}
```

> ⚠ **加了 Nginx 之后，必须重跑一次部署脚本并带上 `-TrustProxy`：**
>
> ```powershell
> cd C:\cooking-agent
> powershell -ExecutionPolicy Bypass -File .\deploy\windows\setup.ps1 -TrustProxy
> ```
>
> 不加的话，限流会把**所有访客都算成同一个 IP**（都是 Nginx 的 `127.0.0.1`），
> 人一多就互相挤掉。
>
> ⚠ **别顺手加 `-Offline`** —— 那是把大模型整个关掉的总闸（见第 4.2 步），
> 加了之后接远程模型就不生效了。

---

## 附 C：日常运维命令

**全部在服务器上、以管理员身份**跑。

```powershell
# 看实时日志（最常用）
Get-Content -Tail 60 -Encoding UTF8 "C:\cooking-agent\logs\server.log"

# 一直盯着日志看
Get-Content -Wait -Tail 20 -Encoding UTF8 "C:\cooking-agent\logs\server.log"

# 重启服务（改完配置后要重启才生效）
Restart-ScheduledTask -TaskName CookingAgent

# 停止 / 启动
Stop-ScheduledTask -TaskName CookingAgent
Start-ScheduledTask -TaskName CookingAgent

# 看任务状态和上次运行结果
Get-ScheduledTask -TaskName CookingAgent | Get-ScheduledTaskInfo

# 看端口有没有在监听
Get-NetTCPConnection -LocalPort 8137 -State Listen

# 从服务器本机测一下（用来区分"服务挂了"还是"网络没通"）
Invoke-WebRequest http://127.0.0.1:8137/health -UseBasicParsing
```

### 改配置

配置都在 `C:\cooking-agent\.env.local`。用记事本打开，改完**重启服务**：

```powershell
notepad C:\cooking-agent\.env.local
Restart-ScheduledTask -TaskName CookingAgent
```

常用开关（重跑 `setup.ps1` 时会自动维护这一段，标记块之外你手写的内容不会被覆盖）：

| 变量 | 作用 |
|---|---|
| `FT_AUTH_TOKEN=xxx` | 访问令牌。删掉这行 = 关闭鉴权 |
| `FT_WARM_LLM=off` | 关掉启动探测（服务器上没有本地模型时必须关，否则每次开机白等 60s） |
| `FT_LLM=off` | ⚠ **大模型总闸**：整体关掉 AI 能力（排程不受影响）。**接远程模型也会失效，且设置页改不动它** —— 除非你确定不要 AI 对话，否则别加 |
| `FT_RATE_LIMIT=600/60` | 每个 IP 每 60 秒最多 600 次 |
| `FT_TRUST_PROXY=1` | 前面有 Nginx 时必须开 |
| `FT_ALLOW_RECIPE_WRITE=off` | 关闭菜谱增删改（发链接给很多人时建议加） |

> ⚠ 编辑 `.env.local` 时**必须存成 UTF-8 无 BOM**。用 Windows 记事本"另存为"时，
> 编码选 **UTF-8**（**不要**选"UTF-8 带 BOM"）。
> 带 BOM 会让第一行的键名变成 `\uFEFFFT_AUTH_TOKEN`，**静默读不到**，鉴权就失效了。

### 加限流 + 只读（发链接给一批人时推荐）

```powershell
cd C:\cooking-agent
powershell -ExecutionPolicy Bypass -File .\deploy\windows\setup.ps1 -RateLimit 600/60 -ReadOnly
```

---

## 附 D：排错表

**排查的第一步永远是看日志：**

```powershell
Get-Content -Tail 60 -Encoding UTF8 "C:\cooking-agent\logs\server.log"
```

| 现象 | 最可能的原因 | 怎么办 |
|---|---|---|
| 浏览器一直转圈 / "连接超时" | **腾讯云控制台防火墙没放行** | 回到「第 5 步」 |
| 同上，但服务器上 `curl 127.0.0.1:8137` 是通的 | 外面那两层没放行（云防火墙 / 宝塔「安全」页） | 三层都查一遍（见第 5 步的表） |
| "无法访问此网站 / 拒绝连接" | 服务没起来 | `Get-ScheduledTask -TaskName CookingAgent \| Get-ScheduledTaskInfo`，看上次运行结果 |
| 页面能开，但排程点了没反应 | 看日志里的报错 | 多半是依赖没装全，重跑 `setup.ps1` |
| 提示 401 / 一直让输令牌 | 链接里没带 `?token=` | 用完整链接（含 `?token=...`）重新打开一次 |
| 打开很慢，每次重启后第一下特别慢 | 启动探测在等本地 Ollama（60s） | 确认 `.env.local` 里有 `FT_WARM_LLM=off` |
| **设置页切了「远程」、显示保存成功，AI 对话还是回"离线模式"** | `.env.local` 里有 `FT_LLM=off`（多半是部署时加了 `-Offline`）—— **设置页改不动这个总闸**。💡 **新版界面会自己告诉你**：设置页顶部会红字点出「总闸 FT_LLM=off」，点「测试连接」的报错也会直接说是总闸，不会再让你去查地址和密钥 | 删掉 `.env.local` 里 `FT_LLM=off` 那一行，重跑 `setup.ps1`（**不加** `-Offline`） |
| `pip install` 卡住 / 超时 | 国内网络 | 手动换镜像重装（见下） |
| `pip` 报 `Could not find a version that satisfies ... (from versions: none)` | **当前 pip 源被挡了**（不是包名错、也不是没网） | 换源重装（见下）。这条最坑：浏览器能打开镜像站，pip 却一个版本都找不到 |
| `python` 不是内部或外部命令 | 装 Python 时没勾 PATH | 重跑安装包 → `Modify` → 勾 `Add python.exe to PATH`；或直接 `-Python C:\Python314\python.exe` |
| `python -m venv` 报错 | Python 装得不完整 | 重装 Python（选 `Repair`） |
| 端口 8137 被占用 | 别的程序占了 | `Get-NetTCPConnection -LocalPort 8137 -State Listen` 看是谁；或换个端口重装：`setup.ps1 -Port 9000` |
| 中文显示成乱码 | 编码问题 | 一般不会出现；若出现请把日志发我 |
| 计划任务显示"上次运行结果: 1" | 启动器立刻退出了 | 手动跑 `C:\cooking-agent\run-server.cmd` 看真实报错 |

**手动换国内镜像装依赖：**

```powershell
cd C:\cooking-agent
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://mirrors.cloud.tencent.com/pypi/simple
```

> ⚠ **如果报 `Could not find a version ... (from versions: none)`** —— 这不是"包不存在"，
> 而是**当前这个源取不到索引**（pip 把"源挂了"和"包没有"报成同一句话）。
> 换个源就行，例如直接走官方源：
>
> ```powershell
> cd C:\cooking-agent
> .\.venv\Scripts\python.exe -m pip install -r requirements.txt `
>   -i https://pypi.org/simple/ --trusted-host pypi.org
> ```
>
> 实测踩过：`pip.ini` 里配着清华源时，`curl` 打开镜像页返回 200，pip 却一个版本都找不到；
> 换成官方源立刻装上。**不要花时间怀疑包名或 Python 版本。**

**手动前台跑一次（能看到全部报错，排查神器）：**

```powershell
cd C:\cooking-agent
Stop-ScheduledTask -TaskName CookingAgent      # 先把后台那个停掉，免得抢端口
.\.venv\Scripts\python.exe -m uvicorn app.api.main:app --host 0.0.0.0 --port 8137 --workers 1
```

按 `Ctrl+C` 退出。排查完记得 `Start-ScheduledTask -TaskName CookingAgent` 把服务拉回来。

> ⚠ **`--workers` 必须是 1。** 设备配置、排程历史、熔断器、在线闸门全是**进程内状态**，
> 开多 worker 会出现"改了设备配置，下一次排程又变回去了"这种鬼故事。

---

## 附 E：卸载

**保守卸载**（只摘掉服务，**不动**代码、日志和你的菜谱改动）：

```powershell
cd C:\cooking-agent
powershell -ExecutionPolicy Bypass -File .\deploy\windows\uninstall.ps1
```

它会：停进程 → 注销计划任务 → 删 Windows 防火墙规则 → 打印还剩什么没删。

> ⚠ **腾讯云控制台那条防火墙规则它删不掉**（不在本机），要自己去控制台删。

想重装的话，保守卸载完直接重跑 `setup.ps1` 就行 —— 数据都还在。

**彻底清空**（连代码、`.venv`、日志、菜谱改动一起删，**不可恢复**）：

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\windows\uninstall.ps1 -Purge
```

会要求你手动输入 `DELETE` 确认。

---

## 一页速查（贴墙上）

```powershell
# ① 开发机打包
powershell -ExecutionPolicy Bypass -File deploy\windows\pack.ps1
#    → dist\cooking-agent-windows.zip

# ② 传到服务器，解压到 C:\cooking-agent

# ③ 服务器上（管理员 PowerShell）一键部署
cd C:\cooking-agent
powershell -ExecutionPolicy Bypass -File .\deploy\windows\setup.ps1

# ④ 腾讯云控制台 → 防火墙 → 放行 TCP 8137   ← 别忘

# ⑤ 打开 http://公网IP:8137/app?token=令牌

# 日常：看日志 / 重启
Get-Content -Tail 60 -Encoding UTF8 "C:\cooking-agent\logs\server.log"
Restart-ScheduledTask -TaskName CookingAgent
```

---

## 附：本教程涉及的文件

| 文件 | 作用 |
|---|---|
| `deploy/windows/pack.ps1` | **开发机**上跑，打包成 `dist\cooking-agent-windows.zip` |
| `deploy/windows/setup.ps1` | **服务器**上跑，一键部署（六步全幂等） |
| `deploy/windows/uninstall.ps1` | **服务器**上跑，卸载（默认保守，`-Purge` 才删数据） |
| `tests/test_deploy_windows.py` | 这套脚本的静态护栏（42 条） |
| `scripts/_mutate_deploy_guards.py` | 变异测试：给脚本注入缺陷，确认护栏真的会红（29 条） |

改完脚本后，建议跑一遍：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_deploy_windows.py -q
.\.venv\Scripts\python.exe -m scripts._mutate_deploy_guards
```
