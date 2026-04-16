# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

Hermes WebUI 是 Hermes Agent 的浏览器端界面，提供类似 Claude 风格的对话界面。设计理念：**无构建步骤、无打包工具、无前端框架**——纯 Python 后端 + 原生 JS 前端，便于终端或 Agent 直接修改。

## 常用命令

```bash
# 启动开发服务器
python3 bootstrap.py

# 手动启动（需指定 Agent 虚拟环境的 Python）
HERMES_WEBUI_PORT=8787 python server.py

# 运行全部测试
pytest tests/ -v --timeout=60

# 运行单个测试文件
pytest tests/test_sprint1.py -v --timeout=60

# 运行单个测试函数
pytest tests/test_sprint1.py::test_function_name -v --timeout=60

# Docker 启动
docker compose up -d
```

## 架构

### 后端（Python，仅依赖 stdlib + pyyaml）

- **server.py** — 路由壳，约 154 行，将所有请求委托给 `api/routes.py`
- **api/config.py** — 路径发现、环境变量、全局状态（`SESSIONS` 字典 + `LOCK`）、模型检测
- **api/routes.py** — 所有 GET/POST 路由处理器
- **api/streaming.py** — SSE 引擎，Agent 线程运行器，`/api/chat/start` 打开 SSE 流
- **api/models.py** — Session 模型 + CRUD，JSON 持久化到 `~/.hermes/webui/sessions/`
- **api/auth.py** — 可选密码认证，HMAC 签名 Cookie（24h TTL）
- **api/upload.py** — 多部分文件上传处理
- **api/workspace.py** — 文件操作，`safe_resolve()` 防路径穿越

### 前端（原生 JS，零构建步骤）

- **static/index.html** — HTML 模板入口
- **static/boot.js** — 事件绑定、移动端导航、语音输入、启动 IIFE
- **static/messages.js** — `send()`、SSE 事件处理、审批流、转录
- **static/sessions.js** — Session CRUD、列表渲染、搜索
- **static/ui.js** — DOM 工具、Markdown 渲染、工具卡片、模型下拉
- **static/panels.js** — Cron、技能、记忆、工作区、配置文件、设置面板
- **static/commands.js** — 斜杠命令注册与自动补全
- **static/i18n.js** — 国际化，支持 en/de/es/zh/zh-Hant

### 关键设计模式

- **SSE 流式传输**：`/api/chat/start` 返回 SSE 流，事件类型 `put`（状态）、`chunk`（token）、`done`（完成）
- **线程隔离**：每个 Session 独立 Agent 线程，带环境变量备份/恢复
- **线程安全**：全局 `LOCK`（RLock）保护 `SESSIONS` 字典，per-session Agent 锁防止并发运行
- **Agent 集成**：直接 Python import（`from run_agent import AIAgent`），非 HTTP 调用，仅在流式启动时延迟导入
- **状态存储**：默认 `~/.hermes/webui/`，包含 sessions/、workspaces.json、settings.json 等

## 测试

- 测试使用隔离服务器（端口基于 repo 路径哈希），独立状态目录 `~/.hermes/webui-test-<hash>`
- 不会影响生产数据
- CI 在 Python 3.11、3.12、3.13 上运行

## 环境变量

所有可选，支持自动发现：
- `HERMES_WEBUI_AGENT_DIR` — Agent 目录路径
- `HERMES_WEBUI_PORT` — 端口（默认 8787）
- `HERMES_WEBUI_STATE_DIR` — 状态存储目录
- `HERMES_WEBUI_PASSWORD` — 启用密码认证
- `HERMES_HOME` — 基础目录（默认 `~/.hermes`）

## 安全注意事项

- POST 请求有 CSRF 校验（Origin/Referer 检查）
- HTML/Markdown 渲染有 XSS 转义
- API 响应中会脱敏凭据信息
- `safe_resolve()` 防止路径穿越攻击
- 30 秒连接超时防慢客户端耗尽

## 详细文档

完整架构设计见 `ARCHITECTURE.md`，测试指南见 `TESTING.md`，变更日志见 `CHANGELOG.md`。
