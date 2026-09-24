FROM python:3.12-slim

# ffmpeg: merging/converting, tzdata: correct local times, deno: JS runtime yt-dlp needs for YouTube
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tzdata tini \
 && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
ARG VERSION=dev
ENV APP_VERSION=$VERSION
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV DATA_DIR=/config \
    DOWNLOAD_DIR=/downloads \
    DENO_DIR=/config/.deno \
    XDG_CACHE_HOME=/config/.cache \
    PYTHONUNBUFFERED=1

EXPOSE 8945
VOLUME ["/config", "/downloads"]
HEALTHCHECK --interval=1m --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8945/healthz')"

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
