#!/usr/bin/env bash
set -eu

BASE_URL="${1:-http://127.0.0.1:4000}"

echo "[1/2] Health check: ${BASE_URL}/health"
curl -fsS "${BASE_URL}/health"
echo
echo

echo "[2/2] Minimal /v1/messages request"
curl -fsS "${BASE_URL}/v1/messages" \
  -H 'content-type: application/json' \
  -d '{
    "model": "claude-compatible",
    "max_tokens": 64,
    "messages": [
      {
        "role": "user",
        "content": "Reply with exactly: ok"
      }
    ]
  }'
echo
