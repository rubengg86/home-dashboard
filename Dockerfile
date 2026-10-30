# Build the mobile app as a PWA served by the backend at /app.
# node 24 ships npm 11, which wrote package-lock.json; npm 10 (node 22) rejects it.
FROM node:24-slim AS pwa
WORKDIR /build
COPY app/package.json app/package-lock.json ./
RUN npm ci
COPY app/ .
# Stamp the build (shown on the login and home screens). The layer is cached together with
# the build, so the stamp only changes when the app's code changes.
RUN printf "export const BUILD = 'v%s';\n" "$(date -u +'%Y-%m-%d %H:%M') UTC" > src/environments/version.ts \
    && npx ng build --configuration production --base-href /app/

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
COPY --from=pwa /build/www ./app-www

# config + state live in the mounted /data volume (see docker-compose.yml)
ENV DATA_DIR=/data \
    PORT=8811 \
    PYTHONUNBUFFERED=1
VOLUME /data

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8811\")}/healthz', timeout=4)"

CMD ["python", "app.py"]
