#!/bin/sh
set -e

# YouTube changes often; a fresh yt-dlp on every start avoids most breakage
if [ "${YTDLP_AUTO_UPDATE:-true}" = "true" ]; then
  echo "Updating yt-dlp..."
  pip install --no-cache-dir --quiet --root-user-action=ignore --disable-pip-version-check --upgrade "yt-dlp[default]" || echo "yt-dlp update failed, continuing with installed version"
fi

mkdir -p "$DATA_DIR" "$DOWNLOAD_DIR"
CMD="uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8945}"

if [ -n "$PUID" ] && [ -n "$PGID" ]; then
  chown -R "$PUID:$PGID" "$DATA_DIR"
  chown "$PUID:$PGID" "$DOWNLOAD_DIR" 2>/dev/null || true
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups env HOME="$DATA_DIR" $CMD
fi

exec $CMD
