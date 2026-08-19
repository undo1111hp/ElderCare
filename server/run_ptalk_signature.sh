#!/usr/bin/env bash
# run_ptalk_signature.sh — Elder Care /device server (isolated clone of ptalk_v2)
# Reuses the SAME venv + Redis workers + Gemma as prod. Listens on :8005.
set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${ELDER_LOG:-$PROJECT_ROOT/logs/ptalk_signature.log}"
VENV="${VENV_PATH:-$PROJECT_ROOT/venv}"
PORT="${ELDER_PORT:-8005}"

echo "=================================================="
echo "🧓 PTalk Signature — Elder Care (/device)"
echo "   Port:    $PORT"
echo "   Log:     $LOG_FILE"
echo "=================================================="

pkill -f "ptalk_signature.main" 2>/dev/null || true
sleep 1

cd "$PROJECT_ROOT"
touch "$LOG_FILE" && chmod 666 "$LOG_FILE" 2>/dev/null || true

nohup bash -c "
    source '$VENV/bin/activate' 2>/dev/null || true
    export PYTHONPATH='$PROJECT_ROOT'
    export PYTHONUNBUFFERED=1
    export STT_PROVIDER=cpu
    export CUDA_VISIBLE_DEVICES=''
    exec python3 -m uvicorn ptalk_signature.main:app --host 0.0.0.0 --port $PORT
" >> "$LOG_FILE" 2>&1 &

echo "✅ Elder /device server starting on :$PORT (PID=$!)"
echo "   Logs: tail -f $LOG_FILE"
