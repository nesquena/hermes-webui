## Context

hermes-webui 当前形态：Python stdlib HTTP 服务（`server.py` + `api/routes.py` 分发），静态前端（`static/index.html` + `boot.js/panels.js/ui.js`），零构建链。核心数据落在 `~/.hermes/state.db`（SQLite），其中 `sessions` 表记录 `(id, title, model, message_count, started_at, source)`，`messages` 表记录 `(session_id, timestamp, role, ...)` —— 含所有 surface（webui/telegram/discord/cron/cli）活动。gateway 相关 surface 已通过 `api/gateway_watcher.py` 以 5s 轮询差量 + SSE (`/api/sessions/gateway/stream`) 推送到前端，但没有任何"聚合视图"或"可视化"层。

参考对象：`~/Codes/OpenClaw-bot-review`（Next.js + React），其 `/stats`（460 行）、`/pixel-office`（2328 行 + `lib/pixel-office/` 多模块）已经把相似数据展开成产品化体验，但产品定位是"多 agent 运维看板"，和 hermes"单 agent 多 surface"语义不同；不能直接 fork。

利益相关者：
- hermes 个人用户（本地部署）—— 想"看见"自己的 agent 在忙什么、吃了多少 token
- 运维使用者（远程部署 + 多 surface）—— 想快速判断哪条消息通路异常
- 未来潜在贡献者 —— 保留 hermes"零构建、stdlib 后端"的入门友好性

## Goals / Non-Goals

**Goals:**
- 补齐 token / 响应时间 / 活跃度等指标的可视化（Insights 面板）
- 给多 surface 用户一个"谁在线谁沉默"的鸟瞰视角（Surface Dashboard）
- 以"每个 surface 一个像素人"为核心语义提供娱乐化监控（Pixel Office P1）
- 所有数据聚合纯只读，不改 `state.db` schema，不破坏现有 API
- 保持 hermes 零构建链原则：纯 vanilla JS + inline SVG + Canvas 2D
- 复用已有的 auth/CSRF/profiles/SSE 基础设施

**Non-Goals:**
- 不做 Alert 告警中心（用户明确排除；cron 已可替代）
- 不做 Pixel Office 的布局编辑器（P2，留给后续 change）
- 不做蚂蚁信息素 / matrix 特效 / 音效（P2）
- 不引入 React / TypeScript / 构建工具链
- 不做多 profile 聚合视图（每次只展示当前 active profile 数据，profile 切换由 `/api/profile/switch` 已有机制负责）
- 不向 `state.db` 写入任何数据（保持只读）

## Decisions

### D1. 数据源 —— `state.db` SQL 聚合，不扫 session JSON

OpenClaw 的 `/api/stats-all` 扫 `~/.openclaw/agents/*/sessions/*.jsonl` 逐行解析。hermes 已有结构化 `state.db`，可以 SQL 直查。**Schema 已验证**（2026-04-17 对 `~/.hermes/state.db` 运行 `.schema`）：

**`sessions` 表（Token 信息落在这里）**：
`input_tokens / output_tokens / cache_read_tokens / cache_write_tokens / reasoning_tokens / estimated_cost_usd / actual_cost_usd / message_count / tool_call_count / model / source / started_at / ended_at / title`

**`messages` 表（仅聚合粒度的 token）**：
`id / session_id / role / content / tool_call_id / tool_calls / tool_name / timestamp / token_count / finish_reason / reasoning / reasoning_details / codex_reasoning_items`

⚠️ **关键发现**：`messages` 表只有 `token_count`（合并值），没有 input/output 拆分；input/output 拆分只存在于 `sessions` 表。因此聚合策略分两层：

**D1.1 每日 token 趋势（主折线）** —— 按 `messages.timestamp` 聚合 `token_count`：
```sql
SELECT DATE(m.timestamp, 'unixepoch') AS d, SUM(m.token_count) AS total_tokens, COUNT(*) AS msgs
FROM messages m
WHERE m.timestamp > ?
GROUP BY d
ORDER BY d;
```
这给出精确的"每日实际产生的 token"折线（不分 input/output，但颗粒度准）。

**D1.2 Input/Output 拆分（次级切片）** —— 按 `sessions.started_at` 聚合：
```sql
SELECT DATE(s.started_at, 'unixepoch') AS d, s.model,
       SUM(s.input_tokens) AS input, SUM(s.output_tokens) AS output,
       SUM(s.cache_read_tokens) AS cache_read, SUM(s.reasoning_tokens) AS reasoning,
       SUM(s.estimated_cost_usd) AS est_cost
FROM sessions s
WHERE s.started_at > ?
GROUP BY d, s.model;
```
注意：是按 session **起始日**归集；跨日 session 的 token 全部计入起始日。对单轮 / 短 session 准确，对长 session 略偏前。UI 上要标注"按 session 起始日归集"。

**D1.3 按 model 切片** —— JOIN：
```sql
SELECT s.model, SUM(s.input_tokens) AS input, SUM(s.output_tokens) AS output,
       SUM(s.message_count) AS msgs, SUM(s.estimated_cost_usd) AS cost
FROM sessions s
WHERE s.started_at > ?
GROUP BY s.model
ORDER BY (input + output) DESC;
```

**D1.4 响应时间与热力图** —— 都基于 `messages.timestamp` + `messages.role`，不受 token 拆分缺失影响。

**为什么不扫文件**：
- `state.db` 已是权威来源（`state_sync.py` 保证 webui 会话也同步进来）
- SQL 聚合比扫 JSONL 快 10~100x
- 避免重复定义"usage 字段在哪一行"的解析逻辑
- hermes 的 message 表字段比 OpenClaw JSONL 更干净（已去重、已规范化）

### D2. 引擎 port 方式 —— 手工改写为 vanilla JS

OpenClaw 像素引擎约 3000 行 TypeScript（`lib/pixel-office/*.ts` + `app/pixel-office/page.tsx`）。三种选择：

| 方案 | 工作量 | 后果 |
|---|---|---|
| A. 手工改写为 vanilla JS | 3–4 天 | 保持 hermes 零构建原则；长期可维护 |
| B. 引入 esbuild/tsc 产出 bundle | 1–2 天初装 + 长期维护 | 破坏"零构建"；引入 node 依赖到 python 项目 |
| C. iframe 挂 Next 子站 | 半天 | 两个服务两套 auth；运维噩梦 |

**选 A**。改写 5 个核心模块到 `static/pixel/`：`engine/state.js` / `engine/characters.js` / `engine/game-loop.js` / `engine/renderer.js` / `engine/tile-map.js`；sprites 保持 base64 常量照搬到 `static/pixel/sprites.js`；桥接层 `static/pixel/surface-bridge.js` 是**重写**（不是 port）。

TS → JS 转换规则：去掉所有类型注解、`interface`/`type`、`as` 断言；`enum` 改常量对象；`class` 保留；`import`/`export` 改为 `<script>` 加载顺序。

### D3. 角色语义 —— 按 surface 而非按 session / agent

OpenClaw 一个 agent = 一个角色。hermes 单 agent，需要重新定义"谁是人"。

- **选中：按 surface** —— webui、telegram、discord、slack、cron、cli 各一个固定角色。有活跃 session 就在工位，超过阈值闲置就起身走动。符合 hermes"一个我多入口"的产品定位，角色数稳定（≤8），视觉不拥挤。
- 放弃：按 session —— 数量膨胀到几十个，办公室会挤爆
- 放弃：按 profile —— 多 profile 用户占比低，覆盖面窄

`surface` 的枚举值以 `state.db` 的 `sessions.source` 列实际值为准——**每次构建快照时对 active profile 的 DB 执行 `SELECT DISTINCT source FROM sessions`**，不在启动时固定。理由：不同 profile 的 source 集合可能完全不同（比如 profile A 只用过 cli，profile B 有 telegram+webui），启动时固定枚举会在 profile 切换后漏 surface 或残留旧 surface。动态查询成本可忽略：`sessions` 有 `idx_sessions_source` 索引，DISTINCT 查询 O(log n)，且与 D6 的 2 秒快照缓存叠加后每 profile 只实际查一次/2s。

**2026-04-17 验证**：当前 DB 仅观察到 `cli` 和 `weixin`。前端图标字典预置以下已知 surface：`cli / webui / weixin / telegram / discord / slack / signal / whatsapp / sms / email / cron`。未在字典中的归 `other` 卡片，label 仍显示原始 source 字符串。

### D4. 实时通道 —— 复用现有 SSE

已有 `/api/sessions/gateway/stream`（SSE）由 `gateway_watcher` 5s 推送。新增 `/api/agent-activity/stream` 作为**独立 endpoint 但共享 gateway_watcher 的轮询结果**——通过 `GatewayWatcher.subscribe()` 的 queue 机制拿到 `sessions_changed` 事件，本地 agent_activity 模块再按 surface 聚合产出 `snapshot` / `delta` 事件。不改现有 `/api/sessions/gateway/stream` 的事件格式。前端 Pixel Office 和 Surface Dashboard 共用同一个 EventSource。

空闲状态推导（仅基于 `MAX(messages.timestamp)` per surface，不依赖 hermes-agent 内部状态）：
- `working`: 该 surface 最近 60 秒内 `state.db` 有新 message
- `waiting`: 该 surface 最近一条 message 在 60–300s 前
- `idle`: 该 surface 最近一条 message 在 300s – 24h 前
- `offline`: 该 surface 超过 24h 无 message，或该 profile 的 `sessions.source` 枚举里根本不存在

⚠️ **重要**：这是"数据库视角"的状态，不是"agent 真实工作视角"。例如 telegram surface 此刻正在处理一条慢工具调用（webui 不可见），从 state.db 看可能是 `waiting`。这个不准确我们接受——文案上明确写 "based on message activity"，不使用 "the agent is currently doing X" 这种暗示运行时感知的措辞。真正的运行时状态采集需要 agent↔webui IPC，属 D9 范围。

### D5. 图表渲染 —— Inline SVG，不引第三方库

OpenClaw 用 SVG 自绘折线。hermes 已有 inline SVG 习惯（`index.html` 的 icons）。三条 chart 足够：折线（token 日趋势）、柱状（响应时间分桶）、热力图（7×24 格）。手写不超过 300 行 JS，避免 Chart.js/D3 依赖。暗色/亮色用 CSS 变量 `--accent`/`--muted` 自动切换。

### D6. 缓存策略

- Stats API：30s 内存 TTL（对齐 OpenClaw）；key 为 profile hermes_home path
- agent-activity：2s TTL（比 gateway_watcher 的 5s 更激进，因为像素动画需要流畅；缓存 key 同上）
- Sprites：放 `static/pixel/sprites.js` 作为 base64 常量，浏览器 HTTP 缓存自然命中

### D7. 前端面板集成

hermes 已有 sidebar nav-tab 模式（Chat/Tasks/Skills/Memory/Spaces/Profiles/Todos）。新增 3 个 tab：Insights / Surfaces / Pixel。
- Insights 和 Surfaces 作为 sidebar panel + 右侧主视图（对齐现有 Memory/Todos 模式）
- Pixel Office 因需要大画布，作为**主视图全屏接管**（隐藏 chat 消息区），点击 tab 切换；退出按钮回到 Chat
- i18n 新增 `tab_insights` / `tab_surfaces` / `tab_pixel` 等键

### D8. 安全 / 权限

- 所有新端点经现有 `require_auth` 装饰器（承继 session cookie）
- `/api/stats/*` 和 `/api/surfaces` 走标准 GET，自动复用 CSRF 不需要（GET 无 CSRF）
- 数据 redaction：复用 `redact_session_data` / `_redact_text`，若 surface 展示 session 标题需脱敏
- Profile 隔离：所有 DB path 经 `api.profiles.get_active_hermes_home()`，切 profile 后 cache 失效（mtime check）

### D9. 运行时真相源边界（本 change 不做的事）

hermes 的运行时分两个进程：
- **hermes-agent** —— 真正执行模型调用、工具调用、收发 telegram/discord 消息的长进程
- **hermes-webui** —— 本项目，HTTP 服务 + 浏览器 UI

两个进程目前**只通过 `state.db` 文件共享数据**。这意味着从 webui 侧能看到的 "真相" 只有：
- 已经写入 `state.db` 的 session 元数据和历史 message（append-only，带时间戳）
- 本 webui 进程内的 `SESSIONS` LRU 缓存（**仅 WebUI 会话**的临时状态，如 `active_stream_id` / `pending_user_message`）—— 不覆盖 telegram/discord 等 gateway surface

webui **无法**从现有通道可靠获知：
- telegram / discord surface 当前是否正在处理一条消息（agent 进程内部的 tool-call 栈）
- 任何 surface 的 `current_tool`（哪个工具在跑）
- `pending_approval_count`（approval 也只在 webui 进程内部）
- 其他 surface 的 `is_waiting_for_user_input`

因此本 change 的 spec / 前端文案明确：
- **仅使用"数据库视角"的状态**（基于 message timestamp + source 聚合）
- 不承诺 `current_tool` / `pending_count` 等运行时字段
- Pixel Office 的角色动作（type / walk / sit）只由 surface.state（working/waiting/idle/offline）决定，**不**依赖 current_tool
- 未来补 agent↔webui IPC（比如 agent 通过 UNIX socket 或 state.db 新表写入 heartbeat/current_tool）属另一个 change

### D10. "点击 Surface 卡片"的交互范围收敛

proposal 初稿写"点击卡片跳到 Chat 并用 filterSessions 过滤该 surface"。核实发现：
- `filterSessions()`（`static/sessions.js:353`）只按标题 + 消息内容关键字搜索，无 source 筛选能力
- `/api/sessions`（`api/routes.py:554`）合并 webui + cli 两路；webui session 没有统一的 `source` 字段；cli session 暴露的是 `source_tag`（`api/models.py:320`）
- 要按 source 筛选 session 列表，需要（a）统一 `/api/sessions` 的 source 字段（b）扩展 `filterSessions` 支持 source 过滤——**两者本身是一个独立 change**

本 change 范围收敛：点击卡片**不跳转**，而是**就地展开**该 surface 的最近 N 条 session（调用 `/api/surfaces?source=telegram&expand=1` 返回 session 子集，前端在卡片内渲染折叠详情）。保持本 change 自包含、不改 `/api/sessions`。future change 再做"按 surface 筛选 session 列表"的整合。

## Risks / Trade-offs

| Risk | Mitigation |
|---|---|
| `messages` 表 token 颗粒度只有 `token_count` 合并值（已验证） | 主趋势走 messages.token_count（日粒度准确），input/output 拆分走 sessions 级（见 D1.1/D1.2）；UI 上分两张图标注来源 |
| `sessions.started_at` 日聚合对长 session 偏前 | 文案明确"按 session 起始日归集"；后续 change 可补 `messages.input_tokens/output_tokens` 列做精确日拆分 |
| Pixel Office 在低端设备掉帧 | rAF 自适应降频 + 后台 tab 暂停 + canvas 分辨率按 `devicePixelRatio` 但 cap 在 2x |
| 引擎手工 port 引入 bug | 每个引擎模块配 1–2 个单元测试（DOM-less，用 jsdom 或纯逻辑）；port 时保持函数签名一致便于对比 |
| 30s 缓存可能看到过时数据 | Insights 面板 header 显示 `updated_at`；用户可手动点刷新按钮强制绕过缓存（`?refresh=1`） |
| sprite base64 太大撑爆 HTML | Sprites 放独立 `.js` 文件加载，首屏不 inline；仅 Pixel tab 打开时按需 `<script>` 注入 |
| Surface Dashboard 的 source 枚举不稳定（将来新增 slack/sms） | 后端 distinct 查询动态建立；未知值归 `other` 角色；前端字典失败时 fallback 到 source 字符串 |
| OpenClaw 代码的许可证（参考移植）| OpenClaw 是 MIT；我们不是"修改+再发布"而是"重新实现"，行为层借鉴但代码独立编写，许可兼容 |
| 用户误以为 Pixel Office 的 "working 角色" 代表 agent 正在执行工具 | 文案明确 "based on recent messages"；角色 tooltip 显示 "Last message: 42s ago"；不用 "Agent is currently running Bash" 这类暗示运行时感知的语言（见 D9） |
| `/api/surfaces` 的 `active_webui_sessions` 字段只反映 webui 进程缓存，不反映 telegram/discord | 字段名明确加 `webui_` 前缀；其他 surface 的 `active_sessions` 字段不存在（而非返回 0，避免误导） |

## Migration Plan

不涉及数据迁移（纯只读 + 新增 UI）。部署步骤：
1. 合并后端新端点（仍未被 UI 调用，对现状无影响）
2. 合并 sidebar tab + panel HTML（tab 存在但空）
3. 合并 Insights → 先发 Insights 单独可用
4. 合并 Surface Dashboard → 自然复用 gateway_watcher
5. 合并 Pixel Office 引擎 + 桥接 → 娱乐化视图最后上

回滚策略：每阶段独立 PR；若某阶段出问题只回该 PR，前序 PR 仍独立可用。所有新端点在 routes 分发表末尾追加，移除不影响其他路由。

## Open Questions

1. ~~`messages` 表 token 列~~ —— **已验证 (2026-04-17)**：无 input/output 拆分，只有合并 `token_count`；input/output 来自 `sessions` 表。聚合策略已在 D1.1–D1.4 定稿。
2. ~~`sessions.source` 实际枚举~~ —— **已验证 (2026-04-17)**：当前 DB 仅 `cli` + `weixin`。字典预置 11 个已知 surface，未知归 `other`。
3. Pixel Office 的角色 sprite 是直接复用 OpenClaw 的 base64 精灵（需验证 MIT 兼容后代码注释保留出处）还是另行委托设计？建议 P1 先复用 OpenClaw 精灵 + 注明出处；P2 考虑替换自制
4. 移动端要不要做 Pixel Office？建议首版桌面 only（画布最小 640px），移动端自动 fallback 到 Surface Dashboard 文字卡片
