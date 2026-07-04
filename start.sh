#!/bin/bash
set -e

echo "=== iMessage Search ==="
echo "Running indexer..."
python3 /app/indexer.py

echo ""
echo "Starting image embedder in background (first run may take 1-2 hours)..."
python3 /app/indexer.py embed &

echo "Starting file-change watcher in background (polls for updates from an external sync pipeline)..."
python3 /app/indexer.py watch &

echo "Starting web server on port 6333..."
exec python3 /app/app.py
