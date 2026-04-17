## ADDED Requirements

### Requirement: 数据能力边界（先于一切）
系统 SHALL 仅基于 Hermes **当前实际存在的数据源**暴露字段，不得返回任何无法可靠推导的"伪精确"字段。已验证的真相源：
- `state.db` 的 `sessions` 表：`source / started_at / ended_at / message_count / model / title`
- `state.db` 的 `messages` 表：`session_id / timestamp / role / tool_calls / tool_name`（注意：`tool_name` 是已完成的 tool call 记录，不是"实时正在执行的 tool"）
- `gateway_watcher` 5s 轮询快照（`api/gateway_watcher.py:49–86`）：**仅非 webui** 会话的 `source / message_count / last_activity`
- `api/models.py` 的 `SESSIONS` 字典：WebUI 会话的 **LRU 缓存**（上限 `SESSIONS_MAX=100`），**不是** 跨 surface 的实时活动注册表
- `api/routes.py` 的 `_pending` approvals 队列：**仅 WebUI** 的 approval/clarify 等待

明确 **不可获得** 的字段（本 change 不得在响应中伪造）：
- `current_tool`（"该 surface 此刻正在执行哪个工具"）—— 无真相源；Hermes 没有为 Telegram/Discord 等 surface 维护实时 in-flight tool 注册表
- `pending_count` **跨 surface 统一语义** —— 仅 WebUI 有 `_pending`；其他 surface 的"等待"概念不存在

#### Scenario: 响应字段严格受限
- **WHEN** 客户端 GET `/api/agent-activity`
- **THEN** 响应 surface 对象**仅**包含下列字段：`source / state / last_active / message_count_24h / active_session_count / is_webui_current`
- **AND** 响应体**不**含 `current_tool` / `pending_count` 等未纳入真相源的字段

#### Scenario: 最近使用的工具（可选、明确语义）
- **WHEN** 客户端传 `?include=last_tool` 查询参数
- **AND** 该 surface 最近 60s 内有 `messages.tool_name` 非空的记录
- **THEN** 响应附加 `last_tool_name`（字符串，含时间戳），UI 文案必须显示为"最近一次工具调用"而非"正在使用"

### Requirement: /api/agent-activity 快照端点
系统 SHALL 提供 `GET /api/agent-activity`，返回当前 active profile 下所有 surface 的活动快照。响应格式为 JSON：
```
{
  generated_at: <unix_ts>,
  surfaces: [
    {
      source: "<string>",
      state: "working"|"waiting"|"idle"|"offline",
      last_active: <unix_ts or null>,
      message_count_24h: <int>,
      active_session_count: <int>,   // 过去 30min 有活动且 ended_at 为 NULL 的 session 数
      is_webui_current: <bool>       // 当前浏览器对应 session 是否属于此 surface (仅 webui 卡为 true)
    }
  ]
}
```
经 `require_auth` 鉴权。

#### Scenario: 返回全量快照
- **WHEN** 客户端 GET `/api/agent-activity`
- **THEN** 响应包含每个 surface 的 `state`、`last_active`、`message_count_24h`、`active_session_count`

#### Scenario: 无活跃 surface
- **WHEN** 所有 surface 过去 30 分钟无活动
- **THEN** 响应仍返回 `surfaces` 数组（每个 surface `state=offline`），不返回空体

### Requirement: /api/agent-activity/stream SSE 端点
系统 SHALL 提供 `GET /api/agent-activity/stream` 作为 Server-Sent Events 长连接。服务端 SHALL 在 surface 状态变化时推送事件，空闲时每 30 秒发送 `heartbeat` 保活。

#### Scenario: 订阅状态流
- **WHEN** 客户端建立 SSE 连接
- **THEN** 服务端立即发送一次全量快照作为初始事件（`event: snapshot`）
- **AND** 之后仅在状态变化时推送 delta 事件（`event: delta`），包含变更的 surface 子集

#### Scenario: 心跳保活
- **WHEN** 连接建立后 30 秒内无状态变化
- **THEN** 服务端发送 `event: heartbeat` 保持连接不被代理超时

#### Scenario: Profile 切换断开
- **WHEN** 用户通过 `/api/profile/switch` 切换 profile
- **THEN** 该 SSE 连接被服务端关闭，触发客户端重连以订阅新 profile 的流
- **AND** 重连后服务端**重新**查询新 profile 的 `sessions.source` distinct 集合（不得使用启动时的全局枚举缓存）

### Requirement: 状态推导规则（只用能获得的数据）
系统 SHALL 使用以下规则从可获得数据源推导 surface 状态。`active_session` 定义：`sessions.ended_at IS NULL AND MAX(messages.timestamp) > now - 1800`：
- `working`: 该 surface 有 active_session 且其最近一条 message 在 60s 内
- `waiting`: 该 surface 有 active_session 且最近 message 在 60s–300s
- `idle`: 该 surface 有 active_session 且最近 message 在 300s–1800s
- `offline`: 该 surface 无 active_session（>1800s 无 message 或 session 已 ended）

#### Scenario: working 判定
- **WHEN** surface `telegram` 的某 session 在最近 30 秒内有新 `messages` 记录
- **AND** 该 session 的 `ended_at` 为 NULL
- **THEN** 快照返回 telegram.state = "working"

#### Scenario: offline 判定
- **WHEN** surface 过去 30 分钟无 message 写入
- **THEN** 返回 state = "offline"，`active_session_count = 0`

#### Scenario: 跨 surface 的状态可靠性
- **WHEN** 后端无法为某 surface 找到任何 active_session（因其完全没有 gateway_watcher 轨迹或 DB 记录）
- **THEN** **不得** 虚构 `working` 或 `waiting` 状态；必须返回 `offline`

### Requirement: Profile 隔离的 surface 枚举
系统 SHALL 按 profile 独立查询 `SELECT DISTINCT source FROM sessions`，**不得** 在全局启动时一次性固定 surface 枚举。枚举结果按 profile hermes_home 路径做缓存 key，profile 切换时 SHALL 失效对应缓存并重新查询。

#### Scenario: profile A 与 profile B 的 surface 集合不同
- **GIVEN** profile A 的 `state.db` 有 source `cli / webui`
- **AND** profile B 的 `state.db` 有 source `cli / weixin / telegram`
- **WHEN** 用户从 A 切换到 B 并请求 `/api/agent-activity`
- **THEN** 响应中 surfaces 数组包含 B 的 3 个 source；不得残留 A 的 `webui` 卡，也不得遗漏 B 的 `weixin` 和 `telegram`

#### Scenario: 运行时新 source 首次出现
- **WHEN** 某 surface（如新接入的 `line`）**首次** 写入 `sessions.source`
- **THEN** 在缓存 TTL（2s）内或下次 DB poll 后，该 surface 出现在 `/api/agent-activity` 响应中

### Requirement: 数据源与缓存
后端 SHALL 以 `state.db`（SELECT-only，`mode=ro` URI）+ `gateway_watcher` 共享快照 + WebUI 专属的 `SESSIONS` LRU（**仅**用于判定当前浏览器自身的 `is_webui_current`）为数据源。`/api/agent-activity` 快照 SHALL 使用 2 秒 TTL 内存缓存，按 profile hermes_home 路径作为 cache key。

#### Scenario: 缓存命中
- **WHEN** 2 秒内重复 GET `/api/agent-activity`
- **THEN** 第二次请求命中缓存，不重复扫描 DB

#### Scenario: 强制刷新
- **WHEN** 查询参数 `refresh=1`
- **THEN** 绕过缓存重建快照

#### Scenario: SESSIONS LRU 不被误用为跨 surface 事实源
- **WHEN** 后端构建 surface 活动快照
- **THEN** `SESSIONS` LRU 只用于标注 `is_webui_current`；其他 surface 的 `active_session_count` 全部从 `state.db` 聚合（不从 SESSIONS 推断 telegram/discord 等非 webui surface）

### Requirement: 与 gateway_watcher 协作
新后台逻辑 SHALL NOT 重复造轮子覆盖 `gateway_watcher` 的职责；SHOULD 复用 `gateway_watcher` 的 DB 快照哈希与推送队列机制，或共享其后台轮询结果。注意 `gateway_watcher` 当前 WHERE 子句为 `source != 'webui'`；本 change 需在 `api/agent_activity.py` 内**额外** 聚合 webui surface（从 `sessions` 表中 `source = 'webui'` 的行单独查询），不得扩大 `gateway_watcher` 的职责范围以免破坏现有 `/api/sessions/gateway/stream` 契约。

#### Scenario: 共享轮询
- **WHEN** `gateway_watcher` 完成一次 5s DB 轮询
- **THEN** `agent_activity` 模块复用同一快照生成非 webui surface 聚合，额外一次 SELECT 补 webui surface

### Requirement: 只读与安全
所有查询 SHALL 以 SELECT-only 模式打开 `state.db`。端点 SHALL 复用现有 auth / CSRF / profile 解析中间件。Session 标题若包含敏感信息 SHALL 经 `_redact_text` 脱敏。

#### Scenario: 未鉴权拒绝
- **WHEN** 未登录客户端请求 `/api/agent-activity` 或其 stream
- **THEN** 响应 401 并关闭 SSE 连接
