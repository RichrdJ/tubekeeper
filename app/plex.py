"""Write YouTube titles, descriptions, dates and channel posters into Plex via its HTTP API.

Plex's "Personal Media Shows" agent ignores titles in file names and embedded tags, so
episodes show up as "Episode 09-20". Episodes are matched on the [videoId] in the file
name, which works regardless of how Plex's mount paths differ from ours.
"""
import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request

from . import db

log = logging.getLogger("tubekeeper")

VIDEO_ID_RE = re.compile(r"\[([\w-]{11})\]\.\w+$")
KEYS = ("plex_url", "plex_token", "plex_section")
SYNC_EVERY = 600

state = {"last_sync": None, "last_result": None}
_wake = threading.Event()
_lock = threading.Lock()


def settings():
    rows = {r["key"]: r["value"] for r in db.query(
        "SELECT key, value FROM settings WHERE key IN (?, ?, ?)", KEYS)}
    return {k: rows.get(k, "") for k in KEYS}


def save(values):
    with db.tx() as c:
        for key in KEYS:
            if key in values:
                c.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                          "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, values[key]))


def enabled(s=None):
    s = s or settings()
    return all(s[k] for k in KEYS)


def _request(method, path, params=None, s=None):
    s = s or settings()
    url = s["plex_url"].rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method=method,
                                 headers={"X-Plex-Token": s["plex_token"], "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
    return json.loads(body) if body.strip().startswith(b"{") else {}


def sections(s=None):
    """TV libraries on the server as [(key, title)]."""
    dirs = _request("GET", "/library/sections", s=s).get("MediaContainer", {}).get("Directory", [])
    return [(d["key"], d["title"]) for d in dirs if d.get("type") == "show"]


def _episodes(s):
    start, size = 0, 500
    while True:
        box = _request("GET", f"/library/sections/{s['plex_section']}/all",
                       {"type": 4, "X-Plex-Container-Start": start, "X-Plex-Container-Size": size}, s)
        box = box.get("MediaContainer", {})
        items = box.get("Metadata", [])
        yield from items
        start += len(items)
        if not items or start >= box.get("totalSize", start):
            return


def _video_id(item):
    for media in item.get("Media", []):
        for part in media.get("Part", []):
            m = VIDEO_ID_RE.search(part.get("file", ""))
            if m:
                return m.group(1)
    return None


def _iso(date):
    return f"{date[:4]}-{date[4:6]}-{date[6:8]}" if date and len(date) == 8 else None


def sync():
    """Returns (episodes updated, posters set)."""
    s = settings()
    if not enabled(s):
        return 0, 0
    with _lock:
        rows = {r["video_id"]: r for r in db.query(
            "SELECT m.video_id, m.title, m.description, m.upload_date, s.avatar_url "
            "FROM media m JOIN sources s ON s.id = m.source_id WHERE m.status = 'done'")}
        updated, shows = 0, {}
        for ep in _episodes(s):
            row = rows.get(_video_id(ep))
            if not row:
                continue
            if ep.get("grandparentRatingKey"):
                shows.setdefault(ep["grandparentRatingKey"], row["avatar_url"])
            want = {"title": row["title"], "summary": row["description"],
                    "originallyAvailableAt": _iso(row["upload_date"])}
            changes = {k: v for k, v in want.items() if v and ep.get(k) != v}
            if not changes:
                continue
            params = {"type": 4, "id": ep["ratingKey"]}
            for key, value in changes.items():
                params[f"{key}.value"] = value
                params[f"{key}.locked"] = 1  # a metadata refresh in Plex must not undo this
            _request("PUT", f"/library/sections/{s['plex_section']}/all", params, s)
            updated += 1
        posters = 0
        for show_key, avatar in shows.items():
            if not avatar:
                continue
            show = _request("GET", f"/library/metadata/{show_key}", s=s)
            show = (show.get("MediaContainer", {}).get("Metadata") or [{}])[0]
            if not show.get("thumb"):
                _request("POST", f"/library/metadata/{show_key}/posters", {"url": avatar}, s)
                posters += 1
    return updated, posters


def run_sync():
    try:
        updated, posters = sync()
        state["last_result"] = f"✓ {updated} afleveringen bijgewerkt, {posters} posters gezet"
        if updated or posters:
            log.info("Plex sync: %d episodes updated, %d posters", updated, posters)
    except Exception as e:  # noqa: BLE001 - shown on the Plex page
        log.warning("Plex sync failed: %s", e)
        state["last_result"] = f"✗ {e}"
    state["last_sync"] = time.time()
    return state["last_result"]


def after_download():
    """Ask Plex to scan, then update titles once the new episode has been picked up."""
    s = settings()
    if not enabled(s):
        return
    try:
        _request("GET", f"/library/sections/{s['plex_section']}/refresh", s=s)
    except Exception as e:  # noqa: BLE001
        log.warning("Plex scan request failed: %s", e)
    _wake.set()


def _loop():
    while True:
        triggered = _wake.wait(SYNC_EVERY)
        _wake.clear()
        if triggered:
            time.sleep(60)  # give Plex's scanner time to add the new file
        if enabled():
            run_sync()


def start():
    threading.Thread(target=_loop, name="plex", daemon=True).start()
