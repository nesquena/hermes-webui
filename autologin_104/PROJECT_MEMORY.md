# 104 自動化人才篩選系統 — 專案記憶

> 最後更新：2026-05-11｜狀態：可用，已端到端驗證

## 一句話描述

讓 HR 用自然語言寫職缺需求 → 系統自動搜尋 104 → LLM 評分 → 自動轉寄符合條件人選。

## 整體流程

```
HR 在 Google Drive 寫 .txt
       ↓ rclone sync
Mac mini: jobs_brief/
       ↓ parse_job_brief.py (gemma4:26b 解析)
jobs/<id>.json
       ↓ run_all.sh
       ├─ run_search.py → autologin_104.py → candidates_raw.json
       └─ auto_forward.py → 規則 60% + LLM 40% + 轉寄
results/<job>/analysis_*.md
       ↓ rclone copy
Google Drive 分析報告/（HR 在這看結果）
```

## 程式清單（共 7 支）

| 檔案 | 角色 |
|------|------|
| `sync_briefs.sh` | 入口：Drive 同步 → 解析 → 跑全套 → 回傳結果 |
| `run_all.sh` | 串接搜尋+評分；含「今日已搜過自動略過」「自動關閉自動化 Chrome」 |
| `run_search.py` | 搜尋包裝器；含 retry、chrome-error 偵測、idle/hard timeout |
| `autologin_104.py` | 既有 1265 行 browser-use 引擎（不動） |
| `auto_forward.py` | 評分+轉寄；含規則評分、LLM 整合、履歷快取、去重 |
| `llm_score.py` | Ollama 介面（gemma4:e4b 評分、gemma4:26b 寫信）|
| `parse_job_brief.py` | 自然語言 → JSON parser (gemma4:26b) |

## 4 個職缺設定

| job_id | 顯示名 | 城市 | 年齡 | 年資 | threshold |
|--------|--------|------|------|------|-----------|
| jidian-tainan | 機電(副)主任(台南) | 台南 | 38-58 | 3+ | 65 |
| jidian-kaohsiung | 機電(副)主任(高雄) | 高雄 | 38-58 | 3+ | 65 |
| gongdi-tainan | 工地工程師(台南) | 台南 | 28-40 | 3+ | 80 |
| gongdi-taichung | 工地(副)主任(台中) | 台中 | 40-58 | 5+ | 85 |

## 評分公式

```
最終分數 = 規則分(0-100) × 0.6 + LLM分(0-100) × 0.4
```

### 規則分（從 candidates_raw.json + 完整履歷）
- 營造/建設經驗 ×N → +14~22
- 相關職稱 ×N → +6~22
- 長任職 ≥3年 → +12（或 require_two_long_tenures 時要 2 份）
- 等級資格（主任/副主任）→ +16~25
- 集合住宅關鍵字 → +0~8
- 公共工程關鍵字 → +0~6
- 自傳積極正向（認真/負責/溝通...）→ +0~8
- 居住地加分 → +7

### LLM 分（gemma4:e4b 從自傳/工作描述語意）
0-100，看自傳積極性、建案完整度、誠信穩定性

## 關鍵技術決策

| 決策 | 原因 |
|------|------|
| 用 CDP debug port + websockets 操作 104 | 不依賴 browser-use，更穩定 |
| 點 `aot-button[label="發送"]` 用 Input.dispatchMouseEvent | Web Component，普通 .click() 不行 |
| gemma4:e4b 評分 + gemma4:26b 寫信 | qwen3.5:9b 在長中文 prompt 回空，gemma 較穩 |
| num_predict=8000, num_ctx=8192 | 中文 token 多，3000 會截斷 |
| JSON extract 用平衡括號掃描 | regex 非貪婪會卡第一個 } |
| Chrome temp profile 每次清掉 | 殘留 user-data-dir 會累積卡死 |
| 自動偵測「今日已搜過」跳過搜尋 | 避免每 job 都 cold-start Chrome (~60s) |
| 履歷正文 cache TTL 3 天 | 平衡新鮮度 vs 速度 |

## 已知 issue / 待辦

### 已知問題
1. **LLM 偶爾錯字**：例如「冷凍空調」→「冷凍空iod」
   - 待加：白名單校正模組（fuzzy match 104 標準欄位）
2. **Chrome 冷啟動 Profile 4 不穩**：首次連 104 容易 chrome-error
   - 已加：自動 retry 2 次 + 殘留清理
3. **autologin_104.py 完成後 BrowserSession 重連迴圈不退出**
   - 已加：偵測 candidates_raw.json 落盤後主動 terminate

### 計畫中
- [ ] 白名單校正（半天）
- [ ] HR 端錯誤回報（.errors.txt 寫回 Drive，半天）
- [ ] 每日 summary report（半天）
- [ ] 部署 cron 每日 9:00 自動跑

## 常用指令速查

```bash
# === HR 視角（完全不用碰）===
# HR 只需在 Google Drive 改 .txt 即可

# === IT 視角 ===

# 手動觸發完整流程
./sync_briefs.sh                  # 同步+解析+跑+回傳
./sync_briefs.sh --dry-run        # 跑但不轉寄
./sync_briefs.sh --parse-only     # 只到解析

# 單 job 操作
python3 auto_forward.py --job jidian-tainan --dry-run
python3 auto_forward.py --job jidian-tainan --no-llm          # 純規則
python3 auto_forward.py --job jidian-tainan --no-cache        # 不用快取
python3 auto_forward.py --job jidian-tainan --force <rid>     # 強制重發
python3 auto_forward.py --job jidian-tainan --clear-cache     # 清快取

# 列職缺
python3 auto_forward.py --list-jobs

# 檢查 Ollama
python3 auto_forward.py --llm-check

# 多 job 批次
./run_all.sh                       # 全跑（含 Chrome 自動關）
./run_all.sh --keep-chrome         # 保留 Chrome
./run_all.sh --skip-search         # 用既有 candidates
./run_all.sh --force-search        # 強制重搜
./run_all.sh jidian-tainan         # 指定 job

# rclone
rclone lsd gdrive:                 # 列雲端資料夾
rclone copy gdrive:104職缺/ jobs_brief/ --update
```

## 路徑與檔案

```
/Users/fongyimac/hermes-webui/autologin_104/
├── jobs/                    機器格式（自動產生）
├── jobs_brief/              HR 自然語言（從 Drive 同步）
├── results/<job>/           執行結果
│   ├── candidates_raw.json  搜尋產出
│   ├── resume_cache.json    履歷正文快取（TTL 3 天）
│   ├── forward_log.json     轉寄歷史 + forwarded_ids 去重
│   └── analysis_*.md        日報
└── (7 支 .py / .sh)

Google Drive:
└── 104職缺/
    ├── _README_給HR.md
    ├── <職缺>.txt           HR 寫的
    └── 分析報告/<job>/      系統回傳
```

## LLM 模型用途

- **gemma4:e4b** (9.6GB) - 履歷評分階段，每位人選跑一次，~40s
- **gemma4:26b** (17GB) - 寫轉寄信件 + 解析職缺 brief，~80-120s
- 已嘗試但不適用：qwen3.5:9b（長中文 prompt 回空）
- 已查證但不可用：NVFP4 / MTP 模型（需 NVIDIA + 特殊推理棧）

## 已轉寄歷史（去重基準）

- **蘇倉億** (1843770840755) at jidian-tainan：已轉寄至 i00788@fong-yi.com.tw
  - 規則 92% / 加 LLM 後 86%
  - 主任級，PCM 經驗 20+ 案
