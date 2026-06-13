#!/usr/bin/env bash
# run_all.sh — 一鍵跑所有職缺：搜尋 → 評分 → 轉寄
#
# 用法：
#   ./run_all.sh                  # 跑所有 jobs/*.json
#   ./run_all.sh --dry-run        # 只分析不轉寄
#   ./run_all.sh --keep-chrome    # 全部跑完後保留 Chrome（預設會關閉自動化 Chrome）
#   ./run_all.sh --skip-search    # 完全跳過搜尋階段（用既有 candidates）
#   ./run_all.sh --force-search   # 強制重搜（即便今日已搜過）
#   ./run_all.sh jidian-tainan jidian-kaohsiung   # 只跑指定 job
#
# 預設行為：今日已搜過的 job 會自動跳過搜尋階段（避免每個 job 都重啟 Chrome）
#
# 注意：
#   - 每個 job 跑完後會有人選清單可供下一個 job 接手 Chrome session
#   - 結束時只關掉 browser-use 自動化用的 Chrome，不影響你日常開的 Chrome

set -e
cd "$(dirname "$0")"

DRY=""
KEEP_CHROME=0
SKIP_SEARCH=0
FORCE_SEARCH=0
JOBS=()
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY="--dry-run" ;;
        --keep-chrome) KEEP_CHROME=1 ;;
        --skip-search) SKIP_SEARCH=1 ;;       # 完全跳過搜尋（用既有 candidates）
        --force-search) FORCE_SEARCH=1 ;;     # 強制重搜（即便今日已搜過）
        *) JOBS+=("$arg") ;;
    esac
done

# 判斷今日是否已搜過該 job
is_searched_today() {
    local job=$1
    local f="results/${job}/candidates_raw.json"
    [ -f "$f" ] || return 1
    local today=$(date +%Y-%m-%d)
    local file_date=$(stat -f "%Sm" -t "%Y-%m-%d" "$f" 2>/dev/null)
    [ "$today" = "$file_date" ]
}

# 檢查是否有可用的 Chrome CDP（直接戳 port 確認，不靠 ps grep）
has_cdp_chrome() {
    # 嘗試常見 CDP port（9222 / 9223），有任一個回應就算有
    for port in 9222 9223 9224; do
        if curl -s --max-time 1 "http://localhost:${port}/json/version" 2>/dev/null | grep -q '"Browser"'; then
            return 0
        fi
    done
    # 退而求其次：用 ps 但排除 grep 自己（命令列含 'Google Chrome' 才算）
    ps -axo command= 2>/dev/null | grep -E "Google Chrome.*--remote-debugging-port=" | grep -v grep >/dev/null && return 0
    return 1
}

# 啟動帶 CDP 的 Chrome（載入 Profile 4 並開啟 104 後台）
ensure_cdp_chrome() {
    if has_cdp_chrome; then
        echo "  ✓ 已偵測到 CDP Chrome"
        return 0
    fi
    echo "  ▶ 沒有 CDP Chrome，啟動中..."
    # 用 Profile 4（已有 104 登入 session）
    local profile_name="Profile 4"
    local profile_src="$HOME/Library/Application Support/Google/Chrome/$profile_name"
    local tmp_dir
    tmp_dir=$(mktemp -d /tmp/chrome-cdp-forward-XXXXXX)
    cp -R "$profile_src" "$tmp_dir/$profile_name" 2>/dev/null || true
    cp "$HOME/Library/Application Support/Google/Chrome/Local State" "$tmp_dir/" 2>/dev/null || true

    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
        --remote-debugging-port=9222 \
        --user-data-dir="$tmp_dir" \
        --profile-directory="$profile_name" \
        --no-first-run \
        --disable-default-apps \
        --disable-popup-blocking \
        "https://vip.104.com.tw/rms/index" &>/dev/null &
    CDP_CHROME_PID=$!
    echo "  ✓ Chrome 已啟動 (PID=$CDP_CHROME_PID, port=9222)"
    echo "  ⏳ 等待頁面載入..."
    sleep 10
}

CDP_CHROME_PID=""

# 只關閉 browser-use 自動化的 Chrome（含 user-data-dir 帶 browser-use 字樣）
close_automation_chrome() {
    local pids
    # macOS pgrep 不支援 --full（POSIX），用 ps + grep 過濾
    pids=$(ps -axo pid=,command= | grep -E "Google Chrome.*browser-use-user-data-dir" | grep -v grep | awk '{print $1}')
    if [ -z "$pids" ]; then
        echo "  （沒有自動化 Chrome 在跑）"
        return 0
    fi
    echo "  關閉自動化 Chrome PIDs: $pids"
    for p in $pids; do
        kill "$p" 2>/dev/null || true
    done
    sleep 2
    # 還沒關掉的強制 kill
    pids=$(ps -axo pid=,command= | grep -E "Google Chrome.*browser-use-user-data-dir" | grep -v grep | awk '{print $1}')
    if [ -n "$pids" ]; then
        for p in $pids; do
            kill -9 "$p" 2>/dev/null || true
        done
    fi
}

# 預設跑全部
if [ ${#JOBS[@]} -eq 0 ]; then
    JOBS=()
    for f in jobs/*.json; do
        JOBS+=("$(basename "$f" .json)")
    done
fi

echo "============================================="
echo "Plan A 多職缺自動化"
echo "  jobs : ${JOBS[*]}"
echo "  dry  : ${DRY:-false}"
echo "============================================="

for job in "${JOBS[@]}"; do
    echo
    if [ $SKIP_SEARCH -eq 1 ]; then
        echo "▶▶▶ [$job] 階段一：略過搜尋（--skip-search）"
        if [ ! -f "results/${job}/candidates_raw.json" ]; then
            echo "✗ 沒有 candidates_raw.json 可用，跳過 $job"
            continue
        fi
    elif is_searched_today "$job" && [ $FORCE_SEARCH -eq 0 ]; then
        echo "▶▶▶ [$job] 階段一：今日已搜過 → 跳過（用 --force-search 可強制重搜）"
    else
        echo "▶▶▶ [$job] 階段一：搜尋"
        if ! python3 run_search.py --job "$job"; then
            echo "✗ search 失敗"
            # search 失敗但若 candidates_raw.json 存在（即使 0 人），仍進入評分階段產出 0 人報告
            if [ ! -f "results/${job}/candidates_raw.json" ]; then
                echo "✗ 無 candidates_raw.json，跳過 $job"
                continue
            fi
            echo "  candidates_raw.json 存在，仍進入評分階段（可能會產出 0 人提示報告）"
        fi
    fi

    echo
    echo "▶▶▶ [$job] 階段二：評分 + 轉寄"
    # 確保有 CDP Chrome 可用（搜尋跳過時 Chrome 可能沒在跑）
    if [ -f "results/${job}/candidates_raw.json" ]; then
        count=$(python3 -c "import json; d=json.load(open('results/${job}/candidates_raw.json')); print(d.get('count',len(d.get('candidates',[]))))" 2>/dev/null || echo "0")
        if [ "$count" -gt 0 ] && ! has_cdp_chrome; then
            ensure_cdp_chrome
        fi
    fi
    python3 auto_forward.py --job "$job" $DRY || echo "✗ auto_forward 失敗"
done

echo
echo "============================================="
echo "✓ 全部 jobs 完成"
echo "  結果在 results/<job>/analysis_*.md 與 forward_log.json"

if [ $KEEP_CHROME -eq 0 ]; then
    echo
    echo "▶ 關閉自動化 Chrome..."
    close_automation_chrome
    # 也關閉我們啟動的 CDP Chrome
    if [ -n "$CDP_CHROME_PID" ]; then
        echo "  關閉 CDP Chrome (PID=$CDP_CHROME_PID)..."
        kill "$CDP_CHROME_PID" 2>/dev/null || true
        sleep 2
        kill -9 "$CDP_CHROME_PID" 2>/dev/null || true
        # 清掉 temp profile
        ps -p "$CDP_CHROME_PID" &>/dev/null || rm -rf /tmp/chrome-cdp-forward-* 2>/dev/null
    fi
    echo "✓ 完成"
else
    echo
    echo "（--keep-chrome 已設，保留 Chrome 不關閉）"
fi
