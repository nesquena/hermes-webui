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
每张 surface 卡 SHALL 展示至少以下字段：surface 名称 + 图标、最近一次活动时间（相对时间）、最近 24h 消息数、最近 24h token 消耗、状态指示灯（working / waiting / idle / offline）。仅当 `source == "webui"` 时 SHALL 额外显示 "active webui sessions: N"；非 webui surface **不展示** "active sessions" 字段（因为 webui 无法可靠获取 telegram/discord 等 surface 的活跃会话数，避免误导）。

#### Scenario: 活跃 surface 卡
- **WHEN** 某 surface 最近 60s 内有新消息
- **THEN** 卡片状态灯为 `working`（绿色），顶部显示 "Last message N seconds ago"

#### Scenario: 离线 surface 卡
- **WHEN** 某 surface 最近 24h 无活动
- **THEN** 卡片状态灯为 `offline`（灰色），依然展示历史累计 24h 计数（可能全 0）

#### Scenario: webui 卡独占 active sessions 字段
- **WHEN** 卡片渲染 webui surface
- **THEN** 显示 "N active sessions" 字样并取值自 `active_webui_sessions`
- **WHEN** 卡片渲染 telegram surface
- **THEN** 不渲染 "active sessions" 字样

### Requirement: 点击卡片就地展开详情
系统 SHALL 在用户点击某 surface 卡片时**就地展开**该 surface 的最近 N 条 session 摘要（折叠式抽屉），而**不跳转**到 Chat 面板。理由：现有 `/api/sessions` 对 webui 会话无统一 `source` 字段（`api/routes.py:554`），现有 `filterSessions()`（`static/sessions.js:353`）只支持标题/内容关键字搜索，不支持按 source 筛选。跨面板 source 过滤需要改动 `/api/sessions` 响应契约与 `filterSessions` 函数——不在本 change 范围。

#### Scenario: 点击展开
- **WHEN** 用户点击某 surface 卡片
- **THEN** 卡片下方折叠展开区域，异步拉取 `/api/surfaces?source=<src>&expand=1` 获取该 surface 最近 5 条 session 摘要（title / model / last_activity / message_count）

#### Scenario: 再次点击折叠
- **WHEN** 用户再次点击已展开的卡片
- **THEN** 抽屉收起；再次展开时复用最近一次拉取结果（30s 内）

#### Scenario: 不跨面板跳转
- **WHEN** 用户点击卡片
- **THEN** 不切换 panel tab，不调用 `switchPanel('chat')`，不调用 `filterSessions()`

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
系统 SHALL 提供 `GET /api/surfaces` 返回当前 active profile 全部 surface 的快照 `{surfaces: [{source, state, last_active_ts, message_count_24h, tokens_24h, active_webui_sessions?}], profile, generated_at}`。`active_webui_sessions` 仅出现在 `source=="webui"` 的条目。本端点 SHALL 支持查询参数 `?source=<src>&expand=1` 返回指定 surface 的最近 5 条 session 摘要 `{sessions: [{session_id, title, model, last_activity, message_count}]}`，用于"点击卡片就地展开"。端点 SHALL 经 `require_auth` 鉴权。

#### Scenario: 全量快照
- **WHEN** 客户端首次打开 Surfaces 面板
- **THEN** GET `/api/surfaces` 返回所有 surface 的当前快照
- **AND** 快照来自 `state.db` 的 SELECT-only 查询；webui 条目额外注入 `active_webui_sessions`（从 webui 进程内的 `SESSIONS` LRU 缓存计数）

#### Scenario: 展开查询
- **WHEN** 客户端请求 `/api/surfaces?source=telegram&expand=1`
- **THEN** 响应为 `{sessions: [...]}`，包含该 source 下按 `last_activity DESC` 排序的前 5 条 session 摘要
- **AND** session 标题经 `_redact_text` 脱敏

#### Scenario: 查询缓存
- **WHEN** 2 秒内重复请求（相同参数）
- **THEN** 命中内存缓存，不再触发 DB 查询

#### Scenario: 未知 source 的展开请求
- **WHEN** 客户端请求 `?source=bogus&expand=1` 但该 source 不在 `SELECT DISTINCT source` 结果内
- **THEN** 返回 `{sessions: []}`（空列表，非 404）

### Requirement: 移动端降级
在视口宽度小于 640px 时，Surface Dashboard SHALL 仍可用，卡片 SHALL 垂直堆叠成单列。

#### Scenario: 移动端布局
- **WHEN** 用户在手机浏览器打开 Surfaces 面板
- **THEN** 卡片采用单列布局，字体与间距按现有 hermes 移动端样式规范缩放
