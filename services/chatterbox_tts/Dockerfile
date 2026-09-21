FROM python:3.11-slim-bookworm

ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models/huggingface \
    HF_HUB_CACHE=/models/huggingface/hub

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ffmpeg git libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /service

COPY requirements.txt .

# Torch 2.7 with CUDA 12.8 is required for RTX 50-series (sm_120) support.
RUN pip install torch==2.7.1 torchaudio==2.7.1 --index-url "${TORCH_INDEX_URL}" \
    && pip install --no-deps chatterbox-tts==0.1.7 \
    && pip install -r requirements.txt

COPY app.py .

RUN useradd --create-home --uid 10001 tts \
    && mkdir -p /models/huggingface /voices \
    && chown -R tts:tts /service /models/huggingface

USER tts

EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --start-period=10m --retries=3 \
    CMD curl --fail --silent http://127.0.0.1:8100/health || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8100", "--workers", "1"]
