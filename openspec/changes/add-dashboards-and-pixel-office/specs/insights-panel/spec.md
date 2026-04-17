## ADDED Requirements

### Requirement: Insights 面板入口
系统 SHALL 在 sidebar 提供名为 "Insights" 的 nav tab，点击后切换到指标展示视图。该视图 SHALL 包含四个子区块：token 消耗趋势图、响应时间分布图、活跃热力图、按 model 的用量切片表。

#### Scenario: 通过 sidebar 打开 Insights 视图
- **WHEN** 用户在 sidebar 点击 `Insights` tab
- **THEN** 主视图切换显示四个子区块，上次选中的其他 tab 取消激活状态，URL hash 或 localStorage 记录当前选择

#### Scenario: 多语言标签
- **WHEN** 用户切换 UI 语言为中文 / 英文 / 德文 / 繁体中文
- **THEN** Insights tab 标签与各子区块标题按 `i18n.js` 当前语言显示

### Requirement: Token 消耗趋势图（主折线）
系统 SHALL 展示**主折线图**显示最近 N 天（默认 30 天）每日 **总 token**（合并值，即 `messages.token_count` 求和）消耗，支持"日 / 周 / 月"粒度切换。数据源 SHALL 为当前 active profile 的 `state.db` `messages` 表按 `DATE(timestamp, 'unixepoch')` 聚合。理由：`messages` 表只存储合并的 `token_count`，没有 input/output 拆分；日粒度取决于消息实际发生时间，因此主折线只能画"总 token"。

#### Scenario: 默认加载 30 天日粒度
- **WHEN** 用户打开 Insights 面板
- **THEN** 主折线图展示过去 30 天每日总 token 消耗（单条线 + 面积填充），X 轴为日期，Y 轴为 token 数
- **AND** 图表下方以小字注明数据来源："messages.token_count per day"

#### Scenario: 切换到周粒度
- **WHEN** 用户点击粒度切换按钮选择 "周"
- **THEN** 数据按 ISO 周分桶重新聚合并重绘；URL query 或 panel state 记录粒度选择以便刷新保留

#### Scenario: 无数据状态
- **WHEN** `messages` 表为空或时间窗口内无记录
- **THEN** 折线图区域显示友好空态提示（如 "No activity yet"），而非崩溃或显示空白 SVG

### Requirement: Input/Output Token 拆分（次级切片）
系统 SHALL 在主折线下方额外展示**次级切片**展示 input / output / cache_read / reasoning token 的构成比例，数据源 SHALL 为 `sessions` 表按 `DATE(started_at, 'unixepoch')` 聚合（按 session 起始日归集）。UI SHALL 明确注明"按 session 起始日归集"。

#### Scenario: 堆叠柱状图展示 token 构成
- **WHEN** 用户查看 token 趋势子区块
- **THEN** 主折线图下方展示堆叠柱状图，每个柱子分 4 段：input / output / cache_read / reasoning
- **AND** 图表下方以小字注明："按 sessions.started_at 归集——跨日 session 归入起始日"

#### Scenario: 长 session 归属说明
- **WHEN** 用户 hover 某一日的柱子
- **THEN** tooltip 显示该日起始的全部 session 总 token 拆分，并注明"含跨日 session 的全程 token"

### Requirement: 响应时间分布
系统 SHALL 展示柱状图显示 user→assistant 的响应时间分布，按固定分桶（0–1s / 1–3s / 3–10s / 10–30s / 30s+）。响应时间 SHALL 由同一 session 内连续的 user 消息和其后 assistant 消息的 timestamp 差值计算，超过 10 分钟的差值被过滤掉。

#### Scenario: 分桶展示
- **WHEN** 用户查看响应时间子区块
- **THEN** 柱状图按 5 个分桶展示每个区间的消息数量，按钮支持切换时间窗口（7 天 / 30 天）

#### Scenario: 过滤异常长间隔
- **WHEN** 两条相邻 user/assistant 消息间隔超过 10 分钟
- **THEN** 该间隔不计入分布统计（视为会话暂停，非响应延迟）

### Requirement: 活跃热力图
系统 SHALL 展示 7×24 格热力图，展示一周内每个小时的消息数量，颜色深浅代表活跃度。

#### Scenario: 热力图渲染
- **WHEN** 用户查看活跃热力图
- **THEN** SVG 渲染 7 行 24 列格子，cell 的 fill 由 `accent` 颜色按对数标度加 alpha 表示 bucket 消息数
- **AND** hover 任意 cell 显示 tooltip "周X HH:00 — N messages"

### Requirement: 按模型的用量切片
系统 SHALL 展示表格列出每个 model 的 input/output token、消息数、估算成本、占比。数据源 SHALL 为 `sessions` 表（含 `input_tokens / output_tokens / message_count / estimated_cost_usd / model`）。

#### Scenario: 模型用量表
- **WHEN** 用户查看模型用量子区块
- **THEN** 表格展示 model 名、input tokens、output tokens、message count、estimated_cost_usd、total token 占比百分比
- **AND** 表格按 total token 降序排序

### Requirement: 后端聚合端点 /api/stats/*
系统 SHALL 提供以下 GET 端点，均经过 `require_auth` 鉴权，返回 JSON。所有 SQL 查询基于已验证的 `state.db` schema（2026-04-17）：
- `GET /api/stats/summary` — 总览卡片：`{total_messages, total_input_tokens, total_output_tokens, total_cost_usd, active_sessions, last_activity_ts}`。前三个 token 值来自 `sessions` 表 SUM，last_activity 来自 `MAX(messages.timestamp)`
- `GET /api/stats/timeseries?granularity=day|week|month&window=30&source=total|split` — `source=total` 时从 `messages.token_count` 按 `DATE(timestamp)` 聚合返回 `[{date, total, count}]`；`source=split` 时从 `sessions` 按 `DATE(started_at)` 聚合返回 `[{date, input, output, cache_read, reasoning, cost}]`
- `GET /api/stats/response-time?window=30` — 响应时间分桶 `{buckets: [{label, count, min_ms, max_ms}]}`，基于 `messages.timestamp` + `role` 同 session 内连续 user→assistant 差值，过滤 >600s
- `GET /api/stats/heatmap?window=7` — 7×24 活跃度矩阵 `{cells: [[msgs]x24]x7}`，基于 `strftime('%w', timestamp, 'unixepoch')` + `strftime('%H', ...)` 分组
- `GET /api/stats/models?window=30` — 按 model 聚合 `[{model, input, output, count, cost_usd, pct}]`，基于 `sessions` GROUP BY `model`

#### Scenario: 未鉴权拒绝
- **WHEN** 未登录客户端请求任一 `/api/stats/*`
- **THEN** 响应 401 并携带 `WWW-Authenticate` 或 hermes 现有鉴权错误格式

#### Scenario: 查询超窗口参数被夹紧
- **WHEN** 客户端传入 `window=9999`
- **THEN** 后端夹紧到合法上限（如 365）并返回数据，不报错

#### Scenario: 30 秒内存缓存命中
- **WHEN** 30 秒内相同参数连续调用同一端点
- **THEN** 第二次及以后请求直接命中内存缓存，不触发 DB 查询（可通过日志或 `X-Cache: HIT` 响应头观察）

#### Scenario: 强制刷新
- **WHEN** 客户端传入 `refresh=1` 查询参数
- **THEN** 后端绕过缓存重新查询 DB 并更新缓存

### Requirement: Profile 隔离
所有 Stats 查询 SHALL 基于当前 active profile 的 `state.db`。Profile 切换后 SHALL 立即失效旧缓存。

#### Scenario: 切换 profile 后数据隔离
- **WHEN** 用户通过 `/api/profile/switch` 切换到另一 profile
- **GIVEN** 上一 profile 的 Stats 已缓存
- **THEN** 下一次 `/api/stats/summary` 查询新 profile 的 `state.db`，不返回上一 profile 的数据

### Requirement: 只读保证
系统 SHALL 仅对 `state.db` 执行 SELECT 查询，不执行任何 INSERT/UPDATE/DELETE/DDL。

#### Scenario: 只读连接
- **WHEN** 后端打开 `state.db`
- **THEN** 使用 `mode=ro` URI 或等价只读连接；任何写操作尝试都被 SQLite 层拒绝
