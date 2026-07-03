FROM python:3.11-slim

LABEL org.opencontainers.image.title="iMessage Search (fork)"
LABEL org.opencontainers.image.description="Self-hosted iMessage search and browser, adapted for a -c disabled export layout (Attachments/StickerCache instead of attachments/)"
LABEL org.opencontainers.image.source="https://github.com/mbaran5/imessage-exporter-viewer"
LABEL org.opencontainers.image.licenses="MIT"

# Install libheif for HEIC support
RUN apt-get update && apt-get install -y \
    libheif-dev \
    libde265-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# torch CPU-only — the default PyPI wheel bundles CUDA (~2.5 GB)
RUN pip install --no-cache-dir torch torchvision \
    --index-url https://download.pytorch.org/whl/cpu
# ml-mobileclip has no PyPI release; install directly from GitHub
RUN pip install --no-cache-dir git+https://github.com/apple/ml-mobileclip.git
RUN pip install --no-cache-dir -r requirements.txt

COPY indexer.py app.py start.sh ./
RUN chmod +x start.sh

VOLUME ["/data", "/archives"]
EXPOSE 6333
CMD ["/app/start.sh"]
