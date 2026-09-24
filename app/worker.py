"""Background work: indexing sources and downloading media with yt-dlp."""
import glob
import logging
import os
import re
import threading
import time
import urllib.parse
from datetime import datetime, timezone

import yt_dlp

from . import db, notify

log = logging.getLogger("tubekeeper")

DATA_DIR = os.environ.get("DATA_DIR", "/config")
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/downloads")
COOKIES_FILE = os.path.join(DATA_DIR, "cookies.txt")
TEMP_DIR = os.path.join(DATA_DIR, "tmp")
# After the first full index, only the newest N items of a source are re-checked
RECHECK_LIMIT = int(os.environ.get("RECHECK_LIMIT", "50"))
OUTPUT_TEMPLATE = os.environ.get(
    "OUTPUT_TEMPLATE", "%(upload_date>%Y-%m-%d)s - %(title).150B [%(id)s].%(ext)s"
)

# Live state shown in the UI
state = {"indexing": None, "current": None}

_download_wake = threading.Event()
_index_wake = threading.Event()
_forced = set()
_forced_lock = threading.Lock()

CHANNEL_RE = re.compile(
    r"^https?://(www\.|m\.)?youtube\.com/(@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)/?$"
)
# Channel tabs list uploads newest first, so the first too-old item ends the scan
CHANNEL_TAB_RE = re.compile(r"youtube\.com/.+/(videos|shorts|streams)/?$")
LIVE_STATES = ("is_live", "is_upcoming", "post_live")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_url(url):
    """A bare channel URL returns tabs; point it at the uploads tab instead."""
    url = url.strip()
    bare = url.split("?")[0].rstrip("/")
    if CHANNEL_RE.match(bare):
        return bare + "/videos"
    return url


def name_from_url(url):
    m = re.search(r"youtube\.com/@([^/?#]+)", url)
    return urllib.parse.unquote(m.group(1)) if m else None


def safe_name(name):
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip(" .") or "source"


def source_dir(src):
    return os.path.join(DOWNLOAD_DIR, safe_name(src["name"]))


_size_cache = {}


def disk_usage(src, max_age=300):
    """Bytes used by a source's folder; cached because walking a NAS share is slow."""
    path = source_dir(src)
    hit = _size_cache.get(path)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    _size_cache[path] = (time.time(), total)
    return total


def _base_opts():
    opts = {"quiet": True, "no_warnings": True, "noprogress": True}
    if os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


def _flatten(info):
    for entry in info.get("entries") or []:
        if not entry:
            continue
        if entry.get("entries") is not None:
            yield from _flatten(entry)
        elif entry.get("ie_key") == "YoutubeTab":
            continue  # nested playlist/tab we can't expand in flat mode
        else:
            yield entry


def _entry_date(entry):
    if entry.get("upload_date"):
        return entry["upload_date"]
    ts = entry.get("timestamp") or entry.get("release_timestamp")
    if ts:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%d")
    return None


# --------------------------------------------------------------------------- indexing

def _exact_date(url):
    try:
        with yt_dlp.YoutubeDL(_base_opts()) as ydl:
            return ydl.extract_info(url, download=False, process=False).get("upload_date")
    except Exception as e:  # noqa: BLE001 - the download step checks again
        log.warning("Could not get date for %s: %s", url, e)
        return None


def index_source(src):
    state["indexing"] = src["name"]
    # Based on stored items, not last_checked: a failed first index must not count as done
    first_run = db.one("SELECT 1 FROM media WHERE source_id = ? LIMIT 1", (src["id"],)) is None
    opts = _base_opts() | {
        "extract_flat": "in_playlist",
        "skip_download": True,
        # Flat channel listings carry no dates; this derives one from "3 weeks ago".
        # That estimate is never older than the real date, so skipping on it is safe.
        "extractor_args": {"youtubetab": {"approximate_date": ["true"]}},
    }
    if not first_run and RECHECK_LIMIT > 0:
        opts["playlistend"] = RECHECK_LIMIT
    log.info("Indexing %s (%s)", src["name"], src["url"])
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(src["url"], download=False)
        entries = list(_flatten(info)) if info.get("entries") is not None else [info]

        new = 0
        cutoff = src["only_after"]
        ordered = bool(CHANNEL_TAB_RE.search(src["url"]))
        cutoff_reached = False
        for entry in entries:
            vid = entry.get("id")
            if not vid or entry.get("live_status") in LIVE_STATES:
                # Premieres/livestreams are picked up on a later check once they're finished
                continue
            url = entry.get("webpage_url") or entry.get("url") or ""
            if not url.startswith("http"):
                url = f"https://www.youtube.com/watch?v={vid}"
            if db.one("SELECT 1 FROM media WHERE source_id = ? AND video_id = ?", (src["id"], vid)):
                continue
            upload_date = _entry_date(entry)
            status = "pending"
            if first_run and not src["backfill"]:
                status = "skipped"
            elif cutoff_reached:
                status = "skipped"
            elif cutoff:
                if not upload_date and ordered:
                    # YouTube sometimes omits the "3 weeks ago" text; look the date up
                    upload_date = _exact_date(url)
                if upload_date and upload_date < cutoff:
                    status = "skipped"
                    cutoff_reached = ordered
            cur = db.execute(
                "INSERT OR IGNORE INTO media (source_id, video_id, title, url, upload_date, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (src["id"], vid, entry.get("title"), url, upload_date, status, now_iso()),
            )
            new += cur.rowcount
        db.execute(
            "UPDATE sources SET last_checked = ?, last_error = NULL WHERE id = ?",
            (now_iso(), src["id"]),
        )
        if src["name"] == src["url"]:
            title = info.get("channel") or info.get("uploader") or info.get("title")
            if title:
                db.execute("UPDATE sources SET name = ? WHERE id = ?", (title, src["id"]))
        log.info("Indexed %s: %d items, %d new", src["name"], len(entries), new)
    except Exception as e:  # noqa: BLE001 - surface every failure in the UI
        log.warning("Indexing %s failed: %s", src["name"], e)
        if not src["last_error"]:  # only on the transition to failing, not every interval
            notify.notify("index_error", f"Controle mislukt: {src['name']}", str(e)[:500], src["url"])
        db.execute(
            "UPDATE sources SET last_checked = ?, last_error = ? WHERE id = ?",
            (now_iso(), str(e)[:1000], src["id"]),
        )
    finally:
        state["indexing"] = None
        _download_wake.set()


def request_check(source_id):
    with _forced_lock:
        _forced.add(source_id)
    _index_wake.set()


def _is_due(src):
    if not src["last_checked"]:
        return True
    last = datetime.fromisoformat(src["last_checked"])
    return (datetime.now(timezone.utc) - last).total_seconds() >= src["interval_minutes"] * 60


def _scheduler_loop():
    while True:
        try:
            with _forced_lock:
                forced = set(_forced)
                _forced.clear()
            for src in db.query("SELECT * FROM sources ORDER BY id"):
                if src["id"] in forced or (src["enabled"] and _is_due(src)):
                    index_source(src)
        except Exception:  # noqa: BLE001
            log.exception("Scheduler error")
        _index_wake.wait(30)
        _index_wake.clear()


# --------------------------------------------------------------------------- downloading

def _download_opts(src, progress_hook, pp_hook):
    opts = _base_opts() | {
        "paths": {"home": source_dir(src), "temp": TEMP_DIR},
        "outtmpl": OUTPUT_TEMPLATE,
        "windowsfilenames": True,
        "writethumbnail": True,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [pp_hook],
        "postprocessors": [],
    }
    pps = opts["postprocessors"]
    if src["kind"] == "audio":
        opts["format"] = "bestaudio/best"
        pps.append({"key": "FFmpegExtractAudio", "preferredcodec": src["audio_format"]})
    else:
        if src["quality"] == "best":
            opts["format"] = "bv*+ba/b"
        else:
            h = int(src["quality"])
            # Prefer mp4/m4a for Plex/Jellyfin direct play, fall back to anything at that height
            opts["format"] = (
                f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"
            )
        opts["merge_output_format"] = "mp4"
        langs = [l.strip() for l in src["sub_langs"].split(",") if l.strip()]
        if langs:
            opts |= {"writesubtitles": True, "subtitleslangs": langs}
            pps.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False})
    pps += [
        {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
        {"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": True},
        # Keeps the .jpg next to the file so media servers pick it up as poster
        {"key": "EmbedThumbnail", "already_have_thumbnail": True},
    ]
    return opts


def download_one(media):
    src = db.one("SELECT * FROM sources WHERE id = ?", (media["source_id"],))
    if not src:
        return
    current = {"id": media["id"], "title": media["title"] or media["video_id"],
               "source": src["name"], "progress": "controleren…", "speed": "", "eta": ""}
    state["current"] = current
    result = {}

    def progress_hook(d):
        if d["status"] == "downloading":
            current["progress"] = (d.get("_percent_str") or "").strip()
            current["speed"] = (d.get("_speed_str") or "").strip()
            current["eta"] = (d.get("_eta_str") or "").strip()
        elif d["status"] == "finished":
            current["progress"] = "verwerken…"

    def pp_hook(d):
        if d["status"] == "finished" and d.get("info_dict", {}).get("filepath"):
            result["path"] = d["info_dict"]["filepath"]

    log.info("Downloading %s", current["title"])
    try:
        os.makedirs(TEMP_DIR, exist_ok=True)
        with yt_dlp.YoutubeDL(_download_opts(src, progress_hook, pp_hook)) as ydl:
            info = ydl.extract_info(media["url"], download=False)
            if info.get("live_status") in LIVE_STATES:
                # Forget it; the next index run re-adds it once the stream is finished
                db.execute("DELETE FROM media WHERE id = ?", (media["id"],))
                return
            upload_date = info.get("upload_date") or media["upload_date"]
            title = info.get("title") or media["title"]
            current["title"] = title
            if src["only_after"] and upload_date and upload_date < src["only_after"]:
                log.info("Skipping %s: uploaded %s, before %s", title, upload_date, src["only_after"])
                db.execute(
                    "UPDATE media SET status = 'skipped', title = ?, upload_date = ? WHERE id = ?",
                    (title, upload_date, media["id"]),
                )
                return
            db.execute("UPDATE media SET status = 'downloading', error = NULL, title = ?, upload_date = ? "
                       "WHERE id = ?", (title, upload_date, media["id"]))
            current["progress"] = "0%"
            ydl.process_ie_result(info, download=True)
        db.execute(
            "UPDATE media SET status = 'done', filepath = ?, downloaded_at = ? WHERE id = ?",
            (result.get("path"), now_iso(), media["id"]),
        )
        log.info("Finished %s", title)
        _size_cache.pop(source_dir(src), None)
        notify.notify("download", f"Gedownload: {src['name']}", title, media["url"])
        enforce_retention(src)
    except Exception as e:  # noqa: BLE001
        log.warning("Download of %s failed: %s", media["url"], e)
        notify.notify("download_error", f"Download mislukt: {src['name']}",
                      f"{current['title']}\n{str(e)[:400]}", media["url"])
        db.execute("UPDATE media SET status = 'error', error = ? WHERE id = ?",
                   (str(e)[:1000], media["id"]))
    finally:
        state["current"] = None


def delete_files(filepath):
    """Remove a download plus its siblings (thumbnail, subtitles)."""
    if not filepath:
        return
    _size_cache.pop(os.path.dirname(filepath), None)
    stem = os.path.splitext(filepath)[0]
    for f in glob.glob(glob.escape(stem) + ".*"):
        try:
            os.remove(f)
        except OSError as e:
            log.warning("Could not delete %s: %s", f, e)


def enforce_retention(src):
    if not src["keep_last"]:
        return
    rows = db.query(
        "SELECT id, filepath FROM media WHERE source_id = ? AND status = 'done' "
        "ORDER BY COALESCE(upload_date, '') DESC, id DESC",
        (src["id"],),
    )
    for row in rows[src["keep_last"]:]:
        delete_files(row["filepath"])
        db.execute("UPDATE media SET status = 'deleted' WHERE id = ?", (row["id"],))


def _download_loop():
    while True:
        try:
            media = db.one(
                "SELECT m.* FROM media m JOIN sources s ON s.id = m.source_id "
                "WHERE m.status = 'pending' AND s.enabled = 1 "
                "ORDER BY COALESCE(m.upload_date, '99999999'), m.id LIMIT 1"
            )
        except Exception:  # noqa: BLE001
            log.exception("Queue error")
            media = None
        if media:
            download_one(media)
            time.sleep(1)
        else:
            _download_wake.wait(30)
            _download_wake.clear()


def wake_downloads():
    _download_wake.set()


def start():
    threading.Thread(target=_scheduler_loop, name="scheduler", daemon=True).start()
    threading.Thread(target=_download_loop, name="downloader", daemon=True).start()
