# 远程访问（中文指南）

> 本文是 [`docs/remote-access.md`](remote-access.md) 的中文姊妹篇，面向中文用户，
> 重点覆盖 Windows 原生环境。命令与安全约定以官方脚本（`start.sh` / `start.ps1` /
> `scripts/windows/setup_webui_autostart.ps1`）为准。

Hermes WebUI 默认只绑定 `127.0.0.1`（仅本机回环），这是刻意的安全默认值。
要从手机或另一台电脑访问，需要显式选择一条安全通道。本文按推荐程度排序：

| 方式 | 安全性 | 复杂度 | 适用场景 |
|---|---|---|---|
| Tailscale Serve（HTTPS + MagicDNS） | ★★★★★ | 低 | 推荐首选，手机日常使用 |
| SSH 隧道 | ★★★★★ | 低 | Linux/macOS 单次使用 |
| Tailscale 直连 IP | ★★★★☆ | 低 | Tailscale Serve 不可用时 |
| 公网/LAN 直接暴露 | ★★☆☆☆ | 高 | 不推荐，见文末安全边界 |

---

## 一、SSH 隧道（Linux / macOS / VPS）

服务器保持默认回环绑定不变，从本地机器开一条 SSH 隧道：

```bash
ssh -N -L <本地端口>:127.0.0.1:<远程端口> <用户>@<服务器地址>
```

示例（远程 8787 → 本地 8787）：

```bash
ssh -N -L 8787:127.0.0.1:8787 user@your.server.com
```

然后浏览器打开 `http://localhost:8787`。

`start.sh` 检测到你在 SSH 会话中运行时会自动打印这条命令。

---

## 二、Tailscale 私有网络访问（推荐）

[Tailscale](https://tailscale.com) 是基于 WireGuard 的零配置组网工具：
服务器和手机装上后自动进入同一个私有网络，**无需端口转发、无需公网暴露**。
Hermes WebUI 自带移动端适配布局（汉堡侧栏、抽屉式顶部标签、触控友好的控件），
作为手机的日常 AI 助手界面非常合适。

### 方案 A：Tailscale Serve（首选）

WebUI 保持绑定回环，只把端口"发布"到你的 tailnet：

1. 在服务器和手机上都安装 [Tailscale](https://tailscale.com/download) 并登录同一账号。
2. 启用密码认证后启动 WebUI（保持回环绑定）：

   ```bash
   HERMES_WEBUI_PASSWORD=你的密码 ./start.sh
   ```

   Windows 原生环境见下文"三、Windows 原生部署"。

3. 通过 Tailscale Serve 发布本地端口：

   ```bash
   tailscale serve --bg 8787
   ```

4. 用 Tailscale 打印的 HTTPS MagicDNS 地址在手机浏览器打开。

要点：

- Tailscale Serve 让 WebUI 继续待在回环上，同时为你的 tailnet 提供 HTTPS
  MagicDNS 域名，**不需要改 `HERMES_WEBUI_HOST`，也不需要开防火墙端口**。
- Linux 上修改 Serve 配置可能需要提权。若报 `Access denied: serve config denied`，
  用 sudo 执行：

  ```bash
  sudo -S -p '' tailscale serve --bg 8787
  ```

  或者把运行 WebUI 的非 root 用户设为 Tailscale operator：

  ```bash
  sudo -S -p '' tailscale set --operator=$USER
  tailscale serve --bg 8787
  ```

### 方案 B：直连 Tailscale IP（备选）

Serve 不可用/被禁用时，直接走 tailnet IP。因为这会超出回环绑定，**必须启用密码认证**：

```bash
HERMES_WEBUI_HOST=0.0.0.0 HERMES_WEBUI_PASSWORD=你的密码 ./start.sh
```

然后在手机浏览器打开 `http://<服务器Tailscale-IP>:8787`（服务器上执行
`tailscale ip -4` 查看自己的 Tailscale IP）。

流量全程由 WireGuard 端到端加密，密码认证在应用层保护界面。
可以把它添加到手机主屏，获得近似原生 App 的体验。

---

## 三、Windows 原生部署

Windows 用户不要直接 `python server.py`——请用仓库自带的原生启动器
[`start.ps1`](../start.ps1)，它会自动完成：加载 `.env`、发现 Python、
定位 hermes-agent 安装目录、设置默认环境变量，然后拉起 `server.py`。

### 1. 首次准备

原生 Windows 路径要求 Python 3.11+ 与 hermes-agent 已安装。
**关键：依赖要装在 `start.ps1` 实际使用的那个解释器里。** `start.ps1`
启动时会优先用 `<hermes-agent>\venv\Scripts\python.exe`，找不到才回退
到 PATH 上的 `python`——WebUI 仓库本地的 `venv\` 永远不会被它挑中。
所以把 `requirements.txt` 装到 hermes-agent 的 venv，而不是在 WebUI
仓库下新建一个 venv。

hermes-agent 的常见安装位置（任选其一，取决于当初的安装方式）：

```text
%USERPROFILE%\.hermes\hermes-agent            # 官方安装器默认
%LOCALAPPDATA%\hermes\hermes-agent            # 备选
%ProgramW6432%\hermes\hermes-agent           # 全局安装
```

在该目录下创建 venv 并装依赖（下面以默认的 `%USERPROFILE%\.hermes\hermes-agent`
为例）：

```powershell
cd $env:USERPROFILE\.hermes\hermes-agent
python -m venv venv
.\venv\Scripts\pip.exe install -r C:\path\to\hermes-webui\requirements.txt
```

如果 hermes-agent 装在其它位置，把第一行 `cd` 换成对应目录即可。

> 备选：在 WebUI 仓库本地建 venv 也可以（`python -m venv venv` +
> `venv\Scripts\pip install -r requirements.txt`），但这只在
> `<hermes-agent>\venv\Scripts\python.exe` 不存在、且 PATH 上的
> `python.exe` 恰好指向你装依赖时用的那个解释器时，`start.ps1` 才
> 会用上你装的依赖——非常容易踩坑。最稳的路径仍然是装到 hermes-agent
> 的 venv。

### 2. 带密码启动（前台）

```powershell
$env:HERMES_WEBUI_PASSWORD = "你的密码"
.\start.ps1
```

默认绑定 `127.0.0.1:8787`。常用参数：

| 参数 / 环境变量 | 作用 | 示例 |
|---|---|---|
| `-Port` | 端口覆盖（优先于环境变量） | `.\start.ps1 -Port 9000` |
| `-BindHost` | 绑定地址覆盖 | `.\start.ps1 -BindHost 0.0.0.0` |
| `HERMES_WEBUI_HOST` | 绑定地址（默认 `127.0.0.1`） | `$env:HERMES_WEBUI_HOST = "0.0.0.0"` |
| `HERMES_WEBUI_PORT` | 端口（默认 `8787`） | `$env:HERMES_WEBUI_PORT = "8787"` |
| `HERMES_WEBUI_PASSWORD` | 界面密码认证 | 见上 |
| `HERMES_WEBUI_AGENT_DIR` | 显式指定 hermes-agent 目录 | `$env:HERMES_WEBUI_AGENT_DIR = "C:\path\to\hermes-agent"` |

> **绑定 `0.0.0.0` 前先设密码**：一旦绑定所有接口，任何能路由到该端口的设备
> 都能触达界面，密码认证是唯一防线。参见文末"安全边界"。
>
> **改完密码先验证一下**：`start.ps1` 会加载仓库根目录的 `.env`，但
> **已经设置的环境变量优先**——上面用 `$env:HERMES_WEBUI_PASSWORD`
> 设的值会生效，`.env` 里的同名项被跳过。Linux / WSL 下的 `start.sh`
> 恰好相反：它会先 `source` 一遍 `.env`，**`.env` 里的值会覆盖**命令行
> 内联的 `HERMES_WEBUI_PASSWORD=`。无论用哪个启动脚本，改完密码后都用
> 浏览器打开登录页，确认**真的要求输入密码**且新密码能登录；不对的话，
> 先检查仓库根目录的 `.env` 里是否还留着旧值。

### 3. Windows 防火墙

**用 Tailscale Serve（方案 A）时不需要任何防火墙放行**——连接发生在
Tailscale 虚拟网卡与回环之间，Windows 防火墙不会拦截回环流量。

用"直连 Tailscale IP（方案 B）"时，Windows 防火墙可能会拦下从手机进来的
8787 连接。此时请只对 **Tailscale 接口/网络**放行，而不是对所有网络放行：

```powershell
# 允许 Tailscale 网段访问 8787（按你的实际 tailnet CIDR 调整）
New-NetFirewallRule -DisplayName "Hermes WebUI (Tailscale only)" `
  -Direction Inbound -Protocol TCP -LocalPort 8787 `
  -RemoteAddress 100.64.0.0/10 -Action Allow
```

要点：

- `100.64.0.0/10` 是 CGNAT 保留段，Tailscale 的虚拟 IP 都落在这个范围；
  这样规则只对 tailnet 生效，公网接口依然拒绝。
- 不要写 `-Profile Any -RemoteAddress Any` 这类全放行规则——那是把服务
  直接暴露到公网的行为，见"安全边界"。

## 四、WSL 用户：Windows 开机自启

> **本节只适用于在 WSL2 内运行 WebUI 的用户。** 原生 Windows 启动请看
> 上一节"三、Windows 原生部署"——`start.ps1` 本身不自带开机自启
> （Windows 登录时不会自动拉起 PowerShell 脚本），需要自启的话请改
> 用 WSL 路径或自行配置 NSSM / Task Scheduler 启动 `start.ps1`。

官方 WSL 自启通道是 **Windows 任务计划程序 + WSL 启动脚本**，配套脚本在
[`scripts/windows/setup_webui_autostart.ps1`](../scripts/windows/setup_webui_autostart.ps1)。
它注册一个"登录时启动"的计划任务，通过 `wsl.exe` 在 WSL 发行版内执行启动脚本。
该脚本幂等：重复运行只更新已有任务，不会创建重复项。

前置：WSL2 内已能手动启动 WebUI（见 [`docs/wsl-autostart.md`](wsl-autostart.md)），
并确保启动脚本可执行：

```bash
chmod +x /path/to/hermes-webui/scripts/wsl/hermes_webui_autostart.sh
```

然后回到 Windows PowerShell 注册计划任务（`-WslScriptPath` 是 **WSL 内部路径**）：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\scripts\windows\setup_webui_autostart.ps1 `
  -WslScriptPath "/home/你的用户名/hermes-webui/scripts/wsl/hermes_webui_autostart.sh" `
  -Distro "Ubuntu"
```

参数说明：

- `-Distro` 可省略，省略时使用默认 WSL 发行版。
- 任务默认名 `HermesWebUIAutoStart`，需要改名传 `-TaskName`。
- 加 `-RunNow` 注册后立即启动；加 `-WhatIf` 只预览不注册；
  仅当 WSL 路径尚不存在时才需要 `-SkipValidation`。

事后查看或移除任务：

```powershell
Get-ScheduledTask -TaskName HermesWebUIAutoStart
Unregister-ScheduledTask -TaskName HermesWebUIAutoStart -Confirm:$false
```

自启日志位置（WSL 内）：

```text
$HOME/.hermes/webui/logs/webui_autostart.log
$HOME/.hermes/webui/logs/hermes_webui.log
```

---

## 五、常见问题

| 症状 | 可能原因 | 处理 |
|---|---|---|
| 手机打不开地址 | WebUI 没绑定到可达接口 | 方案 A 检查 Serve 状态（`tailscale serve status`）；方案 B 确认 `HERMES_WEBUI_HOST=0.0.0.0` 且防火墙已放行 Tailscale |
| 任务计划已建但 WebUI 没起来 | WSL 脚本路径写错/发行版不对 | 用正确的 `-WslScriptPath` 与 `-Distro` 重跑注册脚本 |
| 打开 WSL 才启动，登录时不启动 | 用的是会话级自启而非计划任务 | 按"四、WSL 用户：Windows 开机自启"装任务计划程序 |
| 健康检查失败但进程存在 | 端口不一致或仍在启动 | 核对 `HERMES_WEBUI_PORT` 与 `hermes_webui.log` |
| 提示需要密码但没设过 | 界面要求认证 | 设置 `HERMES_WEBUI_PASSWORD` 后重启 |

---

## 六、安全边界（重要）

**区分三种暴露范围：**

1. **Tailscale 私有（推荐）**：仅你的 tailnet 设备可达。Serve 方案连端口都不用开；
   直连 IP 方案（"二、方案 B"）则要看 `HERMES_WEBUI_HOST` 设的是什么：

   - **首选：** `HERMES_WEBUI_HOST=$(tailscale ip -4)`——只绑 Tailscale
     虚拟网卡，其他接口根本不开 8787，连防火墙关了也安全。
   - **次选：** `HERMES_WEBUI_HOST=0.0.0.0` + Windows 防火墙**只对
     `100.64.0.0/10` 放行**（"三、3 防火墙"）。"仅 tailnet 可达"
     这条结论完全靠这条防火墙规则兜底——**一旦放行规则被改回
     `Any`、或防火墙被关闭，WebUI 立刻对 LAN/公网开放**，因为
     `0.0.0.0` 是所有接口，不是只绑 Tailscale 网卡。

   WireGuard 端到端加密 + 应用层密码认证是双层保险。
2. **局域网（LAN）**：`HERMES_WEBUI_HOST=0.0.0.0` 且防火墙对私有网段放行。
   同一 WiFi 下的其他设备都能触达——密码认证是必需品，且不建议用于公共 WiFi。
3. **公网**：端口对互联网开放。**不推荐**。WebUI 本身只做简单的登录限速（每个 IP
   每 60 秒最多 5 次失败尝试），但直连时没有传输加密（HTTP 明文）；若确有需要，
   应放在 HTTPS 反向代理（Caddy/Nginx）之后并启用强密码。

**所有超出回环的绑定，第一守则：先设 `HERMES_WEBUI_PASSWORD`，再改绑定地址。**

---

*原文：[`docs/remote-access.md`](remote-access.md) · 部署相关：[`docs/docker.md`](docker.md) · Windows/WSL 自启：[`docs/wsl-autostart.md`](wsl-autostart.md)*
