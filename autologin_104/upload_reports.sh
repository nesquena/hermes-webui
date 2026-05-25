#!/usr/bin/env bash
# upload_reports.sh — 上傳分析報告到 Google Drive，資料夾用 HR 原始中文名稱
#
# 對應關係：
#   results/jidian-kaohsiung/  →  gdrive:104職缺/分析報告/機電副主任(高雄)/
#   results/chengkong/         →  gdrive:104職缺/分析報告/成控專員/
#
# 中文名稱取自 jobs/<id>.json 的 _source_brief 欄位（去掉日期和副檔名）

set -e
cd "$(dirname "$0")"

RCLONE_REMOTE="${RCLONE_REMOTE:-gdrive}"
GDRIVE_REPORT_DIR="${GDRIVE_REPORT_DIR:-104職缺/分析報告}"

uploaded=0
skipped=0

for job_json in jobs/*.json; do
    [ -f "$job_json" ] || continue

    job_id=$(python3 -c "import json; print(json.load(open('$job_json'))['job_id'])")
    source_brief=$(python3 -c "import json; print(json.load(open('$job_json')).get('_source_brief',''))")

    results_dir="results/$job_id"
    if [ ! -d "$results_dir" ]; then
        continue
    fi

    # 從 _source_brief 取中文名稱（去掉 _日期.docx / .txt / .md）
    if [ -n "$source_brief" ]; then
        chinese_name=$(echo "$source_brief" | python3 -c "
import sys, re
name = sys.stdin.read().strip()
# 去掉副檔名
name = re.sub(r'\.(docx|doc|txt|md)$', '', name)
# 去掉結尾的 _日期 (如 _20260512)
name = re.sub(r'_\d{8}$', '', name)
print(name)
")
    else
        chinese_name="$job_id"
    fi

    if [ -z "$chinese_name" ]; then
        chinese_name="$job_id"
    fi

    echo "📤 $job_id → ${GDRIVE_REPORT_DIR}/${chinese_name}/"

    rclone copy "$results_dir" "${RCLONE_REMOTE}:${GDRIVE_REPORT_DIR}/${chinese_name}" \
        --filter "+ *.md" --filter "+ forward_log.json" \
        --filter "- *" \
        --update 2>&1 | tail -5

    uploaded=$((uploaded + 1))
done

echo
echo "✓ 上傳完成：${uploaded} 個職缺報告"
