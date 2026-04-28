# hermes-webui 会话切换性能优化 — 设计与实现计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal**：消除会话切换时的卡顿，让"切换→看到内容"的可感知延迟降到 < 100 ms（无论会话长短，无论是否首次访问）。

**Architecture**：三段式分层优化 —— (1) 后端补"按需分页"接口；(2) 前端把"切换=全量重渲"改成"切换=增量挂载 + 视口虚拟化"；(3) 重构缓存为消息级 DOM 节点池，避免 innerHTML 字符串往返。

**Tech Stack**：保持现状 —— Python stdlib HTTP 服务、原生 JS / CSS、pytest。**不引入框架**。仅在性能基准里使用 `playwright`（dev 依赖，CI 可选）。

---

## 现状摸底（已读完）

| 文件 | 行数 | 关键函数 |
|---|---|---|
| `static/ui.js` | 3463 | `renderMessages` (2221), `renderMd` (767), `_sessionHtmlCache` (2218), `INFLIGHT` |
| `api/routes.py` | — | `/api/session` (698)，已支持 `?messages=0` 仅取 metadata |
| `static/style.css` | 2345 | — |

**瓶颈定位**（基于代码阅读 + Phase 1 Diagnosis 计划再用脚本验证）：

| 编号 | 链路 | 估计耗时（200 条消息长会话） | 影响场景 |
|---|---|---|---|
| **P1** | `GET /api/session` 一次返回全部 message JSON | 100–800 ms 网络 + JSON parse | A |
| **P2** | `renderMessages` 主线程同步 markdown + DOMPurify + Prism | 300–2000 ms | A、B（cache miss 时） |
| **P3** | `inner.innerHTML=''` + 大批 append → layout/paint 风暴 | 100–500 ms | A、B |
| **P4** | mermaid/katex 全量渲染（视口外也渲） | 200 ms × N | A |
| **P5** | `_sessionHtmlCache` 仅 8 槽 LRU，且 `cached.msgCount===msgCount` 严格等同 | 命中率低 | B |

**用户确认场景：A（首次切到长会话）+ B（已访问会话间反复切换）。** 计划同时覆盖。

---

## 设计原则

- **YAGNI**：不引虚拟滚动库、不上 React/Vue、不重写架构。
- **测量驱动**：每个优化前先有基准测试；改完比对前后。
- **零回归**：现有 250+ pytest 必须全过。
- **渐进增强**：旧浏览器 / 网络慢时的降级行为不变。

---

## 任务列表（TDD，每步 2–5 分钟）

### Task 0：基准测量与诊断脚手架（先量后改）

**Files:**
- Create: `tests/test_session_switch_perf.py`
- Create: `scripts/perf_session_switch.py` （生成 200 条消息的 fixture session）

**Step 0.1** 写一个 fixture：构造一个含 N=200 条混合消息（含 5 个 mermaid、5 个 katex、20 个 code block）的测试会话写入磁盘。

**Step 0.2** 写测试 `test_api_session_full_payload_under_300ms`：调 `/api/session?session_id=...`，断言响应 < 300 ms 且字节 < 2 MB（**会失败 → 暴露基线**）。

**Step 0.3** 写测试 `test_api_session_metadata_only_under_50ms`：调 `?messages=0`，断言 < 50 ms。（应当过 — 已实现）

**Step 0.4** 写浏览器 perf 测试（Playwright，**仅 dev**）：`tests/perf/test_render_long_session.py`，跑两次切换测 `performance.measure`。**标记 `@pytest.mark.perf`，CI 默认跳过**。

**Step 0.5** 跑一遍记录"基线数字"，写到 `docs/plans/PERF_BASELINE.md`。

**Commit**：`perf(test): add session-switch perf baseline harness`

---

### Task 1：后端 — 消息分页接口

**Files:**
- Modify: `api/routes.py:698-746`（`/api/session` 加 `?since_idx`、`?limit`、`?tail` 参数）
- Create: `tests/test_session_pagination.py`

**Step 1.1** **失败测试**：`test_session_tail_returns_last_n`
```python
def test_session_tail_returns_last_50():
    # 200-msg session
    r = api_get(f"/api/session?session_id={sid}&tail=50")
    assert r["session"]["message_count"] == 200
    assert len(r["session"]["messages"]) == 50
    assert r["session"]["messages"][0]["_idx"] == 150
    assert r["session"]["pagination"] == {"start_idx": 150, "end_idx": 200, "total": 200}
```

**Step 1.2** **实现**：在 `/api/session` 里读 `tail`/`since_idx`/`limit`，对 `s.messages` 做切片；附加 `_idx` 字段（仅响应中），返回 `pagination` 元信息。**默认行为不变**（无参数时返回全部，向后兼容）。

**Step 1.3** **失败测试**：`test_session_since_idx_returns_window`
```python
r = api_get(f"/api/session?session_id={sid}&since_idx=100&limit=50")
assert [m["_idx"] for m in r["session"]["messages"]] == list(range(100, 150))
```

**Step 1.4** 边界用例：`tail` > total、`since_idx` < 0、`limit` <= 0。每个一个失败测试，再实现 clamp。

**Step 1.5** 性能测试：`test_session_tail_50_under_50ms`（200 条会话 + tail=50 < 50 ms）。

**Commit**：`feat(api): paginate /api/session via tail/since_idx/limit (backward compat)`

---

### Task 2：前端 — `_sessionHtmlCache` 升级为节点池 + 改用 DocumentFragment

**Files:**
- Modify: `static/ui.js` 附近 2218–2240、2592–2610
- Create: `tests/test_session_cache_keying.py` (静态分析测试)

**Step 2.1** **失败测试** `test_cache_key_includes_last_msg_id`：grep `static/ui.js`，断言缓存 key 包含 `lastMsgId` 或类似指纹（不是只 `msgCount`）。这样消息编辑/重试不会误命中。

**Step 2.2** **实现**：
- 把 `_sessionHtmlCache` 的 value 从 `{html, msgCount}` 改成 `{fragment: DocumentFragment, msgCount, lastMsgKey}`。
- `lastMsgKey` = `JSON.stringify({len, lastId, lastTs, lastContent.slice(0,40)})`。
- 命中后用 `inner.replaceChildren(fragment.cloneNode(true))`，**避免 `innerHTML=` 字符串往返**（省一次 HTML parse）。
- LRU 容量 8 → 16。

**Step 2.3** **失败测试** `test_cache_uses_clonenode`：grep 断言出现 `cloneNode(true)` 或 `replaceChildren`。

**Step 2.4** 跑回归：`pytest tests/ -q`，250+ 应全过。

**Commit**：`perf(ui): cache parsed DOM fragments per session, finer cache key`

---

### Task 3：前端 — 视口虚拟化（Windowed Render）

**核心思路**：长会话切入时，**先只渲染最后 30 条 + 顶部占位**；用户向上滚动时，按页（30 条）增量插入。**不引虚拟滚动库**，自己实现"懒挂载头部"。

**Files:**
- Modify: `static/ui.js`（新增 `_mountHistoryWindow()` + IntersectionObserver）
- Modify: `static/style.css`（顶部 sentinel 样式）
- Create: `tests/test_windowed_render.py`

**Step 3.1** **失败测试** `test_windowed_render_present`：grep 断言 `_mountHistoryWindow` 函数存在 + IntersectionObserver 用于 sentinel。

**Step 3.2** **实现**：
- `renderMessages` 在 `vis.length > WINDOW_INITIAL (=30)` 时：
  - 顶部插一个 `<div class="history-sentinel" data-pending="N">` 占位（带 N 条消息的预估高度，避免滚动条跳）。
  - 只渲染最后 30 条到 `inner`。
  - 注册 IntersectionObserver；sentinel 进入视口时，`_mountHistoryWindow(prev=30)` 再渲一批，更新 sentinel pending 数。
- 预估高度：用历史 `avgMsgHeight = recordedSum / recordedCount`，初始用 200 px。
- 滚动位置：保持"最新消息可见且贴底"。

**Step 3.3** 配合 Task 1：首次切换时，前端调 `/api/session?tail=30`，**只下载尾部**；用户上滚才补全（或后台 idle 时预拉）。

**Step 3.4** 测试切换时间 < 100 ms（Playwright `@pytest.mark.perf`）。

**Commit**：`perf(ui): windowed render — only mount tail on switch, lazy mount on scroll`

---

### Task 4：前端 — markdown 解析迁出主线程（Web Worker）

**Files:**
- Create: `static/markdown-worker.js`
- Modify: `static/ui.js`（`renderMd` 调度到 worker，主线程仅做 DOMPurify + DOM 挂载）
- Create: `tests/test_markdown_worker.py`

**Step 4.1** **失败测试** `test_markdown_worker_exists` + `test_render_md_uses_worker_for_long_text`：grep 断言。

**Step 4.2** 抽出 `renderMd` 中的纯字符串处理（regex、escape、tokenize）到 worker。**主线程保留 DOMPurify + DOM 操作**（安全 + DOM 必须主线程）。

**Step 4.3** 短文本（< 500 字符）走同步快路径，避免 worker IPC 开销。

**Step 4.4** 错误降级：worker 加载失败 / postMessage 超时 → 同步回退到原 `renderMd`。

**Step 4.5** 回归 250+ 测试。

**Commit**：`perf(ui): offload markdown parsing to web worker for long messages`

---

### Task 5：mermaid/katex 懒渲染（视口可见才渲）

**Files:**
- Modify: `static/ui.js:2882 renderMermaidBlocks` 和 `renderKatexBlocks`

**Step 5.1** **失败测试**：grep 断言 mermaid/katex 渲染走 IntersectionObserver。

**Step 5.2** 实现：把 `forEach(block)` 改成 IntersectionObserver 注册；`isIntersecting` 时才 `mermaid.render`。视口外的块保持 `<pre><code>` 占位。

**Step 5.3** 配合上次"Syntax Error 巨型遮罩"修复一并验证；新写一个 `test_lazy_diagram_render.py`。

**Commit**：`perf(ui): lazy-render mermaid/katex when scrolled into view`

---

### Task 6：HTTP 层 — 启用 gzip 与 ETag

**Files:**
- Modify: `api/helpers.py` 或 `server.py`（`j()` 写响应处加 gzip）
- Create: `tests/test_http_gzip_etag.py`

**Step 6.1** **失败测试** `test_session_response_is_gzipped_when_accept_encoding_set`。

**Step 6.2** 实现：响应 ≥ 1 KB + `Accept-Encoding: gzip` → 用 `gzip.compress`；加 `Content-Encoding: gzip` 头。

**Step 6.3** **失败测试** `test_session_returns_etag_and_304_on_match`。

**Step 6.4** 实现 ETag = `weak"<sha1(payload)[:16]>"`；客户端 `If-None-Match` 命中返回 304。

**Step 6.5** 前端：`api()` 自动带 `If-None-Match`，304 时复用上次响应。

**Commit**：`perf(api): gzip + ETag for /api/session responses`

---

### Task 7：DocumentFragment + RAF 切片渲染（兜底未命中）

**Files:**
- Modify: `static/ui.js` `renderMessages` 主路径

**Step 7.1** **失败测试** `test_render_uses_document_fragment`：grep 断言 `createDocumentFragment` 在 renderMessages 内。

**Step 7.2** 实现：所有 `inner.appendChild(node)` 改成先放进 `DocumentFragment`，循环结束再 `inner.appendChild(frag)` 一次性挂载。

**Step 7.3** 长会话渲染走 `requestIdleCallback` / `requestAnimationFrame` 切片：每帧 ≤ 16 ms 工作量，分多帧完成。**带 sentinel 占位**保证滚动条不跳。

**Step 7.4** 回归。

**Commit**：`perf(ui): batch DOM mounts via DocumentFragment + frame-sliced rendering`

---

### Task 8：终结测试 + 文档

**Files:**
- Create: `docs/PERFORMANCE.md`
- Create: `docs/plans/PERF_RESULTS.md`

**Step 8.1** 跑全量基线对比，记录改进数字。

**Step 8.2** 写 `docs/PERFORMANCE.md`：架构图、缓存策略、虚拟化说明、未来优化方向。

**Step 8.3** 更新 `BUGS.md` / `CHANGELOG.md`。

**Step 8.4** **`tests/test_session_switch_perf.py` 由 skip 改为强制 PASS**（断言数字 < 阈值）。

**Commit**：`docs(perf): document optimization & finalize benchmark gates`

---

## 验收标准（verification-before-completion）

| 指标 | 当前估计 | 目标 | 测试方式 |
|---|---|---|---|
| `/api/session?tail=30` (200-msg) | — | < 50 ms | pytest |
| `/api/session` 全量 (200-msg) | 100–800 ms | < 200 ms（gzip 后） | pytest |
| 浏览器：首次切到长会话→首屏 | 1–3 s | < 200 ms | playwright |
| 浏览器：已访问会话切换→首屏 | 200–500 ms | < 50 ms | playwright |
| 单元 + 集成测试 | 250 通过 | 250 + 新增全部通过 | pytest |
| 视觉回归 | — | 截图无差异 | playwright（手动） |

**Definition of Done**：
1. 全部 task 提交，每个有独立 commit 信息
2. 基线对比表在 `docs/plans/PERF_RESULTS.md`
3. 新增测试全过；老测试零回归
4. 用户在浏览器中实测确认改善（不能仅靠数字）

---

## 风险与回滚

| 风险 | 缓解 |
|---|---|
| 虚拟化破坏"跳转到指定消息"功能 | Task 3 完成后新增 `test_jump_to_message_in_history.py` 验证 |
| Worker 在 file:// 协议下不可用 | 检测失败自动降级同步 renderMd |
| ETag 与流式响应冲突 | 仅对 GET `/api/session` 加 ETag，不影响 SSE 路径 |
| 缓存 fragment 持有大量 DOM 内存 | LRU + 16 槽上限 + `clear()` API（切换 profile 时调用） |

每个 task 独立 commit，任何一步不达标可单独 revert。

---

## 不在本计划范围内（YAGNI）

- 不重写为 React/Vue/Svelte
- 不引入 Webpack/Vite 构建链
- 不上 IndexedDB 持久化（浏览器 sessionStorage 足够）
- 不改 SSE 流式协议（与切换性能无关）
- 不优化"侧栏列表渲染"（用户痛点是切换 → 内容区，不是侧栏）
