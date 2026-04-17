## Why

Hermes 当前是一个能力很强但"看不见"的 agent——所有 token 消耗、响应时间、多 surface（webui/telegram/discord/cron）活跃度都沉淀在 `state.db` 里，用户没有任何可视化入口。同行产品 OpenClaw Bot Dashboard 用 Stats 趋势图、Surface 卡片墙和一个 2D 像素办公室让用户直观感知 agent 状态；hermes 的 SQLite 数据基础其实比 OpenClaw 的 JSONL 扫描方案更整洁，却没有任何展示层。本次把"指标 + 概览 + 娱乐化呈现"三件套一次补齐。

## What Changes

- 新增 **Insights 面板**：sidebar 新增 tab，展示 token 消耗趋势（日/周/月）、响应时间分布、活跃小时热力图、按 model 的用量切片
- 新增 **Surface 总览卡片**：按 `sessions.source` 分组（webui/telegram/discord/cron/cli），每个 surface 一张卡展示会话数、最后活跃、token 消耗、健康状态
- 新增 **Pixel Office（像素办公室）**：Canvas 2D 动画办公室，每个 surface 对应一个像素角色；working → 坐工位打字，idle → 起身闲逛，waiting → 显示气泡；借鉴 OpenClaw 但**桥接层按 surface 而非 agent**
- 新增后端只读聚合端点：`/api/stats/summary`、`/api/stats/timeseries`、`/api/stats/models`、`/api/surfaces`、`/api/agent-activity`
- **前置变更**：为 `/api/sessions` 所有会话（含 webui）返回统一 `source` 字段；扩展 `filterSessions()` 支持按 source 过滤（支撑 Surface 卡片点击跳转）
- 引擎层 **手工 port** OpenClaw 的 TypeScript 像素引擎 → vanilla JS，放 `static/pixel/`，保持 hermes "零构建链"原则
- 不涉及：Alert 告警中心（明确排除）、state.db schema 变更（全部只读查询）、实时 in-flight tool 展示（无真相源，需另起 change）

## Capabilities

### New Capabilities

- `insights-panel`: Token 消耗、响应时间、活跃热力图、按 model 分组的统计面板；数据源为 `state.db` 只读 SQL 聚合；前端 SVG 渲染，无第三方图表库
- `surface-dashboard`: 按 session source 分组展示多 surface 活跃情况的卡片墙；复用 `gateway_watcher` 的 SSE 通道推送实时更新
- `pixel-office`: Canvas 2D 像素风动画办公室，以 surface 为角色单位的实时状态可视化；含引擎（state/characters/gameLoop/renderer/tileMap）、精灵资产、桥接层；不含布局编辑器、蚂蚁/matrix 特效（P2）
- `agent-activity-api`: 为 Pixel Office 和 Surface Dashboard 提供状态聚合的后端端点；**严格基于已验证的真相源**（`state.db` SELECT 聚合），输出 `{surface, state, last_active, message_count_24h, active_session_count, is_webui_current}`；可选 `?include=last_tool` 返回"最近一次工具调用"（不是"正在使用"）。**不提供** `current_tool` / `pending_count` 等无真相源的伪精确字段（详见 design D9）

### Modified Capabilities

<!-- 本次无已有 spec 的行为修改（openspec/specs/ 为空，均为新增） -->

## Impact

- **后端**：新增 `api/stats.py`、`api/agent_activity.py`；在 `api/routes.py` 的 `handle_get` 分发新增 5 个 `/api/stats/*` + `/api/surfaces` + `/api/agent-activity` + SSE stream 路由；**修改** `/api/sessions` 响应为 WebUI 会话追加统一 `source='webui'` 字段（前置任务 2.0a）；均走现有 auth/CSRF 中间件
- **前端**：`static/index.html` 新增 3 个 sidebar tab（Insights / Surfaces / Pixel）+ 对应 panel view；**修改** `static/panels.js` 的 `switchPanel()`（注意：`switchPanel()` 定义在 `panels.js:4`，不是 `ui.js`）注册新 panel 懒加载；**修改** `static/sessions.js` 的 `filterSessions()` 增加可选 `sourceFilter` 参数（前置任务 2.0b）；新增 `static/insights.js`、`static/surfaces.js`、`static/pixel/` 目录（引擎 + sprites + 桥接）；`static/style.css` 追加样式；`static/i18n.js` 新增文案键
- **数据层**：纯只读查询 `state.db` 的 `sessions` / `messages` 表；复用 `api.profiles.get_active_hermes_home()` 解析当前 profile 的 DB 路径；**无 schema 变更**
- **依赖**：无新增第三方依赖（Canvas 2D、SSE、原生 SVG 均浏览器内置；`sqlite3` stdlib 已用）
- **性能**：Stats 查询加 30s 内存缓存（对齐 OpenClaw）；Pixel Office 引擎 rAF 在 tab 不可见时自动暂停（`document.visibilityState`）
- **兼容性**：纯增量功能，不改动现有 API 和页面；旧客户端不受影响
- **排除范围**：不做 Alert 告警中心；不做布局编辑器；不做 matrix/bugs 特效；不引入 React/构建链
