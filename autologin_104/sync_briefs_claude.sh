#!/usr/bin/env bash
# sync_briefs_claude.sh — 用 Claude Code Agent 取代 Ollama 解析職缺需求
#
# 與 sync_briefs.sh 相同流程，但 Step 2（解析）改用 Claude Sonnet 4.6
# 若 Claude CLI 不可用，自動 fallback 到 Ollama（parse_job_brief.py）
#
# 用法：
#   ./sync_briefs_claude.sh              # 完整流程
#   ./sync_briefs_claude.sh --dry-run    # 只到分析（不轉寄）
#   ./sync_briefs_claude.sh --parse-only # 只同步 + 解析
#
# 系統 crontab 範例：
#   3 9 * * * cd /Users/fongyimac/hermes-webui/autologin_104 && ./sync_briefs_claude.sh >> logs/cron.log 2>&1

set -e
cd "$(dirname "$0")"

RCLONE_REMOTE="${RCLONE_REMOTE:-gdrive}"
GDRIVE_BRIEF_DIR="${GDRIVE_BRIEF_DIR:-104職缺}"
GDRIVE_REPORT_DIR="${GDRIVE_REPORT_DIR:-104職缺/分析報告}"
LOCAL_BRIEF_DIR="jobs_brief"
LOCAL_RESULTS_DIR="results"

DRY=""
PARSE_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY="--dry-run" ;;
        --parse-only) PARSE_ONLY=1 ;;
    esac
done

mkdir -p logs

echo "============================================="
echo "📥 Step 1: 從雲端同步職缺需求"
echo "============================================="
mkdir -p "$LOCAL_BRIEF_DIR"
rclone sync "${RCLONE_REMOTE}:${GDRIVE_BRIEF_DIR}" "$LOCAL_BRIEF_DIR" \
    --max-depth 1 \
    --filter "+ *.txt" --filter "+ *.md" \
    --filter "+ *.docx" --filter "+ *.doc" \
    --filter "- _*" --filter "- ~\$*" --filter "- *.errors.txt" \
    --filter "- *" 2>&1 | tail -10
echo "✓ 同步完成"

echo
echo "============================================="
echo "📦 Step 1.5: 封存已完成職缺"
echo "============================================="
python3 archive_orphans.py --apply || true

echo
echo "============================================="
echo "🤖 Step 2: 解析職缺需求 → JSON"
echo "============================================="

# 偵測 Claude CLI（支援全域安裝、npx、homebrew）
CLAUDE_CMD=""
if command -v claude &>/dev/null 2>&1; then
    CLAUDE_CMD="claude"
elif npx --no-install @anthropic-ai/claude-code --version &>/dev/null 2>&1; then
    CLAUDE_CMD="npx -y @anthropic-ai/claude-code"
fi

if [ -n "$CLAUDE_CMD" ]; then
    echo "🔵 使用 Claude Code (Sonnet 4.6) 解析"
    $CLAUDE_CMD --print --model sonnet \
        "你是 104 招募自動化的解析引擎。請執行以下工作：

1. 讀取 /Users/fongyimac/hermes-webui/autologin_104/jobs_brief/ 下所有 .docx/.txt/.md 檔（跳過 _ 和 ~\$ 開頭）
2. 對每個檔案，檢查 jobs/ 裡是否已有對應 JSON（_source_brief 欄位匹配且 JSON 較新）→ 有就跳過
3. 讀取 brief 內容（.docx 用 textutil -convert txt -stdout）
4. 解析成結構化 JSON 寫到 jobs/<id>.json

JSON schema 參考 jobs/ 裡的現有檔案（如 jidian-tainan.json）。

job_id 規則：用檔名優先匹配 role（機電→jidian, 水電→shuidian, 工地→gongdi, 營造→yingzao, 監造→jiandao, 建築→jianzhu, 採購→caigou, 品管→pinguan, 成控→chengkong, 估算→gusuan, 跑照→paozhao, 秘書→mishu, 主管→zhuguan, 會計→kuaiji, 人資→renzi, 行政→xingzheng, 業務→yewu, 設計→sheji, 安衛→anwei）+ city（台北→taipei, 新北→xinbei, 桃園→taoyuan, 新竹→hsinchu, 台中→taichung, 台南→tainan, 高雄→kaohsiung, 屏東→pingtung）

重要：
- 「高雄」→「高雄市」、AutoCad→AutoCAD
- scoring.autobio_positive 固定用 [\"認真\",\"負責\",\"細心\",\"溝通\",\"協調\",\"解決問題\",\"承擔\",\"完成\",\"用心\",\"主動\"]
- llm 區段固定：{\"enabled\":true,\"rule_weight\":0.6,\"llm_weight\":0.4,\"scoring_model\":\"gemma4:e4b\",\"summary_model\":\"gemma4:26b\"}
- forward 固定：{\"format\":\"complete\",\"summary_max_chars\":980}
- _source_brief 填入原始檔名

完成後印出處理摘要。" 2>&1 || {
        echo "⚠️  Claude 解析失敗，fallback 到 Ollama"
        python3 parse_job_brief.py
    }
else
    echo "⚪ Claude CLI 不可用，使用 Ollama (gemma4:e4b) 解析"
    python3 parse_job_brief.py || {
        echo "✗ 解析失敗，中止"
        exit 1
    }
fi

if [ $PARSE_ONLY -eq 1 ]; then
    echo "（--parse-only：跳過 run_all 與回傳結果）"
    exit 0
fi

echo
echo "============================================="
echo "🔍 Step 3: 搜尋 + 評分 + 轉寄"
echo "============================================="
./run_all.sh $DRY || echo "⚠️  run_all 部份失敗"

echo
echo "============================================="
echo "📤 Step 4: 上傳分析報告回雲端"
echo "============================================="
./upload_reports.sh

echo
echo "============================================="
echo "✓ 全部結束 $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================="
