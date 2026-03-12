FROM runpod/worker-comfyui:5.5.1-base

WORKDIR /app

RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY workflow_api.json /app/workflow_api.json
COPY handler.py /app/handler.py
COPY start.sh /app/start.sh
COPY serviceAccountKey.json /app/serviceAccountKey.json

RUN chmod +x /app/start.sh

RUN comfy model download --url https://huggingface.co/Kim2091/UltraSharp/resolve/main/4x-UltraSharp.pth --relative-path models/upscale_models --filename 4x-UltraSharp.pth

CMD ["/app/start.sh"]

