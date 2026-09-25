"""Import YouTube subscriptions from the account (via cookies.txt) or a Google Takeout CSV."""
import csv
import io
import json
import logging
import os
import re
import time

import yt_dlp

from . import db, notify, worker

log = logging.getLogger("tubekeeper")

CHANNEL_ID_RE = re.compile(r"UC[\w-]{22}")
SYNC_EVERY = 6 * 3600
# Defaults for imported channels: only new uploads, so an import never starts a mass download
DEFAULTS = {
    "kind": "video", "quality": "1080", "audio_format": "m4a", "interval_minutes": 60,
    "only_after": None, "keep_last": None, "sub_langs": "", "lang": "", "layout": "series",
    "backfill": 0, "enabled": 1, "redownload_missing": 0,
}


def _setting(key, default=None):
    row = db.one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def _set_setting(key, value):
    db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
               "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))


def get_defaults():
    try:
        return DEFAULTS | json.loads(_setting("import_defaults", "{}"))
    except ValueError:
        return dict(DEFAULTS)


def save_defaults(values):
    _set_setting("import_defaults", json.dumps({k: values[k] for k in DEFAULTS if k in values}))


def sync_enabled():
    return _setting("subs_sync", "0") == "1"


def set_sync(enabled):
    _set_setting("subs_sync", "1" if enabled else "0")


def last_sync():
    return _setting("subs_last_sync")


def save_cookies(data):
    os.makedirs(os.path.dirname(worker.COOKIES_FILE), exist_ok=True)
    with open(worker.COOKIES_FILE, "wb") as f:
        f.write(data)
    os.chmod(worker.COOKIES_FILE, 0o600)


def _channel(channel_id, title, handle=None):
    url = f"https://www.youtube.com/{handle}" if handle else f"https://www.youtube.com/channel/{channel_id}"
    return {"id": channel_id, "title": title or handle or channel_id, "url": worker.normalize_url(url)}


def fetch_account():
    """Subscribed channels of the account whose cookies are in /config/cookies.txt."""
    if not os.path.exists(worker.COOKIES_FILE):
        raise RuntimeError("Nog geen cookies.txt: upload die eerst hieronder.")
    opts = worker._base_opts() | {"extract_flat": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info("https://www.youtube.com/feed/channels", download=False)
    channels = []
    for e in info.get("entries") or []:
        cid = e.get("channel_id") or e.get("id")
        if not cid or not CHANNEL_ID_RE.fullmatch(cid):
            continue
        handle = e.get("uploader_id") if (e.get("uploader_id") or "").startswith("@") else None
        channels.append(_channel(cid, e.get("title") or e.get("channel"), handle))
    if not channels:
        raise RuntimeError("Geen abonnementen gevonden. Zijn de cookies verlopen of van een ander account?")
    return channels


def parse_takeout(data):
    """subscriptions.csv from Google Takeout (column names differ per language, so match on content)."""
    text = data.decode("utf-8-sig", errors="replace")
    channels, seen = [], set()
    for row in csv.reader(io.StringIO(text)):
        cid = next((m.group(0) for cell in row for m in [CHANNEL_ID_RE.search(cell)] if m), None)
        if not cid or cid in seen:
            continue
        title = next((c.strip() for c in reversed(row) if c.strip() and "youtube.com" not in c
                      and not CHANNEL_ID_RE.fullmatch(c.strip())), cid)
        seen.add(cid)
        channels.append(_channel(cid, title))
    if not channels:
        raise RuntimeError("Geen kanalen gevonden in dit bestand. Kies subscriptions.csv uit de Takeout-export.")
    return channels


def mark_existing(channels):
    """Flag channels that already are a source (matched on channel id, handle URL or channel URL)."""
    rows = db.query("SELECT channel_id, url FROM sources")
    ids = {r["channel_id"] for r in rows if r["channel_id"]}
    urls = {r["url"].lower() for r in rows}
    for c in channels:
        c["exists"] = c["id"] in ids or c["url"].lower() in urls or any(c["id"] in u for u in urls)
    return sorted(channels, key=lambda c: (c["exists"], c["title"].lower()))


def add_channels(channels, settings):
    added = 0
    for c in mark_existing(channels):
        if c["exists"]:
            continue
        db.execute(
            "INSERT INTO sources (name, url, channel_id, kind, quality, audio_format, interval_minutes, "
            "only_after, keep_last, sub_langs, lang, layout, backfill, enabled, redownload_missing, created_at) "
            "VALUES (:name, :url, :channel_id, :kind, :quality, :audio_format, :interval_minutes, "
            ":only_after, :keep_last, :sub_langs, :lang, :layout, :backfill, :enabled, :redownload_missing, :created_at)",
            settings | {"name": c["title"], "url": c["url"], "channel_id": c["id"], "created_at": worker.now_iso()},
        )
        added += 1
    return added


def sync_if_due():
    """Called from the scheduler: add new subscriptions with the saved defaults."""
    if not sync_enabled():
        return
    last = last_sync()
    if last and time.time() - float(last) < SYNC_EVERY:
        return
    _set_setting("subs_last_sync", str(time.time()))
    try:
        channels = fetch_account()
    except Exception as e:  # noqa: BLE001
        log.warning("Subscription sync failed: %s", e)
        notify.notify("index_error", "Abonnementen ophalen mislukt", str(e)[:400])
        return
    new = [c for c in mark_existing(channels) if not c["exists"]]
    if new:
        add_channels(new, get_defaults())
        names = ", ".join(c["title"] for c in new[:10]) + (" …" if len(new) > 10 else "")
        log.info("Subscription sync added %d channels", len(new))
        notify.notify("download", f"{len(new)} nieuwe abonnement(en) toegevoegd", names)
