FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PYANNOTE_METRICS_ENABLED=0
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates python3 python3-pip ffmpeg git \
    && rm -rf /var/lib/apt/lists/*

ARG AASIST_COMMIT=a04c9863f63d44471dde8a6abcb3b082b07cd1d1
RUN git clone https://github.com/clovaai/aasist.git /opt/aasist \
    && cd /opt/aasist \
    && git checkout "$AASIST_COMMIT" \
    && test -s models/AASIST.py \
    && test -s models/weights/AASIST.pth \
    && rm -rf .git

WORKDIR /service
COPY requirements.txt .
RUN python3 -m pip install --no-cache-dir \
        torch==2.9.0 torchaudio==2.9.0 torchcodec==0.9.0 \
        --index-url https://download.pytorch.org/whl/cu128 \
    && python3 -m pip install --no-cache-dir -r requirements.txt
COPY app.py admin_antispoof.py ./

EXPOSE 8110
CMD ["python3", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8110"]
