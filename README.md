# hermes-webui-cn

> [English README →](./README.en.md)

[Hermes WebUI](https://github.com/nesquena/hermes-webui) 的中文本地化分支，默认中文界面，定期同步上游。

适合中国大陆用户：上游 zh 字典已较完整，本分支只在其上做"开箱即中文"的默认值调整与必要文档翻译，不做 UI 重写、不裁剪功能，便于跟随上游更新。

---

## 与上游的关系

- **上游**：[`nesquena/hermes-webui`](https://github.com/nesquena/hermes-webui)
- **当前基线**：`v0.51.92`
- **同步策略**：`scripts/sync-upstream.sh` 周期性 `git merge upstream/master`，本地化补丁以 `[CN-fork] P-XXX:` 前缀的提交叠加在 upstream 之上
- **本地化范围**：默认语言、登录页 locale 兜底、`<html lang>` 等"开箱即中文"相关的小改动；UI 字符串仍由上游 `static/i18n.js` 的 `zh` / `zh-Hant` 字典维护

完整本地化补丁列表见 [`MAINTAINING.md`](./MAINTAINING.md)。

## 快速开始

```bash
git clone https://github.com/Eynzof/hermes-webui-cn.git
cd hermes-webui-cn
python3 bootstrap.py
```

服务默认监听 `http://127.0.0.1:8787`，首次启动即为中文界面。

更详细的部署、配置环境变量、对接 hermes-agent 等内容，请参考 [上游英文 README](./README.en.md)——本分支不修改部署逻辑，所有运行手册仍以上游为准。

## 反馈

- 上游 bug / 通用功能问题 → 直接给 [`nesquena/hermes-webui`](https://github.com/nesquena/hermes-webui/issues) 提
- 仅本地化层 / 中文相关问题 → 给本仓库提 issue
