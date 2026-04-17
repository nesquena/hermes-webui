## 1. 前置验证与脚手架

- [x] 1.1 ~~运行 `PRAGMA table_info(messages)` 确认 token 列~~ **已于 2026-04-17 验证**：`messages` 只有合并 `token_count`；input/output 拆分在 `sessions` 表。聚合策略已按 design D1.1–D1.4 定稿
- [x] 1.2 ~~运行 `SELECT DISTINCT source FROM sessions` 确认 surface 枚举~~ **已于 2026-04-17 验证**：当前 DB 仅 `cli` + `weixin`；前端图标字典预置 11 个 surface（`cli / webui / weixin / telegram / discord / slack / signal / whatsapp / sms / email / cron`）
- [ ] 1.3 在 `api/` 新建空文件 `api/stats.py`、`api/agent_activity.py`，先放入模块 docstring 和空函数骨架
- [ ] 1.4 在 `api/routes.py` 的 `handle_get` 分发表末尾为 7 个新端点预留分支（当前返回 501），仅用于通路连通测试
- [ ] 1.5 新增 `static/pixel/` 目录与空文件 `engine/state.js` / `engine/characters.js` / `engine/game-loop.js` / `engine/renderer.js` / `engine/tile-map.js` / `sprites.js` / `surface-bridge.js` / `pixel-office.js`
- [ ] 1.6 在 `tests/` 下新增 `test_stats_endpoints.py` / `test_agent_activity.py` / `test_pixel_office_frontend.py` 空壳，加 pytest import-level 冒烟

## 2. 前置变更（源码审查反馈引入 — 支持 Surface Dashboard 的点击跳转）

- [ ] 2.0a **统一 source 字段**：修改 `api/routes.py:554` 的 `/api/sessions` 响应路径，让 WebUI 会话也携带 `source='webui'` 字段；将 CLI/agent 会话现有的 `source_tag` 镜像为统一 `source`（保留 `source_tag` 向后兼容）。Unit test 断言所有返回 session 都有 `source` 字段
- [ ] 2.0b **扩展 filterSessions**：在 `static/sessions.js:353` 的 `filterSessions()` 增加可选参数 `sourceFilter`（从 `_allSessions` 先按 source 预过滤再执行现有文本搜索）；不改变既有文本搜索行为。新增 JS 单测或手工 QA 验证 `filterSessions({sourceFilter: 'weixin'})` 只返回 weixin 会话
- [ ] 2.0c 若 2.0a / 2.0b 任一推迟，则 7.6 必须采用降级路径（Toast 提示，不跳转）——在 PR 描述标注选择

## 3. 后端：Stats 聚合 API

- [ ] 3.1 在 `api/stats.py` 实现 `_open_state_db_readonly()` 工具函数（使用 `mode=ro` URI 连接）
- [ ] 3.2 实现 `_get_profile_db_path()` 复用 `api.profiles.get_active_hermes_home()`
- [ ] 3.3 实现 30 秒 TTL 内存缓存装饰器 `@cached(ttl=30, key=profile_path)`
- [ ] 3.4 实现 `handle_stats_summary(handler, parsed)`：`sessions` SUM input/output/cost + `MAX(messages.timestamp)`；`active_sessions` 来自 `sessions WHERE ended_at IS NULL AND MAX(m.timestamp) > now-1800`（不得用 SESSIONS LRU 推断非 webui surface）
- [ ] 3.5 实现 `handle_stats_timeseries(handler, parsed)`：`source=total` 走 `messages.token_count` 按 `DATE(timestamp, 'unixepoch')` 聚合；`source=split` 走 `sessions` 按 `DATE(started_at, 'unixepoch')` 聚合；支持 granularity=day|week|month，window clamp 到 365
- [ ] 3.6 实现 `handle_stats_response_time(handler, parsed)`：同 session 内 user→assistant 连续消息时延分桶（0–1s / 1–3s / 3–10s / 10–30s / 30s+），过滤 >600s
- [ ] 3.7 实现 `handle_stats_heatmap(handler, parsed)`：7×24 bucket，用 `strftime('%w', timestamp, 'unixepoch')` + `strftime('%H', timestamp, 'unixepoch')` 分组
- [ ] 3.8 实现 `handle_stats_models(handler, parsed)`：`sessions` GROUP BY `model`，返回 `input_tokens / output_tokens / message_count / estimated_cost_usd / pct`
- [ ] 3.9 实现 `?refresh=1` 绕过缓存的逻辑；在响应头加 `X-Cache: HIT|MISS`
- [ ] 3.10 在 `api/routes.py` 的 GET dispatch 把 5 个 `/api/stats/*` 路由绑定到 3.4–3.8
- [ ] 3.11 为以上 5 个端点写 pytest：未鉴权 401、正常返回、缓存命中、强制刷新、窗口夹紧、空 DB 返回空态结构

## 4. 后端：Agent Activity API（严格遵循数据能力边界 — 见 design D9）

- [ ] 4.1 在 `api/agent_activity.py` 实现 `build_surface_snapshot(db_path) -> dict`（纯函数，只读 `state.db`，不依赖 `SESSIONS` LRU）。输出字段严格限定为 `{source, state, last_active, message_count_24h, active_session_count, is_webui_current}`——**不得** 输出 `current_tool` 或 `pending_count`
- [ ] 4.2 实现 `list_active_sources(db_path)`：每次调用都 `SELECT DISTINCT source FROM sessions`，按 profile path 做 2s TTL 缓存；profile 切换触发缓存失效（不得在启动时一次性固定枚举）
- [ ] 4.3 实现状态推导函数 `derive_state(last_msg_ts, has_active_session, now_ts) -> str`：`active_session = sessions.ended_at IS NULL AND MAX(m.timestamp) > now-1800`；返回 working/waiting/idle/offline
- [ ] 4.4 实现 2 秒 TTL 内存缓存（按 profile hermes_home 路径作 key）
- [ ] 4.5 实现 `handle_agent_activity(handler, parsed)`：返回 `/api/agent-activity` 快照 JSON；支持 `?include=last_tool` 可选字段（来自 `messages.tool_name` 最近一条非空记录）
- [ ] 4.6 实现 `handle_agent_activity_stream(handler, parsed)`：SSE 长连接，初始发 `snapshot` 事件，后续发 `delta` 事件，每 30s 发 `heartbeat`
- [ ] 4.7 profile 切换时关闭所有旧 SSE 连接（在 SSE 循环内每 tick 检查 `get_active_hermes_home()` 是否变化；变则关闭连接）
- [ ] 4.8 **独立 endpoint、不改动 `gateway_watcher` 现有契约**：在 `api/agent_activity.py` 中追加 webui surface 的聚合查询（因为 `gateway_watcher` 的 WHERE 过滤了 webui）
- [ ] 4.9 在 `api/routes.py` 绑定 `/api/agent-activity` 与 `/api/agent-activity/stream`
- [ ] 4.10 为端点写 pytest：未鉴权 401；快照结构正确（响应体**无** `current_tool` 字段）；SSE 首事件为 snapshot；heartbeat 间隔；profile 切换断开并重新查询 distinct source

## 5. 后端：Surfaces 聚合 API

- [ ] 5.1 在 `api/agent_activity.py` 增加 `build_surfaces_cards(db_path) -> list`（含 `active_session_count / messages_24h / tokens_24h / icon_key`）——全部来自 `state.db` SELECT，不从 `SESSIONS` LRU 推断
- [ ] 5.2 实现 `handle_surfaces(handler, parsed)`，挂 `/api/surfaces`
- [ ] 5.3 2 秒 TTL 缓存；Surfaces 与 agent-activity 共享缓存 key 避免重复查询
- [ ] 5.4 pytest：未鉴权 401、空 DB 返回空态、正常返回结构、响应体**无** `current_tool` 字段、响应中 `icon_key` 对已知 surface 映射正确、未知 source 的 `icon_key = 'other'`

## 6. 前端：sidebar 三个新 tab 与 i18n

- [ ] 6.1 `static/index.html`：在 sidebar-nav 内追加 `Insights` / `Surfaces` / `Pixel` 三个 `button.nav-tab` 和对应 `panel-view` 容器
- [ ] 6.2 `static/i18n.js`：为 en/zh/de/zh-Hant 添加键 `tab_insights` / `tab_surfaces` / `tab_pixel` 以及各子区块文案
- [ ] 6.3 **`static/panels.js`**（真正的 `switchPanel()` 定义位置，`panels.js:4`）：在 `switchPanel()` 的懒加载分支追加 `if (name === 'insights') renderInsightsPanel();` / `surfaces` / `pixel` 三个分支。原稿误写为 `static/ui.js` — 本任务已修正
- [ ] 6.4 `static/style.css`：追加 Insights 折线/柱状/热力图用 CSS 变量，以及 Surface 卡片网格样式

## 7. 前端：Insights 面板

- [ ] 7.1 `static/insights.js`：实现 `renderInsightsPanel()` 入口，首次打开时拉取 `/api/stats/summary` 填充顶部卡片
- [ ] 7.2 实现 `renderTokenTimeseries(container, data)`：主折线（单线 total token from messages，area fill）+ 次级堆叠柱状（input/output/cache_read/reasoning from sessions），两图上下布局并各自标注数据来源注脚；支持粒度切换按钮
- [ ] 7.3 实现 `renderResponseTimeBuckets(container, data)`：纯 SVG 柱状图 + 窗口切换（7/30 天）
- [ ] 7.4 实现 `renderHeatmap(container, data)`：SVG 7×24 矩形网格，tooltip 显示 "周X HH:00 — N messages"
- [ ] 7.5 实现 `renderModelsTable(container, data)`：HTML 表格，按 total token 降序
- [ ] 7.6 空态：任一 API 返回空数据时显示友好提示（i18n key `insights_empty`）
- [ ] 7.7 手动刷新按钮：`?refresh=1` 绕过缓存
- [ ] 7.8 Playwright / 人工 QA：跑起 server，手动验证四个子区块渲染、粒度切换、空数据、亮暗主题

## 8. 前端：Surface Dashboard 面板

- [ ] 8.1 `static/surfaces.js`：实现 `renderSurfacesPanel()`，首次打开拉取 `/api/surfaces`
- [ ] 8.2 实现卡片模板：icon / name / state 灯 / `active_session_count` / last activity / 24h msgs / 24h tokens；**不** 展示 current_tool
- [ ] 8.3 Icon 字典（cli / webui / weixin / telegram / discord / slack / signal / whatsapp / sms / email / cron）定义在 `surfaces.js` 顶部；优先用后端返回的 `icon_key` 映射；未识别 source 的 `icon_key=other` 但 label 保留原始 source 字符串
- [ ] 8.4 订阅 `/api/agent-activity/stream` SSE，解析 `snapshot`/`delta`/`heartbeat` 事件增量更新卡片
- [ ] 8.5 SSE 断线 5 秒重连
- [ ] 8.6 点击卡片跳转：**依赖 2.0a + 2.0b 前置任务**。若两者已完成，则调用 `switchPanel('chat')` + `filterSessions({sourceFilter: card.source})`；若任一推迟，则显示 Toast "Session filtering by surface coming soon" 并不跳转
- [ ] 8.7 < 640px 视口切换单列布局（纯 CSS 媒体查询）

## 9. 前端：Pixel Office 引擎移植

- [ ] 9.1 `static/pixel/engine/tile-map.js`：从 OpenClaw `lib/pixel-office/layout/tileMap.ts` 改写为 vanilla JS，保留 BFS 寻路 API
- [ ] 9.2 `static/pixel/engine/state.js`：从 `engine/officeState.ts` 改写；提供 addAgent / removeAgent / setAgentActive / showWaitingBubble / setLastTool 等方法。**注意**：不保留 `setAgentTool`（实时 tool）语义；改为 `setLastTool(name, timestamp)`，UI 渲染为"最近一次工具调用"
- [ ] 9.3 `static/pixel/engine/characters.js`：从 `engine/characters.ts` 改写状态机（IDLE/WALK/TYPE）
- [ ] 9.4 `static/pixel/engine/renderer.js`：从 `engine/renderer.ts` 改写 Canvas 2D 绘制（z-sort、精灵帧、label、气泡）
- [ ] 9.5 `static/pixel/engine/game-loop.js`：从 `engine/gameLoop.ts` 改写 rAF 循环；加 `document.visibilityState` 暂停/恢复
- [ ] 9.6 `static/pixel/sprites.js`：把 OpenClaw 需要的精灵 base64 常量搬运并合规注释出处（MIT）；Furniture 最小子集（desk、chair、door）
- [ ] 9.7 引擎模块单测（pytest 用 Node subprocess 跑或纯 JS headless jsdom）：BFS 寻路、状态机转换、renderer 纯函数分支

## 10. 前端：Pixel Office 桥接与面板

- [ ] 10.1 `static/pixel/surface-bridge.js`：实现 `syncSurfacesToOffice(surfaces, office, surfaceIdMap)`（按 surface 而非 agent，重写自 `agentBridge.ts`）。只消费 `{source, state, last_active, active_session_count, last_tool_name?}`——不得假设 `current_tool` 存在
- [ ] 10.2 实现 "超过 8 surface 合并 +N 徽章角色" 逻辑
- [ ] 10.3 `static/pixel/pixel-office.js`：面板入口，onShow 时初始化 canvas / state / loop，订阅 `/api/agent-activity/stream?include=last_tool`
- [ ] 10.4 onHide 时销毁 canvas、停 rAF、关闭 EventSource
- [ ] 10.5 顶部工具栏：返回按钮、缩放、暂停/恢复（**不含** 编辑布局按钮——明确在 spec 中排除）
- [ ] 10.6 移动端（<640px）检测：fallback 到 Surface Dashboard，显示提示 toast
- [ ] 10.7 人工 QA：多 surface 场景（开 telegram + webui 并发话），观察角色打字 / 巡游 / 气泡过渡；后台 tab 暂停；SSE 断连重连

## 11. 文档与兼容性

- [ ] 11.1 更新 `README.md` 在 Features 章节添加 Insights / Surfaces / Pixel Office 三条，附截图
- [ ] 11.2 更新 `CHANGELOG.md`：本次 change 的用户侧变更摘要，**明确声明** surface 的 `state` 基于 message timestamp 推导，不代表"实时在线"
- [ ] 11.3 确认 `static/boot.js` / `static/panels.js` 的老逻辑不受影响（现有面板切换、i18n、主题切换回归测试）
- [ ] 11.4 更新 `ARCHITECTURE.md`：新增"Dashboard / Pixel Office 模块"小节，引用本 change 的 design.md（含 D9 数据真相源）
- [ ] 11.5 新增 follow-up change 备忘：未来补 `messages.input_tokens/output_tokens` 列可让日粒度精确拆分；当前 change 已通过双图组合规避
- [ ] 11.6 新增 follow-up change 备忘：若需要真正的"实时 in-flight tool"展示，需另起 change 在 `hermes-agent` 侧增加 tool 生命周期事件广播通道（超本 change 范围）

## 12. 验收与合并

- [ ] 12.1 全量 `pytest tests/` 通过；新增测试至少覆盖：stats 5 端点、agent-activity 快照 + SSE、surfaces 端点、`/api/sessions` 统一 source 字段
- [ ] 12.2 手动走查 spec 中每个 Scenario（四份 spec 共约 35 个场景）
- [ ] 12.3 验证 response body 无 `current_tool` / `pending_count` 字段（防回归）
- [ ] 12.4 验证 profile 切换场景：切 profile 后 `/api/agent-activity` 重新查 distinct source，SSE 自动断连重连
- [ ] 12.5 打开一个 PR，PR 描述引用 `proposal.md`；标题遵循 hermes 现有 conventional commit 风格
- [ ] 12.6 通过 CI 所有检查
- [ ] 12.7 合并并运行 `openspec archive add-dashboards-and-pixel-office` 将 change 归档到 specs 目录
