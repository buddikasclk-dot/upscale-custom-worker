#!/bin/bash
set -e

echo "Starting ComfyUI..."
python /comfyui/main.py --listen 0.0.0.0 --port 8188 &

echo "Waiting for ComfyUI to be ready..."
until curl -s http://127.0.0.1:8188/system_stats > /dev/null; do
  sleep 2
done

echo "ComfyUI is ready. Starting RunPod handler..."
python /app/handler.py
