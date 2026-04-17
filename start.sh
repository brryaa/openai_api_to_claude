#!/usr/bin/env bash
set -eu

cd "$(dirname "$0")"

while true; do
  echo "=========================="
  echo "Starting FastAPI Proxy..."
  echo "=========================="

  python3 -m uvicorn openaitoclaude:app --host 0.0.0.0 --port 4000 --workers 1

  echo
  echo "Process crashed. Restarting in 5 seconds..."
  sleep 5
done
