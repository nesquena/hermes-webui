## ADDED Requirements

### Requirement: Pixel Office 面板入口
系统 SHALL 在 sidebar 提供名为 "Pixel" 的 nav tab。点击后主视图 SHALL 被画布全屏接管（隐藏 Chat 消息区），顶部提供返回按钮回到上一面板。

#### Scenario: 打开像素办公室
- **WHEN** 用户点击 `Pixel` tab
- **THEN** 主视图渲染 Canvas 2D 像素办公室场景，Chat 输入区被替换为像素工具栏

#### Scenario: 退出像素办公室
- **WHEN** 用户点击顶部返回按钮或再次点击其他 tab
- **THEN** 画布销毁、rAF 停止、主视图恢复原内容

### Requirement: 角色按 surface 映射
Pixel Office SHALL 为每个活跃 surface 创建一个像素角色。角色数 SHALL 随 surface 活跃状态动态增减，上限 8 个角色。

#### Scenario: surface 上线产生角色
- **WHEN** 某 surface 从 `offline` 变为 `working` 或 `waiting`
- **THEN** 引擎在门口生成一个像素角色并走向其专属工位
- **AND** 角色 label 显示 surface 名称（webui / telegram / …）

#### Scenario: surface 下线回收角色
- **WHEN** 某 surface 变回 `offline` 并持续 60 秒
- **THEN** 引擎将对应角色从画布移除，释放工位

#### Scenario: surface 超过上限
- **WHEN** 同时活跃的 surface 数量超过 8
- **THEN** 超出的 surface 合并为一个 `+N` 徽章角色，避免画面拥挤

### Requirement: 角色行为状态机
角色 SHALL 仅根据桥接层推送的 surface `state`（working/waiting/idle/offline）决定行为——**不依赖 `current_tool` 等运行时字段**（agent-activity API 不提供，见 agent-activity-api spec 的数据视角 requirement）：

- `working` → 坐在工位执行 TYPE 动画（含义：最近 60s 有新 message）
- `waiting` → 坐在工位，头顶显示时钟气泡 ⏱（含义：60–300s 内无新 message）
- `idle` → 离开工位执行 WALK 巡游（含义：300s – 24h 无新 message）
- `offline` → 不在画布（含义：超过 24h 无 message 或该 source 不存在）

#### Scenario: working 态打字
- **WHEN** surface state = `working`
- **THEN** 角色移动到工位并播放 TYPE 动画（精灵帧循环）

#### Scenario: waiting 态时钟气泡
- **WHEN** surface state = `waiting`
- **THEN** 角色坐在工位不打字，头顶显示 ⏱ 气泡
- **AND** 气泡文案（tooltip）为 "Last message <N> seconds ago"，**不**使用 "Waiting for your reply" 等暗示运行时感知的措辞

#### Scenario: idle 态巡游
- **WHEN** surface state = `idle`（300s – 24h 无新消息）
- **THEN** 角色起身，按 BFS 寻路在可走 tile 之间随机游走

### Requirement: Canvas 2D 引擎（vanilla JS）
系统 SHALL 在 `static/pixel/` 下以原生 JS 实现像素引擎（state / characters / game-loop / renderer / tile-map），不引入 TypeScript、React、或任何构建步骤。

#### Scenario: 无构建链可运行
- **WHEN** 开发者 clone 仓库后直接 `python server.py`
- **THEN** Pixel Office 可在浏览器直接运行，无需 `npm install` 或 `tsc` 等构建命令

#### Scenario: 后台 tab 暂停
- **WHEN** 浏览器 tab 切换到后台（`document.visibilityState === 'hidden'`）
- **THEN** 引擎 rAF 循环自动暂停，恢复前台时从暂停位置继续

### Requirement: 实时状态桥接
前端 SHALL 通过 SSE 订阅 `/api/agent-activity/stream` 获得实时 surface 状态，并通过桥接模块 `static/pixel/surface-bridge.js` 映射到角色行为。

#### Scenario: SSE 状态变更驱动动画
- **WHEN** SSE 推送某 surface 从 `working` 变为 `idle`
- **THEN** 桥接层在下一帧把对应角色从 TYPE 切换到 WALK

### Requirement: 精灵资产
精灵 SHALL 以 base64 常量存在 `static/pixel/sprites.js`，按需加载（仅在 Pixel Office 打开时注入 `<script>`），不在首屏 HTML inline。

#### Scenario: 懒加载 sprites
- **WHEN** 用户首次打开 Pixel tab
- **THEN** 浏览器才发起 `static/pixel/sprites.js` 请求；其他 tab 的首屏加载不受影响

### Requirement: 不包含 P2 特性
本 change SHALL NOT 实现以下特性（留给后续 change）：布局编辑器、蚂蚁信息素特效、matrix 雨特效、音效、子 agent 可视化、桌面告警。

#### Scenario: 不含布局编辑器
- **WHEN** 用户查看 Pixel Office 工具栏
- **THEN** 工具栏仅包含缩放、返回、暂停/恢复；无 "Edit layout" 按钮

### Requirement: 移动端降级
视口宽度 < 640px 时，Pixel Office SHALL 自动降级为 Surface Dashboard 卡片视图，显示提示 "Pixel Office 需桌面浏览器"。

#### Scenario: 移动端降级
- **WHEN** 用户在手机浏览器点击 Pixel tab
- **THEN** 前端检测 viewport 宽度并展示 Surface Dashboard fallback，不加载精灵资源
