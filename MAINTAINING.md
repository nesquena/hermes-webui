# Maintaining hermes-webui-cn

本文档说明：
- 本地化补丁的组织方式
- 上游同步的标准流程
- 冲突排查思路
- 如何冒烟验证后再推

## 仓库定位

- 上游：`nesquena/hermes-webui`（默认分支 `master`）
- 本仓库：`Eynzof/hermes-webui-cn`，默认分支 `main`，跟踪上游 `master`
- 关系：真 fork（`git merge-base` 非空），所有本地化以 `[CN-fork] P-XXX:` 前缀的小提交叠加在 upstream 之上

## 当前本地化补丁

| 编号 | 主题 | 范围 |
|------|------|------|
| P-001 | 默认 UI 语言改为中文 | `api/config.py`、`api/routes.py`、`static/i18n.js`、`static/boot.js`、`static/panels.js`、`static/index.html` |
| P-002 | 中文 README，标题改为 hermes-webui-cn | `README.md`（新写）、`README.en.md`（原英文备份） |
| P-003 | 增加上游同步工具链 | `scripts/sync-upstream.sh`、`.github/workflows/upstream-watch.yml`、`MAINTAINING.md` |
| P-004 | 同步到 cnb.cool 镜像的 CI workflow | `.github/workflows/sync-to-cnb-mirror.yml` |
| P-005 | 设置页 sidebar 6 个 tab 标签中文化 | `static/index.html`（5 处补 `data-i18n`），`static/i18n.js`（en 加 `settings_tab_plugins`，zh 翻译 5 个 tab key + `providers_tab_title`） |
| P-006 | README 加"检查更新走哪"段落 | `README.md` |
| P-007 | Providers 面板中文化（20 个 zh stub 翻译 + quota 卡片硬编码 i18n 化） | `static/i18n.js`（en 加 7 个 quota key，zh 翻译 20 个 providers stub + `settings_dropdown_providers` + 加 7 个 quota key 中文），`static/panels.js`（quota 卡片标题/兜底/状态徽标走 t()，加 `_quotaStateLabel` 辅助函数） |
| P-008 | 设置页 settings_* 剩余 stub 翻译 | `static/i18n.js`（zh 翻译 15 个 settings_dropdown_* / settings_heading_* / settings_section_*_title|meta / settings_label_session_jump_buttons / settings_desc_session_jump_buttons） |

新增补丁约定：
- 单一关切（不要把多个无关改动塞进同一个 P）
- 提交标题用 `[CN-fork] P-NNN: 一句话描述`
- 编号顺延，不复用
- 在本表登记

## 同步上游

```bash
git checkout main
./scripts/sync-upstream.sh
```

脚本会：
1. 校验 `upstream` remote、工作树是否干净、当前是否在 `main`
2. `git fetch upstream`
3. 显示 `main` 与 `upstream/master` 的领先/落后情况以及待并入的提交清单
4. 询问是否执行 `git merge upstream/master`
5. 干净合并 → 提示下一步；冲突 → 列出冲突文件并退出码 2

合并干净后必须本地冒烟一次，再推。

## 冒烟测试 (Smoke test)

```bash
# 1. 启动 server（无 hermes-agent 也能起来，落到 onboarding 引导）
python3 bootstrap.py
# 或：HERMES_WEBUI_PORT=8799 python3 server.py

# 2. 关键验证项
#   - http://127.0.0.1:8787 加载正常，<html lang="zh-CN">
#   - GET /api/settings 返回 "language": "zh"（首次启动；已有 settings.json 的不影响）
#   - /login 显示中文标题/占位符/按钮
#   - 在设置面板切到 English → 重新加载后保持英文
```

## 冲突排查

每个 `[CN-fork] P-XXX` 补丁触碰的文件清单见上表。冲突几乎一定落在那些文件上：

- **`api/config.py` 的 `_SETTINGS_DEFAULTS`**：上游往字典里加新 key 时，我们对 `language` 的修改通常不会冲突；如果上游重命名/重排顺序，手动把 `"language": "zh"` 留住即可
- **`api/routes.py` 的 `_resolve_login_locale_key`**：上游若调整 fallback 逻辑，把我们的 `return "zh"`（共 3 处）保留
- **`static/i18n.js` 的 `resolvePreferredLocale` / `setLocale`**：保留 `'zh'` 兜底
- **`static/boot.js`、`static/panels.js`**：保留 `'zh'` 兜底
- **`static/index.html`**：第 2 行保留 `<html lang="zh-CN">`
- **`README.md`**：上游若改 README，我们这边整篇不动；如果想吸收上游的某些段落，更新一下 `README.en.md` 即可
- **`README.en.md`**：理论上每次 sync 都该和上游 README 同步——可以改 `sync-upstream.sh` 在合并完成后自动 `git checkout upstream/master -- README.md && git mv README.md README.en.md.tmp && mv README.en.md.tmp README.en.md`，但目前手工

## 自动监测

`.github/workflows/upstream-watch.yml` 周一 09:00 UTC 跑一次 cron（也可以 workflow_dispatch 手动触发），检测到 upstream 领先就开 / 更新一个带 `upstream-watch` label 的 issue。

issue 出现后的 SOP：
1. 拉本地最新 `main`
2. 跑 `./scripts/sync-upstream.sh`
3. 干净合并 → 冒烟 → push → 关 issue
4. 冲突 → 按"冲突排查"逐一处理 → 解决后 commit 完成 merge → push → 关 issue

## 不要做的事

- **不要 squash 合并 upstream**：会丢上游 commit 历史，未来分歧排查困难
- **不要 rebase main 到 upstream**：会重写 `[CN-fork]` 提交的 SHA，破坏其他人本地的 fork
- **不要在 main 上直接做非 [CN-fork] 提交**：补丁和上游 merge 之外的"杂活"统统走分支 + PR
- **不要给本地化补丁取通用化名字**（比如把 P-001 拆成"修改后端"+"修改前端"）：每个 P 是单一关切，原子可回滚
