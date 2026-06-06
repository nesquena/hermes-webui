# 多職缺自動化流程

把 104 招募管理「搜尋人才 → 評分 → 轉寄」整套包成可由設定檔驅動的 pipeline。
新增職缺只要在 `jobs/` 寫一份 JSON，不必改程式。

## 架構

```
autologin_104/
├── jobs/                              # 職缺設定（每個職缺一份 JSON）
│   ├── jidian-tainan.json             # 機電(副)主任 (台南)
│   ├── jidian-kaohsiung.json          # 機電(副)主任 (高雄)
│   ├── gongdi-tainan.json             # 工地工程師 (台南)
│   └── gongdi-taichung.json           # 工地(副)主任 (台中)
│
├── results/                           # 每個職缺各自的執行結果
│   └── <job_id>/
│       ├── candidates_raw.json        # 搜尋階段產出
│       ├── forward_log.json           # 評分+轉寄歷史（含去重 ids）
│       └── analysis_<YYYYMMDD>.md     # 每次跑的分析報告
│
├── autologin_104.py                   # 既有：搜尋階段（read criteria.md）
├── run_search.py                      # 新增：把 jobs/<id>.json → criteria.md → 跑 autologin
├── auto_forward.py                    # 新增：載 jobs/<id>.json，評分 + 自動轉寄
└── run_all.sh                         # 新增：批次跑所有 / 指定職缺
```

## 快速開始

### 列出所有職缺

```bash
python3 auto_forward.py --list-jobs
python3 run_search.py --list-jobs
```

### 跑單一職缺（兩階段）

```bash
# 階段一：搜尋並產出 candidates_raw.json
python3 run_search.py --job jidian-kaohsiung

# 階段二：評分 + 自動轉寄達門檻人選
python3 auto_forward.py --job jidian-kaohsiung
```

### 一鍵跑所有職缺

```bash
./run_all.sh                                # 全部
./run_all.sh --dry-run                      # 全部，但不實際轉寄
./run_all.sh jidian-tainan jidian-kaohsiung # 指定特定 job
```

### 常用 auto_forward.py flags

```bash
--job <id>          職缺代碼（必填）
--dry-run           只分析不轉寄
--threshold 75      覆寫 profile 的 threshold（預設讀 profile.scoring.threshold）
--port 52751        手動指定 Chrome CDP debug port
--force <resumeId>  強制重發已轉寄過的人選（可重複指定）
--reset-history     清空該 job 的去重記錄
```

## 職缺設定檔格式

每個 `jobs/<job_id>.json` 包含三個區塊：

```jsonc
{
  "job_id": "jidian-tainan",
  "display_name": "機電(副)主任(台南)",

  "search": {                                 // 階段一：餵給 autologin_104.py 的 104 表單欄位
    "keyword": "機電 水電",
    "job_categories": [...],
    "work_locations": ["台南市"],
    "home_locations": ["台南市"],
    "last_action_days": "7天內",
    "work_exp_years": 3,
    "majors": [...],
    "age_min": 40, "age_max": 57,
    "tools": ["AutoCAD"],
    "certificates": [...]
  },

  "scoring": {                                // 階段二：auto_forward.py 用的評分規則
    "construction_kw": ["營造","建設","建築師事務所"],
    "title_kw": ["機電","水電","監造",...],
    "residential_kw": ["集合住宅","住宅大樓",...],
    "public_kw": ["公共工程","道路",...],
    "autobio_positive": ["認真","負責",...],
    "threshold": 80,                          // 達這個百分比才會被轉寄
    "level_rules": {
      "senior": {"min_years":5,"min_projects":2,"name":"主任級"},
      "junior": {"min_years":3,"min_projects":1,"name":"副主任級"}
    },
    "long_tenure_months": 36,                 // 「單一任職多久算長期」
    "require_two_long_tenures": false,        // 工地副主任 (台中) 設 true
    "residence_bonus_keyword": "台南"
  },

  "forward": {
    "format": "complete",                     // 完整版/摘要版（待 auto_forward 支援切換）
    "summary_max_chars": 980                  // 「說明」欄位字數上限
  }
}
```

## 評分模型

對每位人選，依 profile.scoring 動態計分（滿分 100）：

| 項目 | 配分 | 說明 |
|------|------|------|
| 營造/建設/建築師事務所 | 14–22 | 從 candidates_raw.json 工作經歷比對 |
| 相關職稱（依 title_kw） | 6–22 | 同上 |
| 長任職門檻 | 8–16 | `require_two_long_tenures` 為 true 時要 2 份 |
| 等級資格 | 16–25 | 依 level_rules 判定主任/副主任/不符 |
| 集合住宅關鍵字 | 0–8 | 從**完整履歷正文+自傳**逐字搜尋 |
| 公共工程關鍵字 | 0–6 | 同上 |
| 自傳積極正向 | 0–8 | 自傳區塊找 autobio_positive 關鍵字 |
| 居住地加分 | 0–7 | residence_bonus_keyword |

達 `threshold` 自動進入轉寄流程。

## 去重機制

每個 job 各自有 `results/<job>/forward_log.json`，內含：

```jsonc
{
  "forwarded_ids": ["1843770840755", ...],   // 該 job 已成功轉寄過的 resumeId
  "runs": [...]                              // 每次執行的詳細記錄
}
```

下次再跑同一 job：
- 達門檻但 resumeId 已在 `forwarded_ids` → 自動略過
- 不同 job 之間獨立計算（同一人在 A job 轉寄不影響 B job）
- `--force <id>` 強制重發單一人選
- `--reset-history` 清空 forwarded_ids（保留 runs 歷史）

## 新增職缺的步驟

1. 在 `jobs/` 建立新的 `<job_id>.json`，填入 search / scoring / forward
2. `mkdir results/<job_id>`
3. `python3 run_search.py --list-jobs` 確認新 job 出現
4. `python3 run_search.py --job <new_id>` 試跑搜尋
5. `python3 auto_forward.py --job <new_id> --dry-run` 試跑分析（不轉寄）
6. 確認分析結果合理後，去掉 --dry-run 跑正式版

## 排程範例（每日早上九點）

```bash
0 9 * * * cd /Users/fongyimac/hermes-webui/autologin_104 && ./run_all.sh >> daily.log 2>&1
```

## 故障排除

| 問題 | 原因 / 解法 |
|------|------|
| `❌ 找不到 Chrome CDP debug port` | Chrome 已關閉或沒帶 `--remote-debugging-port`。先跑 `run_search.py` 啟動 Chrome session |
| `❌ 沒有開啟中的 104 分頁` | CDP 連得上但分頁全關了。在 Chrome 開任一個 vip.104.com.tw 分頁 |
| 搜尋階段有時卡在 modal | 直接重跑 `run_search.py --job <id>` |
| 想換職缺條件 | 改 `jobs/<id>.json` 的 search 區塊；不必改程式 |
| 想換評分規則 | 改 `jobs/<id>.json` 的 scoring 區塊；不必改程式 |
| Python 3.9 跑出 `int \| None` 錯誤 | 已加 `from __future__ import annotations`，重抓最新版 |
