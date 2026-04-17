## 1. 前置验证与脚手架

- [x] 1.1 ~~运行 `PRAGMA table_info(messages)` 确认 token 列~~ **已于 2026-04-17 验证**：`messages` 只有合并 `token_count`；input/output 拆分在 `sessions` 表。聚合策略已按 design D1.1–D1.4 定稿
- [x] 1.2 ~~运行 `SELECT DISTINCT source FROM sessions` 确认 surface 枚举~~ **已于 2026-04-17 验证**：当前 DB 仅 `cli` + `weixin`；前端图标字典预置 11 个 surface（`cli / webui / weixin / telegram / discord / slack / signal / whatsapp / sms / email / cron`）
- [x] 1.3 在 `api/` 新建 `api/stats.py`、`api/agent_activity.py` 骨架 —— docstring + NotImplementedError/501 handler 存根
- [x] 1.4 在 `api/routes.py` 的 `handle_get` 末尾预留 8 个新端点分发（5 stats + agent-activity + agent-activity/stream + surfaces），全部返回 501
- [x] 1.5 新增 `static/pixel/` 目录与 8 个 JS 文件（engine/{state,characters,game-loop,renderer,tile-map}.js + sprites.js + surface-bridge.js + pixel-office.js），每个含 OpenClaw/MIT 出处注释
- [x] 1.6 `tests/test_stats_endpoints.py` / `test_agent_activity.py` / `test_pixel_office_frontend.py` —— 10 个 smoke 测试全通过（import + handler 存在 + route 注册 + 文案 lint）

## 2. 后端：Stats 聚合 API

- [ ] 2.1 在 `api/stats.py` 实现 `_open_state_db_readonly()` 工具函数（使用 `mode=ro` URI 连接）
- [ ] 2.2 实现 `_get_profile_db_path()` 复用 `api.profiles.get_active_hermes_home()`
- [ ] 2.3 实现 30 秒 TTL 内存缓存装饰器 `@cached(ttl=30, key=profile_path)`
- [ ] 2.4 实现 `handle_stats_summary(handler, parsed)`：`sessions` SUM input/output/cost + `MAX(messages.timestamp)` + webui `SESSIONS` LRU 计数作为 `active_webui_sessions`（不含其他 surface 的活跃会话数，因为 webui 无从判断）
- [ ] 2.5 实现 `handle_stats_timeseries(handler, parsed)`：`source=total` 走 `messages.token_count` 按 `DATE(timestamp, 'unixepoch')` 聚合；`source=split` 走 `sessions` 按 `DATE(started_at, 'unixepoch')` 聚合；支持 granularity=day|week|month，window clamp 到 365
- [ ] 2.6 实现 `handle_stats_response_time(handler, parsed)`：同 session 内 user→assistant 连续消息时延分桶（0–1s / 1–3s / 3–10s / 10–30s / 30s+），过滤 >600s
- [ ] 2.7 实现 `handle_stats_heatmap(handler, parsed)`：7×24 bucket，用 `strftime('%w', timestamp, 'unixepoch')` + `strftime('%H', timestamp, 'unixepoch')` 分组
- [ ] 2.8 实现 `handle_stats_models(handler, parsed)`：`sessions` GROUP BY `model`，返回 `input_tokens / output_tokens / message_count / estimated_cost_usd / pct`
- [ ] 2.9 实现 `?refresh=1` 绕过缓存的逻辑；在响应头加 `X-Cache: HIT|MISS`
- [ ] 2.10 在 `api/routes.py` 的 GET dispatch 把 5 个 `/api/stats/*` 路由绑定到 2.4–2.8
- [ ] 2.11 为以上 5 个端点写 pytest：未鉴权 401、正常返回、缓存命中、强制刷新、窗口夹紧、空 DB 返回空态结构

## 3. 后端：Agent Activity API

- [ ] 3.1 在 `api/agent_activity.py` 实现 `build_surface_snapshot(db_path) -> dict`（纯函数，方便单测）：每次调用内部执行 `SELECT DISTINCT source FROM sessions` 动态建立 surface 枚举（不在模块加载时固定）
- [ ] 3.2 实现状态推导规则函数 `derive_state(last_msg_ts, now_ts) -> str`：**仅基于时间戳**返回 working/waiting/idle/offline（对齐 agent-activity-api spec；不引入"has_active_session"参数，因为 webui 无法可靠判断非 webui surface 的活跃会话）
- [ ] 3.3 实现 2 秒 TTL 缓存；key 按 active profile hermes_home 路径；profile 切换后旧 key 自动失效
- [ ] 3.4 实现 `handle_agent_activity(handler, parsed)`：返回 `/api/agent-activity` 快照 JSON，结构严格对齐 spec（`{surfaces, generated_at, profile}`，`active_webui_sessions` 只出现在 `source=="webui"` 条目）
- [ ] 3.5 通过 `GatewayWatcher.subscribe()` queue 获取 `sessions_changed` 事件并本地重聚合为 surface snapshot；**不新起额外轮询线程**；验证 `api/gateway_watcher.py` 的 `subscribe`/`unsubscribe` 机制满足需求
- [ ] 3.6 实现 `handle_agent_activity_stream(handler, parsed)`：SSE 长连接，初始发 `event: snapshot`，后续 `gateway_watcher` 事件触发时发 `event: delta`（只含状态变化的 surface 子集），无事件时每 30s 发 `event: heartbeat`
- [ ] 3.7 profile 切换时关闭旧 SSE 连接：每次循环检查 `api.profiles.get_active_hermes_home()` 是否变化；变化时往响应写结束帧并跳出循环
- [ ] 3.8 在 `api/routes.py` 绑定 `/api/agent-activity` 与 `/api/agent-activity/stream`
- [ ] 3.9 为端点写 pytest：未鉴权 401；快照结构正确；`active_webui_sessions` 仅在 webui 条目；`current_tool`/`pending_count` 字段**不存在** on contract test；SSE 首事件为 snapshot；heartbeat 间隔；profile 切换断开；空 DB 返回 `surfaces: []`
- [ ] 3.10 前端文案 lint：在 `static/surfaces.js` / `static/pixel/*.js` 的字符串常量里禁止出现 `"currently running"` / `"waiting for your reply"` 等暗示运行时感知的措辞（可在测试里 grep 验证）

## 4. 后端：Surfaces 聚合 API

- [ ] 4.1 在 `api/agent_activity.py` 增加 `build_surfaces_cards(db_path) -> list`（含 `source / state / last_active_ts / message_count_24h / tokens_24h`；仅 webui 条目额外注入 `active_webui_sessions`，从 webui 进程内 `SESSIONS` LRU 计数）
- [ ] 4.2 实现 `handle_surfaces(handler, parsed)`，挂 `/api/surfaces`；**额外支持** `?source=<src>&expand=1` 返回 `{sessions: [{session_id, title, model, last_activity, message_count}]}`（最近 5 条，按 last_activity DESC，title 经 `_redact_text` 脱敏）
- [ ] 4.3 2 秒 TTL 缓存；`?source=*&expand=1` 的展开查询按 source 维度单独缓存
- [ ] 4.4 未知 source 的 expand 查询返回 `{sessions: []}`（非 404）
- [ ] 4.5 pytest：未鉴权 401、空 DB 返回 `surfaces: []`、正常返回结构、`active_webui_sessions` 只在 webui 条目、expand 查询脱敏、未知 source expand 返回空数组

## 5. 前端：sidebar 三个新 tab 与 i18n

- [ ] 5.1 `static/index.html`：在 sidebar-nav 内追加 `Insights` / `Surfaces` / `Pixel` 三个 `button.nav-tab` 和对应 `panel-view` 容器
- [ ] 5.2 `static/i18n.js`：为 en/zh/de/zh-Hant 添加键 `tab_insights` / `tab_surfaces` / `tab_pixel` 以及各子区块文案；字符串严格避免 "currently running X" / "waiting for reply" 之类暗示运行时感知的措辞
- [ ] 5.3 **`static/panels.js`**（不是 ui.js）：`switchPanel()` 定义在此（`static/panels.js:4`）；在其分支结构里新增 `insights` / `surfaces` / `pixel` 三个 case，每个 case 调用对应 module 的 `onShow()` / `onHide()`
- [ ] 5.4 `static/style.css`：追加 Insights 折线/柱状/热力图用 CSS 变量，以及 Surface 卡片网格 + 就地展开抽屉样式

## 6. 前端：Insights 面板

- [ ] 6.1 `static/insights.js`：实现 `renderInsightsPanel()` 入口，首次打开时拉取 `/api/stats/summary` 填充顶部卡片
- [ ] 6.2 实现 `renderTokenTimeseries(container, data)`：主折线（单线 total token from messages，area fill）+ 次级堆叠柱状（input/output/cache_read/reasoning from sessions），两图上下布局并各自标注数据来源注脚；支持粒度切换按钮
- [ ] 6.3 实现 `renderResponseTimeBuckets(container, data)`：纯 SVG 柱状图 + 窗口切换（7/30 天）
- [ ] 6.4 实现 `renderHeatmap(container, data)`：SVG 7×24 矩形网格，tooltip 显示 "周X HH:00 — N messages"
- [ ] 6.5 实现 `renderModelsTable(container, data)`：HTML 表格，按 total token 降序
- [ ] 6.6 空态：任一 API 返回空数据时显示友好提示（i18n key `insights_empty`）
- [ ] 6.7 手动刷新按钮：`?refresh=1` 绕过缓存
- [ ] 6.8 Playwright / 人工 QA：跑起 server，手动验证四个子区块渲染、粒度切换、空数据、亮暗主题

## 7. 前端：Surface Dashboard 面板

- [ ] 7.1 `static/surfaces.js`：实现 `renderSurfacesPanel()`，首次打开拉取 `/api/surfaces`
- [ ] 7.2 实现卡片模板：icon / name / state 灯 / last activity（"Last message Ns ago"） / 24h msgs / 24h tokens；**仅 webui 卡**额外渲染 "N active sessions"，其他 surface 不渲染该字段（避免误导）
- [ ] 7.3 Icon 字典（cli / webui / weixin / telegram / discord / slack / signal / whatsapp / sms / email / cron）定义在 `surfaces.js` 顶部，未识别归 other 卡片但 label 保留原始 source 字符串
- [ ] 7.4 订阅 `/api/agent-activity/stream` SSE，解析 `snapshot`/`delta`/`heartbeat` 事件增量更新卡片
- [ ] 7.5 SSE 断线 5 秒重连
- [ ] 7.6 **点击卡片就地展开**（不跳转）：异步拉取 `/api/surfaces?source=<src>&expand=1`，在卡片下方渲染折叠抽屉显示该 surface 最近 5 条 session 摘要；再次点击折叠；30s 内复用结果；**不调用** `switchPanel('chat')`，**不调用** `filterSessions()`
- [ ] 7.7 < 640px 视口切换单列布局（纯 CSS 媒体查询）

## 8. 前端：Pixel Office 引擎移植

- [ ] 8.1 `static/pixel/engine/tile-map.js`：从 OpenClaw `lib/pixel-office/layout/tileMap.ts` 改写为 vanilla JS，保留 BFS 寻路 API
- [ ] 8.2 `static/pixel/engine/state.js`：从 `engine/officeState.ts` 改写；提供 addAgent / removeAgent / setAgentState(charId, 'working'|'waiting'|'idle') / showClockBubble 等方法；**不移植** `setAgentTool()`（因为本 change 无 current_tool 数据源）
- [ ] 8.3 `static/pixel/engine/characters.js`：从 `engine/characters.ts` 改写状态机（IDLE/WALK/TYPE）
- [ ] 8.4 `static/pixel/engine/renderer.js`：从 `engine/renderer.ts` 改写 Canvas 2D 绘制（z-sort、精灵帧、label、气泡）
- [ ] 8.5 `static/pixel/engine/game-loop.js`：从 `engine/gameLoop.ts` 改写 rAF 循环；加 `document.visibilityState` 暂停/恢复
- [ ] 8.6 `static/pixel/sprites.js`：把 OpenClaw 需要的精灵 base64 常量搬运并合规注释出处（MIT）；Furniture 催化剂最小子集（desk、chair、door）
- [ ] 8.7 引擎模块单测（pytest 用 Node subprocess 跑或纯 JS headless jsdom）：BFS 寻路、状态机转换、renderer 纯函数分支

## 9. 前端：Pixel Office 桥接与面板

- [ ] 9.1 `static/pixel/surface-bridge.js`：实现 `syncSurfacesToOffice(surfaces, office, surfaceIdMap)`（按 surface 而非 agent，重写自 `agentBridge.ts`）；**仅映射 `state` 字段**（working/waiting/idle/offline），不读 `current_tool` / subagent 列表
- [ ] 9.2 实现 "超过 8 surface 合并 +N 徽章角色" 逻辑
- [ ] 9.3 `static/pixel/pixel-office.js`：面板入口，onShow 时初始化 canvas / state / loop，订阅 `/api/agent-activity/stream`；waiting 气泡 tooltip 文案 "Last message Ns ago"（复用 state 与 `last_active_ts` 计算）
- [ ] 9.4 onHide 时销毁 canvas、停 rAF、关闭 EventSource
- [ ] 9.5 顶部工具栏：返回按钮、缩放、暂停/恢复（**不含** 编辑布局按钮——明确在 spec 中排除）
- [ ] 9.6 移动端（<640px）检测：fallback 到 Surface Dashboard，显示提示 toast
- [ ] 9.7 人工 QA：多 surface 场景（开 telegram + webui 并发话），观察角色打字 / 巡游 / 时钟气泡过渡；后台 tab 暂停；SSE 断连重连；profile 切换后旧 surface 消失、新 surface 自动出现

## 10. 文档与兼容性

- [ ] 10.1 更新 `README.md` 在 Features 章节添加 Insights / Surfaces / Pixel Office 三条，附截图
- [ ] 10.2 更新 `CHANGELOG.md`：本次 change 的用户侧变更摘要
- [ ] 10.3 确认 `static/boot.js` / `static/ui.js` 的老逻辑不受影响（现有面板切换、i18n、主题切换回归测试）
- [ ] 10.4 更新 `ARCHITECTURE.md`：新增"Dashboard / Pixel Office 模块"小节，引用本 change 的 design.md
- [ ] 10.5 新增 follow-up change 备忘：未来补 `messages.input_tokens/output_tokens` 列可让日粒度精确拆分；当前 change 已通过双图组合规避

## 11. 验收与合并

- [ ] 11.1 全量 `pytest tests/` 通过；新增测试至少覆盖：stats 5 端点、agent-activity 快照 + SSE、surfaces 端点
- [ ] 11.2 手动走查 spec 中每个 Scenario（四份 spec 共约 30 个场景）
- [ ] 11.3 打开一个 PR，PR 描述引用 `proposal.md`；标题遵循 hermes 现有 conventional commit 风格
- [ ] 11.4 通过 CI 所有检查
- [ ] 11.5 合并并运行 `openspec archive add-dashboards-and-pixel-office` 将 change 归档到 specs 目录
