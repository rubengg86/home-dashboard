FROM python:3.12-slim

# ffmpeg: live camera video (RTSP -> MJPEG); tzdata: local hours/days for the energy chart
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY static ./static

# config + state live in the mounted /data volume (see docker-compose.yml)
ENV DATA_DIR=/data \
    PORT=8811 \
    PYTHONUNBUFFERED=1
VOLUME /data

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8811\")}/healthz', timeout=4)"

CMD ["python", "app.py"]
