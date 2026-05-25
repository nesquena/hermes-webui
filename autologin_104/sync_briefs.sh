#!/usr/bin/env bash
# sync_briefs.sh — 從 Google Drive 同步 HR 寫的職缺需求，自動解析並執行
#
# 流程：
#   1. rclone 從雲端同步 jobs_brief/
#   2. parse_job_brief.py 把 .txt 轉成 jobs/<id>.json
#   3. run_all.sh 跑搜尋 + 評分 + 轉寄
#   4. 結果 markdown 上傳回雲端讓 HR 看
#
# 用法：
#   ./sync_briefs.sh              # 完整流程
#   ./sync_briefs.sh --dry-run    # 只到分析（不轉寄）
#   ./sync_briefs.sh --parse-only # 只同步 + 解析（不跑 run_all）

set -e
cd "$(dirname "$0")"

# ===== 可調參數 =====
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

echo "============================================="
echo "📥 Step 1: 從雲端同步職缺需求（只抓「進行中」職缺，不抓「已完成」子資料夾）"
echo "============================================="
echo "  ${RCLONE_REMOTE}:${GDRIVE_BRIEF_DIR}/  →  ${LOCAL_BRIEF_DIR}/"

mkdir -p "$LOCAL_BRIEF_DIR"
# 先清除本地 jobs_brief 中沒在雲端 root 的 .txt/.md（讓「移到已完成」生效）
rclone sync "${RCLONE_REMOTE}:${GDRIVE_BRIEF_DIR}" "$LOCAL_BRIEF_DIR" \
    --max-depth 1 \
    --filter "+ *.txt" --filter "+ *.md" \
    --filter "+ *.docx" --filter "+ *.doc" \
    --filter "- _*" --filter "- ~$*" --filter "- *.errors.txt" \
    --filter "- *" 2>&1 | tail -10
echo "✓ 同步完成"

echo
echo "============================================="
echo "📦 Step 1.5: 封存「已完成」職缺（brief 已不在雲端根目錄者）"
echo "============================================="
python3 archive_orphans.py --apply || true

echo
echo "============================================="
echo "🤖 Step 2: 解析自然語言為 JSON"
echo "============================================="
python3 parse_job_brief.py || {
    echo "✗ 解析失敗，中止"
    exit 1
}

if [ $PARSE_ONLY -eq 1 ]; then
    echo "（--parse-only：跳過 run_all 與回傳結果）"
    exit 0
fi

echo
echo "============================================="
echo "🔍 Step 3: 跑搜尋 + 評分 + 轉寄"
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
