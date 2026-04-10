#!/usr/bin/env bash
# scripts/pull-model.sh — pull the Qwen model into the Ollama container
#
# Run this once after first bringing up docker compose:
#   docker compose up -d ollama
#   bash scripts/pull-model.sh
#
# The model is stored in the ollama-models Docker volume and persists
# across container restarts.

set -euo pipefail

MODEL="${OLLAMA_MODEL:-qwen2.5:7b-instruct-q4_K_M}"

echo "==> Waiting for Ollama to be ready..."
until docker compose exec ollama curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; do
    echo "    Ollama not ready yet, retrying in 3s..."
    sleep 3
done

echo "==> Pulling model: ${MODEL}"
docker compose exec ollama ollama pull "${MODEL}"

echo ""
echo "Done! Verify with: docker compose exec ollama ollama list"
