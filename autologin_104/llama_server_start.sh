#!/usr/bin/env bash
# llama_server_start.sh — 啟動 llama-server (Qwen2.5-14B 主 + 1.5B draft，speculative decoding)
#
# 用法：
#   ./llama_server_start.sh           # 啟動（含 speculative）
#   ./llama_server_start.sh --no-spec # 啟動但不用 speculative（純 14B）
#   ./llama_server_start.sh --gemma   # 回退到舊版 gemma-4-12b
#
# 停止：pkill -f llama-server

set -e

MODELS_DIR="${MODELS_DIR:-$HOME/models}"
QWEN14B="$MODELS_DIR/qwen2.5-14b/Qwen2.5-14B-Instruct-Q4_K_M.gguf"
QWEN7B="$MODELS_DIR/qwen2.5-7b/Qwen2.5-7B-Instruct-Q4_K_M.gguf"
QWEN15B="$MODELS_DIR/qwen2.5-1.5b/Qwen2.5-1.5B-Instruct-Q4_K_M.gguf"
GEMMA12B="$MODELS_DIR/gemma-4-12b-it/gemma-4-12b-it-Q4_K_M.gguf"
# 預設用 7B 純模式（最穩，不 OOM）。Mac mini GPU 記憶體有限，
# 14B+speculative 處理長履歷會 OOM crash。
MAIN_MODEL="${MAIN_MODEL:-$QWEN7B}"

# 預設 7b：純 7B 無 speculative（最穩定）
# --spec   : 14B + 1.5B speculative（品質最佳但可能 OOM，需 GPU 記憶體充足）
# --14b    : 純 14B 無 speculative
# --gemma  : 回退 gemma-4-12b
MODE="7b"
for arg in "$@"; do
    case "$arg" in
        --spec) MODE="speculative"; MAIN_MODEL="$QWEN14B" ;;
        --14b) MODE="single"; MAIN_MODEL="$QWEN14B" ;;
        --no-spec) MODE="single"; MAIN_MODEL="$QWEN14B" ;;
        --gemma) MODE="gemma" ;;
    esac
done

# 先停掉現有 llama-server
echo "停止既有 llama-server..."
pkill -f llama-server 2>/dev/null || true
sleep 2

case "$MODE" in
    speculative)
        [ ! -f "$MAIN_MODEL" ] && { echo "✗ 找不到 $MAIN_MODEL"; exit 1; }
        [ ! -f "$QWEN15B" ] && { echo "✗ 找不到 $QWEN15B"; exit 1; }
        echo "啟動 Qwen2.5-14B + 1.5B draft (speculative decoding)..."
        echo "  主模型: $MAIN_MODEL"
        echo "  draft : $QWEN15B"
        # 14B + 1.5B + ctx 6144 + KV q4_0 ≈ 11.5 GB GPU 記憶體
        nohup llama-server \
            -m "$MAIN_MODEL" \
            -md "$QWEN15B" \
            --port 8080 \
            --ctx-size 6144 \
            -ngl 99 \
            -ngld 99 \
            --spec-draft-n-max 16 \
            --spec-draft-n-min 4 \
            --cache-type-k q4_0 \
            --cache-type-v q4_0 \
            > /tmp/llama-server.log 2>&1 &
        ;;
    7b)
        [ ! -f "$QWEN7B" ] && { echo "✗ 找不到 $QWEN7B"; exit 1; }
        echo "啟動 純 Qwen2.5-7B (無 speculative，最穩，不 OOM)..."
        # 7B + ctx 8192 + KV q8_0 ≈ 6.5 GB GPU（留充足餘裕）
        nohup llama-server \
            -m "$QWEN7B" \
            --port 8080 \
            --ctx-size 8192 \
            -ngl 99 \
            --cache-type-k q8_0 \
            --cache-type-v q8_0 \
            > /tmp/llama-server.log 2>&1 &
        ;;
    single)
        [ ! -f "$MAIN_MODEL" ] && { echo "✗ 找不到 $MAIN_MODEL"; exit 1; }
        echo "啟動 純 14B (無 speculative，省記憶體)..."
        # 14B + ctx 4096 + KV q4_0 ≈ 10 GB GPU
        nohup llama-server \
            -m "$MAIN_MODEL" \
            --port 8080 \
            --ctx-size 4096 \
            -ngl 99 \
            --cache-type-k q4_0 \
            --cache-type-v q4_0 \
            > /tmp/llama-server.log 2>&1 &
        ;;
    gemma)
        [ ! -f "$GEMMA12B" ] && { echo "✗ 找不到 $GEMMA12B"; exit 1; }
        echo "啟動 Gemma-4-12B (舊版回退)..."
        nohup llama-server \
            -m "$GEMMA12B" \
            --port 8080 \
            --ctx-size 8192 \
            -ngl 99 \
            > /tmp/llama-server.log 2>&1 &
        ;;
esac

SERVER_PID=$!
echo "PID: $SERVER_PID"
echo "log: /tmp/llama-server.log"

echo "等待 server 就緒..."
for i in {1..60}; do
    sleep 2
    if curl -s -m 2 http://localhost:8080/health > /dev/null 2>&1; then
        echo "✓ llama-server 就緒 (${i}×2s)"
        exit 0
    fi
done

echo "✗ 60×2s 仍未就緒，看 log："
tail -20 /tmp/llama-server.log
exit 1
