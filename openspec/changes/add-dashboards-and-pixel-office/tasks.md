## 1. 前置验证与脚手架

- [x] 1.1 ~~运行 `PRAGMA table_info(messages)` 确认 token 列~~ **已于 2026-04-17 验证**：`messages` 只有合并 `token_count`；input/output 拆分在 `sessions` 表。聚合策略已按 design D1.1–D1.4 定稿
- [x] 1.2 ~~运行 `SELECT DISTINCT source FROM sessions` 确认 surface 枚举~~ **已于 2026-04-17 验证**：当前 DB 仅 `cli` + `weixin`；前端图标字典预置 11 个 surface（`cli / webui / weixin / telegram / discord / slack / signal / whatsapp / sms / email / cron`）
- [x] 1.3 在 `api/` 新建 `api/stats.py`、`api/agent_activity.py` 骨架 —— docstring + NotImplementedError/501 handler 存根
- [x] 1.4 在 `api/routes.py` 的 `handle_get` 末尾预留 8 个新端点分发（5 stats + agent-activity + agent-activity/stream + surfaces），全部返回 501
- [x] 1.5 新增 `static/pixel/` 目录与 8 个 JS 文件（engine/{state,characters,game-loop,renderer,tile-map}.js + sprites.js + surface-bridge.js + pixel-office.js），每个含 OpenClaw/MIT 出处注释
- [x] 1.6 `tests/test_stats_endpoints.py` / `test_agent_activity.py` / `test_pixel_office_frontend.py` —— 10 个 smoke 测试全通过（import + handler 存在 + route 注册 + 文案 lint）

## 2. 后端：Stats 聚合 API

- [x] 2.1 `_open_state_db_readonly(db_path)` using `file:...?mode=ro` URI; readonly verified by test
- [x] 2.2 `_get_profile_db_path()` resolves via `api.profiles.get_active_hermes_home()` with env fallback; returns `None` if state.db missing
- [x] 2.3 module-level `_CACHE` dict + `_cache_get/_cache_set/_cache_clear_all`; key includes profile DB path; TTL 30 s
- [x] 2.4 `handle_stats_summary` → `_build_summary`: sessions SUM + `MAX(messages.timestamp)` + webui SESSIONS count
- [x] 2.5 `handle_stats_timeseries` → `_build_timeseries`: `source=total` via messages.token_count, `source=split` via sessions; granularity day/week/month; window clamped to [1,365]
- [x] 2.6 `handle_stats_response_time` → `_build_response_time`: 5-bucket (`0-1s / 1-3s / 3-10s / 10-30s / 30s+`), filters `delta <= 0 or delta > 600`
- [x] 2.7 `handle_stats_heatmap` → `_build_heatmap`: 7×24 matrix via `strftime('%w'/%H, timestamp, 'unixepoch')`
- [x] 2.8 `handle_stats_models` → `_build_models`: sessions GROUP BY model, returns input/output/count/cost/pct
- [x] 2.9 `?refresh=1` bypasses `_cache_get`; response header `X-Cache: HIT|MISS` added in `_j_cached`
- [x] 2.10 routes dispatch already wired in stage 1 (501→live handlers; no routing change needed)
- [x] 2.11 `tests/test_stats_endpoints.py` — 19 tests: 3 scaffold + 9 pure builders (incl. empty-DB, over-10-min filter, window-clamp, granularity, model ranking) + 7 handler-level (401, cache HIT/MISS, refresh, empty-DB, window-clamp, 200, readonly-write rejected). All pass

## 3. 后端：Agent Activity API

- [x] 3.1 `build_surface_snapshot(db_path)` 纯函数：LEFT JOIN sessions×messages 每次 GROUP BY source（等价 DISTINCT source）；per-surface 的 `last_active_ts / message_count_24h / tokens_24h`；empty-DB 情况返回 `{surfaces: [], ...}`
- [x] 3.2 `derive_state(last_msg_ts, now_ts)`：`<60s→working / <300s→waiting / <86400s→idle / else→offline`；future-ts (clock skew) 视为 working
- [x] 3.3 独立 `_CACHE / _cache_get / _cache_set / _cache_clear_all`（2s TTL）；key 包含 profile DB 路径；profile 切换自动换 key
- [x] 3.4 `handle_agent_activity` → snapshot JSON，`active_webui_sessions` 严格只在 webui 条目
- [x] 3.5 SSE 复用 `GatewayWatcher.subscribe()` 队列；5s `queue.get` 超时后也重建快照（用于 webui-only 变化），无额外 polling 线程
- [x] 3.6 `handle_agent_activity_stream`：`event: snapshot` 初始 → `event: delta` 差异（`_snapshot_signature` 忽略 `generated_at`）→ `event: heartbeat` 30s 无事件时
- [x] 3.7 循环首部检查 `_active_profile_name()` / `_resolve_db_path()` 变化；变化时写 `event: profile_changed` 并 break
- [x] 3.8 routes.py 已在 stage 1 绑定；501 存根自动换成实现
- [x] 3.9 `tests/test_agent_activity.py` — 38 tests 全绿：scaffold + derive 参数化 + 空 DB + 24h 计数 + expand/未知 source/空 source + cache/refresh/401 + **contract 测试确认 `current_tool / pending_count / is_running_tool` 不在响应里** + SSE 无 watcher 503
- [x] 3.10 `tests/test_pixel_office_frontend.py::test_pixel_no_runtime_perception_phrasing` 已在 stage 1 落地；覆盖 4 条禁用短语

## 4. 后端：Surfaces 聚合 API

- [x] 4.1 `build_surfaces_cards = build_surface_snapshot` — 卡片数据与 agent-activity 快照结构一致（合并避免重复聚合）
- [x] 4.2 `handle_surfaces`：默认走快照；`?expand=1&source=X` 走 `build_surface_expand`，返回 `{sessions: [...]}`（最近 5，title 经 `_redact_text` 脱敏）
- [x] 4.3 独立缓存 key：`('surfaces', ...)` vs `('surfaces-expand', db, source)`；expand 按 source 维度单独缓存
- [x] 4.4 未知 source 或空 source 返回 `{"sessions": []}`（HTTP 200，非 404）
- [x] 4.5 `tests/test_agent_activity.py` 覆盖 surfaces 分支 — 6 个用例：snapshot/expand/未知 source/分 cache/401/`_redact_title` 脱敏 (复用 `api.helpers._redact_text`)

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
