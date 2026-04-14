#!/usr/bin/env bash
# docker-entrypoint.sh — Pre-flight checks then start server
set -euo pipefail

echo "═══════════════════════════════════════════════"
echo "  Meeting Realtime Voice — Docker Container"
echo "═══════════════════════════════════════════════"

# ── 2. Load .env (only set vars not already provided by Docker) ──
# Docker -e / environment: take precedence over .env file
# So we only set variables that are not already in the environment
if [ -f .env ]; then
    while IFS='=' read -r key value; do
        # Skip comments and empty lines
        [[ -z "$key" || "$key" == \#* ]] && continue
        # Only set if not already defined (Docker env takes priority)
        if [ -z "${!key:-}" ]; then
            export "$key=$value"
        fi
    done < .env
fi

# ── 3. Check Ollama connectivity (skip if using OpenAI) ──
OLLAMA_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"
OPENAI_BASE="${OPENAI_BASE_URL:-}"

if [ -z "$OPENAI_BASE" ]; then
    echo "[CHECK] Waiting for Ollama at ${OLLAMA_URL} ..."
    max_wait=60
    elapsed=0
    while ! curl -sf "${OLLAMA_URL}/api/tags" >/dev/null 2>&1; do
        if [ "$elapsed" -ge "$max_wait" ]; then
            echo "[ERROR] Ollama not reachable at ${OLLAMA_URL} after ${max_wait}s"
            echo "        If using docker compose, ensure the ollama service is running."
            echo "        Otherwise set OLLAMA_BASE_URL to the correct address."
            exit 1
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done
    echo "[OK] Ollama is reachable"

    # Check/pull model
    MODEL="${OLLAMA_MODEL:-qwen3.5:9b}"
    MODEL_NAME="${MODEL%%:*}"
    if ! curl -sf "${OLLAMA_URL}/api/tags" | python3 -c "
import sys, json
tags = json.load(sys.stdin).get('models', [])
names = [m.get('name','').split(':')[0] for m in tags]
sys.exit(0 if '$MODEL_NAME' in names else 1)
" 2>/dev/null; then
        echo "[INFO] Model ${MODEL} not found locally — please pull it first:"
        echo "       docker compose exec ollama ollama pull ${MODEL}"
        echo "       or:  ollama pull ${MODEL}"
        echo "[WARN] Continuing anyway; the server will fail on LLM calls until the model is available."
    else
        echo "[OK] Ollama model ${MODEL} is available"
    fi
else
    echo "[OK] Using OpenAI-compatible API: ${OPENAI_BASE}"
fi

# ── 4. Check ASR model ──
ASR_PATH="${ASR_MODEL_PATH:-/models/Qwen3-ASR-1.7B}"
ASR_PATH="${ASR_PATH/#\~/$HOME}"
if [ ! -d "$ASR_PATH" ]; then
    echo "[ERROR] ASR model not found at: ${ASR_PATH}"
    echo "        Mount the model directory when starting the container:"
    echo "        docker compose up   (with ASR_MODEL_PATH volume configured)"
    echo "        or:  docker run -v /path/to/models:/models ..."
    exit 1
fi
echo "[OK] ASR model found at ${ASR_PATH}"

# ── 5. Create data directories ──
mkdir -p "${MEETING_RECORDINGS_DIR:-recordings}"
mkdir -p "$(dirname "${MEETING_DB_PATH:-data/meeting_realtime_voice.sqlite3}")"

echo ""
echo "───────────────────────────────────────────────"
echo "  Starting server on http://${HOST:-0.0.0.0}:${PORT:-8800}"
echo "───────────────────────────────────────────────"
echo ""

exec python server.py
