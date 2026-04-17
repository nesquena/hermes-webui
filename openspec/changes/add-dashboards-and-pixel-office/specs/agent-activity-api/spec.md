## ADDED Requirements

### Requirement: 数据视角与能力边界
本 API 的所有字段 SHALL **仅基于 `state.db` 的可观测数据**（session 元数据 + message 时间戳 + session source）和 webui 进程内的 WebUI 会话 LRU 缓存派生。本 API SHALL NOT 声称知道 hermes-agent 进程内部的运行时状态，包括但不限于：当前正在执行的工具名、待处理的用户消息队列、模型推理是否进行中、非 WebUI surface 的"会话是否处于工具调用栈中间"。

#### Scenario: 未暴露运行时状态字段
- **WHEN** 客户端请求 `/api/agent-activity` 或 `/api/agent-activity/stream`
- **THEN** 响应体不包含 `current_tool` / `pending_count` / `is_running_tool` 等字段
- **AND** 相关字段如需要在未来补齐，应等待 agent↔webui IPC 设计落地后新增

#### Scenario: 文案不暗示运行时感知
- **WHEN** 前端展示某 surface 的状态
- **THEN** 使用 "Last message 42s ago" / "active based on recent messages" 等基于时间戳的措辞
- **AND** 不使用 "Agent is running Bash" / "Waiting for your reply" 等暗示 webui 已经知道 agent 内部状态的措辞

### Requirement: /api/agent-activity 快照端点
系统 SHALL 提供 `GET /api/agent-activity`，返回当前 active profile 下所有 surface 的活动快照。响应格式为 JSON：
```
{
  "surfaces": [
    {
      "source": "<string>",
      "state": "working|waiting|idle|offline",
      "last_active_ts": <unix-seconds or null>,
      "message_count_24h": <int>,
      "tokens_24h": <int>,
      "active_webui_sessions": <int, webui surface only>
    },
    ...
  ],
  "generated_at": <unix-seconds>,
  "profile": "<active-profile-name>"
}
```
`active_webui_sessions` 字段 SHALL 仅存在于 `source == "webui"` 的条目中；其他 surface 的条目 SHALL 省略该字段（而非填 0），以避免暗示 webui 已经知道 telegram/discord 等外部 surface 的会话运行时状态。端点 SHALL 经 `require_auth` 鉴权。

#### Scenario: 返回全量快照
- **WHEN** 客户端 GET `/api/agent-activity`
- **THEN** 响应包含每个 surface 的当前 `state`（working/waiting/idle/offline）
- **AND** `last_active_ts` 为该 surface 最后一条 message 的 UNIX timestamp（秒）；若无 message 则为 `null`

#### Scenario: 无活跃 surface
- **WHEN** 当前 profile 的 `sessions` 表为空
- **THEN** 响应返回 `"surfaces": []`、`profile` 字段仍正常填充，HTTP 状态 200

#### Scenario: active_webui_sessions 仅限 webui
- **WHEN** `surfaces` 数组里同时包含 `source=webui` 与 `source=telegram` 的条目
- **THEN** webui 条目含 `active_webui_sessions` 字段
- **AND** telegram 条目**不包含** `active_webui_sessions` 字段

### Requirement: /api/agent-activity/stream SSE 端点
系统 SHALL 提供 `GET /api/agent-activity/stream` 作为 Server-Sent Events 长连接。服务端 SHALL 在 surface 状态变化时推送事件，空闲时每 30 秒发送 `heartbeat` 保活。端点 SHALL 经 `require_auth` 鉴权。

#### Scenario: 订阅状态流
- **WHEN** 客户端建立 SSE 连接
- **THEN** 服务端立即发送一次全量快照作为初始事件（`event: snapshot`）
- **AND** 之后仅在状态变化时推送 delta 事件（`event: delta`），包含变更的 surface 子集

#### Scenario: 心跳保活
- **WHEN** 连接建立后 30 秒内无状态变化
- **THEN** 服务端发送 `event: heartbeat` 保持连接不被代理超时

#### Scenario: Profile 切换断开并重新计算枚举
- **WHEN** 用户通过 `/api/profile/switch` 切换 profile
- **THEN** 该 SSE 连接被服务端关闭
- **AND** 客户端重连时，服务端对新 profile 的 DB 重新执行 `SELECT DISTINCT source` 重建 surface 枚举——旧 profile 特有的 surface 不残留，新 profile 特有的 surface 自动出现

### Requirement: 状态推导规则
系统 SHALL 使用以下规则从 `state.db` 的每个 surface 的 `MAX(messages.timestamp)`（通过 `sessions.source` JOIN 聚合）推导 surface 状态。本规则**不引用** in-memory `SESSIONS` 或"是否有活跃 session"概念，因为 webui 无法可靠判断非 webui surface 的"活跃 session"。

| 状态 | 推导条件（基于 `now - last_msg_ts`） |
|---|---|
| `working` | 最近 60 秒内该 surface 有新 message |
| `waiting` | 60 秒 < 距今 ≤ 300 秒 |
| `idle`    | 300 秒 < 距今 ≤ 24 小时 |
| `offline` | 距今 > 24 小时，或该 surface 在 `sessions.source` 中不存在任何 message |

#### Scenario: working 判定
- **WHEN** surface `telegram` 在最近 30 秒内收到新 message
- **THEN** `/api/agent-activity` 返回该 surface `state = "working"`

#### Scenario: waiting 判定
- **WHEN** surface 最后一条 message 距今 2 分钟
- **THEN** 返回 `state = "waiting"`

#### Scenario: 从 waiting 进入 idle
- **WHEN** surface 最后一条 message 距今 6 分钟
- **THEN** 返回 `state = "idle"`

#### Scenario: offline 判定
- **WHEN** surface 最后一条 message 距今 48 小时，或该 source 从未出现过 message
- **THEN** 返回 `state = "offline"`

### Requirement: 数据源与缓存
后端 SHALL 以 `state.db`（SELECT-only）+ webui 进程内的 `SESSIONS` LRU 缓存（**仅用于 `active_webui_sessions` 字段**）为数据源。`/api/agent-activity` 快照 SHALL 使用 2 秒 TTL 内存缓存，按 active profile 的 `hermes_home` 路径作为 cache key。Profile 切换后旧 cache SHALL 失效。

#### Scenario: 缓存命中
- **WHEN** 2 秒内重复 GET `/api/agent-activity`
- **THEN** 第二次请求命中缓存，不重复扫描 DB

#### Scenario: 强制刷新
- **WHEN** 查询参数 `refresh=1`
- **THEN** 绕过缓存重建快照

#### Scenario: profile 切换失效缓存
- **WHEN** 切换 profile 后首次请求
- **THEN** cache key 不同，重新走完整查询

### Requirement: Surface 枚举动态化
系统 SHALL **不在进程启动时**固定 surface 枚举。每次构建快照时，系统 SHALL 对当前 active profile 的 `state.db` 执行 `SELECT DISTINCT source FROM sessions` 以获取该 profile 实际出现过的 surface 集合。

#### Scenario: Profile 间 surface 集合不同
- **GIVEN** profile A 的 `sessions.source` 仅有 `cli`
- **GIVEN** profile B 的 `sessions.source` 包含 `telegram / webui / weixin`
- **WHEN** 用户在 profile A 下请求 `/api/agent-activity`
- **THEN** 响应 `surfaces` 数组只含 cli 相关条目
- **WHEN** 切换到 profile B 后请求
- **THEN** 响应 `surfaces` 数组反映 profile B 的三个 surface，无 profile A 残留

### Requirement: 与 gateway_watcher 协作
新 `agent_activity` 模块 SHALL NOT 重复造轮子覆盖 `gateway_watcher` 的职责；SHALL 通过 `GatewayWatcher.subscribe()` 获取 `sessions_changed` 事件，本地按 surface 重新聚合为 `/api/agent-activity/stream` 的 `delta` 事件格式。

#### Scenario: 共享轮询
- **WHEN** `gateway_watcher` 完成一次 5s DB 轮询并推送 `sessions_changed`
- **THEN** `agent_activity` 模块收到事件后按 surface 聚合产出 SSE `delta`，不额外发起 DB 查询

### Requirement: 只读与安全
所有查询 SHALL 以 SELECT-only 模式打开 `state.db`（`file:...?mode=ro` URI）。端点 SHALL 复用现有 auth / profile 解析中间件。Session 标题若出现在响应中（本 API 不含，但未来扩展时）SHALL 经 `_redact_text` 脱敏。

#### Scenario: 未鉴权拒绝
- **WHEN** 未登录客户端请求 `/api/agent-activity` 或其 stream
- **THEN** 响应 401 并关闭 SSE 连接

#### Scenario: 只读 DB 连接
- **WHEN** 后端打开 `state.db` 执行查询
- **THEN** 使用 `mode=ro` URI；任何 INSERT/UPDATE/DELETE 尝试都被 SQLite 层拒绝
