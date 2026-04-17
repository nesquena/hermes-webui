## ADDED Requirements

### Requirement: Surface Dashboard 面板入口
系统 SHALL 在 sidebar 提供名为 "Surfaces" 的 nav tab。主视图 SHALL 展示当前 profile 下所有 surface 的卡片墙。surface 枚举 SHALL 从 `sessions.source` 的 distinct 值动态确定；前端图标字典 SHALL 预置以下 11 个已知 surface：`cli / webui / weixin / telegram / discord / slack / signal / whatsapp / sms / email / cron`；2026-04-17 在默认 DB 已观察到 `cli` 和 `weixin`。未在字典中的 source 归入 `other` 卡片，卡片 label 仍显示原始 source 字符串。

#### Scenario: 通过 sidebar 打开 Surfaces 视图
- **WHEN** 用户点击 sidebar 的 `Surfaces` tab
- **THEN** 主视图显示卡片网格，每个 surface 一张卡

#### Scenario: 已知 source 显示专属图标
- **WHEN** `state.db` 中存在 `source='weixin'`
- **THEN** 对应卡片使用预置的 weixin 图标与中文 label "微信"

#### Scenario: 未知 source 归类
- **WHEN** `state.db` 中存在 `source='line'` 但前端字典未定义该枚举
- **THEN** 该记录在前端合并入 `other` 卡片，卡片标题显示原始字符串 `line` 以便识别

### Requirement: Surface 卡片内容
每张 surface 卡 SHALL 展示以下字段（严格对齐 agent-activity-api 的数据能力边界，不含 `current_tool`）：surface 名称 + 图标、`active_session_count`（过去 30min 有活动且未 ended 的 session 数）、最近一次活动时间（相对时间）、最近 24h 消息数、最近 24h token 消耗（来自 `sessions` SUM）、状态指示灯（working / waiting / idle / offline）。

#### Scenario: 活跃 surface 卡
- **WHEN** 某 surface 最近 60s 内有新消息
- **THEN** 卡片状态灯为 `working`（绿色），顶部显示 "active now"

#### Scenario: 离线 surface 卡
- **WHEN** 某 surface 当前无任何活跃 session 且过去 24h 无活动
- **THEN** 卡片状态灯为 `offline`（灰色），依然展示历史累计 token 但不显示 "active now"

#### Scenario: 点击卡片跳转（依赖前置任务）
- **WHEN** 用户点击某 surface 卡片
- **THEN** 跳转到 Chat 面板并筛选该 surface 的 session 列表
- **PREREQUISITE**: 该 Scenario 依赖两个前置变更（见 tasks 2.0a / 2.0b）：
  1. 后端 `/api/sessions` 响应中**所有** session（含 webui）SHALL 返回统一的 `source` 字段；当前 WebUI 会话无此字段，CLI 会话用 `source_tag`，需合并为统一 `source`
  2. 前端 `static/sessions.js` 的 `filterSessions()` 当前仅按标题 / 内容过滤（`sessions.js:353`），需扩展为支持按 `source` 过滤，接受一个可选的 `sourceFilter` 参数
- **AND** 若上述两个前置任务在本 change 未完成，本 Scenario SHALL 降级：点击卡片改为 Toast 提示 "Session filtering by surface coming soon"，不做跳转

### Requirement: 实时更新
Surface Dashboard SHALL 通过 SSE 实时接收 surface 状态变更，延迟不超过 10 秒。

#### Scenario: SSE 事件驱动更新
- **WHEN** `gateway_watcher` 或 `agent_activity` 后台检测到某 surface 的 `message_count` 变化
- **THEN** 通过 `/api/agent-activity/stream` SSE 推送事件到前端
- **AND** 前端在不重新加载页面的情况下更新对应卡片的计数与状态灯

#### Scenario: SSE 断线重连
- **WHEN** 客户端与服务端 SSE 连接意外断开
- **THEN** 客户端在 5 秒内自动重连（复用现有 `EventSource` 重连策略）；重连成功后立即拉取最新全量快照

### Requirement: 后端聚合端点 /api/surfaces
系统 SHALL 提供 `GET /api/surfaces` 返回当前 active profile 全部 surface 的快照数据 `{surfaces: [{source, icon_key, state, active_session_count, last_activity, messages_24h, tokens_24h}]}`。经 `require_auth` 鉴权。数据源严格限定为 `state.db` SELECT 聚合；**不得** 从 `SESSIONS` LRU 推断非 webui surface 的 `active_session_count`（见 agent-activity-api spec 的"数据能力边界"）。

#### Scenario: 全量快照
- **WHEN** 客户端首次打开 Surfaces 面板
- **THEN** GET `/api/surfaces` 返回所有 surface 的当前快照
- **AND** 快照完全来自 `state.db` 聚合查询（`sessions` + `messages` JOIN）

#### Scenario: icon_key 由后端发出
- **WHEN** 响应构建时遇到已知 source（如 `weixin`）
- **THEN** 附加 `icon_key = "weixin"` 供前端字典映射；未知 source 的 `icon_key = "other"` 但 `source` 字段仍保留原始字符串

#### Scenario: 查询缓存
- **WHEN** 2 秒内重复请求
- **THEN** 命中内存缓存，不再触发 DB 查询

### Requirement: 移动端降级
在视口宽度小于 640px 时，Surface Dashboard SHALL 仍可用，卡片 SHALL 垂直堆叠成单列。

#### Scenario: 移动端布局
- **WHEN** 用户在手机浏览器打开 Surfaces 面板
- **THEN** 卡片采用单列布局，字体与间距按现有 hermes 移动端样式规范缩放
