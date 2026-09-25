import logging
import os
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import db, notify, subs, worker

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)

QUALITIES = ["best", "2160", "1440", "1080", "720", "480", "360"]
AUDIO_FORMATS = ["m4a", "mp3", "opus"]


@asynccontextmanager
async def lifespan(_app):
    db.init()
    worker.start()
    yield


app = FastAPI(title="TubeKeeper", lifespan=lifespan)
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))


def _fmt_dt(value):
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%d-%m-%Y %H:%M")
    except ValueError:
        return "—"


def _fmt_date(value):
    if not value or len(value) != 8:
        return "—"
    return f"{value[6:8]}-{value[4:6]}-{value[0:4]}"


def _fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _iso_date(value):
    """YYYYMMDD -> YYYY-MM-DD for <input type=date>."""
    return f"{value[0:4]}-{value[4:6]}-{value[6:8]}" if value else ""


templates.env.filters.update(dt=_fmt_dt, date=_fmt_date, isodate=_iso_date, size=_fmt_size,
                             ts=lambda t: datetime.fromtimestamp(float(t)).strftime("%d-%m-%Y %H:%M"))
templates.env.globals.update(qualities=QUALITIES, audio_formats=AUDIO_FORMATS,
                             version=os.environ.get("APP_VERSION", "dev"))


def render(request, name, **ctx):
    return templates.TemplateResponse(request, name, ctx)


def back(url):
    return RedirectResponse(url, status_code=303)


def _source_or_404(source_id):
    src = db.one("SELECT * FROM sources WHERE id = ?", (source_id,))
    if not src:
        raise HTTPException(404, "Bron niet gevonden")
    return src


def _settings_from_form(form):
    keep_last = str(form.get("keep_last", "")).strip()
    return {
        "kind": "audio" if form.get("kind") == "audio" else "video",
        "quality": form.get("quality") if form.get("quality") in QUALITIES else "1080",
        "audio_format": form.get("audio_format") if form.get("audio_format") in AUDIO_FORMATS else "m4a",
        "interval_minutes": max(5, int(form.get("interval_minutes") or 60)),
        "only_after": str(form.get("only_after", "")).replace("-", "") or None,
        "keep_last": int(keep_last) if keep_last.isdigit() and int(keep_last) > 0 else None,
        "sub_langs": str(form.get("sub_langs", "")).strip(),
        "lang": str(form.get("lang", "")).strip(),
        "layout": "series" if form.get("layout") == "series" else "flat",
        "backfill": 1 if form.get("backfill") else 0,
        "enabled": 1 if form.get("enabled") else 0,
        "redownload_missing": 1 if form.get("redownload_missing") else 0,
    }


async def _source_from_form(request):
    form = await request.form()
    url = worker.normalize_url(str(form.get("url", "")))
    if not url.startswith("http"):
        raise HTTPException(400, "Ongeldige URL")
    return {
        # Empty name: @handle from the URL, otherwise the channel/playlist title after the first check
        "name": str(form.get("name", "")).strip() or worker.name_from_url(url) or url,
        "url": url,
    } | _settings_from_form(form)


# --------------------------------------------------------------------------- pages

@app.get("/")
def index(request: Request, msg: str = ""):
    sources = db.query(
        """SELECT s.*,
                  COUNT(m.id)                   AS n_total,
                  SUM(m.status = 'done')        AS n_done,
                  SUM(m.status = 'pending')     AS n_pending,
                  SUM(m.status = 'error')       AS n_error
           FROM sources s LEFT JOIN media m ON m.source_id = s.id
           GROUP BY s.id ORDER BY s.name COLLATE NOCASE"""
    )
    sizes = {s["id"]: worker.disk_usage(s) for s in sources}
    return render(request, "index.html", sources=sources, sizes=sizes, total_size=sum(sizes.values()), msg=msg)


@app.post("/sources")
async def create_source(request: Request):
    s = await _source_from_form(request)
    cur = db.execute(
        "INSERT INTO sources (name, url, kind, quality, audio_format, interval_minutes, only_after, "
        "keep_last, sub_langs, lang, layout, backfill, enabled, redownload_missing, created_at) "
        "VALUES (:name, :url, :kind, :quality, :audio_format, :interval_minutes, :only_after, "
        ":keep_last, :sub_langs, :lang, :layout, :backfill, :enabled, :redownload_missing, :created_at)",
        s | {"created_at": worker.now_iso()},
    )
    worker.request_check(cur.lastrowid)
    return back(f"/sources/{cur.lastrowid}")


@app.get("/sources/{source_id}")
def source_detail(request: Request, source_id: int, status: str = "", saved: int = 0, msg: str = ""):
    src = _source_or_404(source_id)
    sql = "SELECT * FROM media WHERE source_id = ?"
    args = [source_id]
    if status:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY COALESCE(upload_date, '99999999') DESC, id DESC LIMIT 1000"
    counts = {r["status"]: r["n"] for r in db.query(
        "SELECT status, COUNT(*) AS n FROM media WHERE source_id = ? GROUP BY status", (source_id,))}
    return render(request, "source.html", src=src, media=db.query(sql, args),
                  counts=counts, status=status, size=worker.disk_usage(src), saved=saved, msg=msg)


@app.get("/sources/{source_id}/edit")
def edit_source(request: Request, source_id: int):
    return render(request, "edit.html", src=_source_or_404(source_id))


@app.post("/sources/{source_id}")
async def update_source(request: Request, source_id: int):
    old = _source_or_404(source_id)
    s = await _source_from_form(request)
    db.execute(
        "UPDATE sources SET name = :name, url = :url, kind = :kind, quality = :quality, "
        "audio_format = :audio_format, interval_minutes = :interval_minutes, only_after = :only_after, "
        "keep_last = :keep_last, sub_langs = :sub_langs, lang = :lang, layout = :layout, "
        "backfill = :backfill, enabled = :enabled, redownload_missing = :redownload_missing "
        "WHERE id = :id",
        s | {"id": source_id},
    )
    if s["layout"] != old["layout"] or s["name"] != old["name"]:
        worker.reorganize_async(source_id, old_dir=worker.source_dir(old))
    if s["keep_last"] != old["keep_last"]:
        worker.enforce_retention(_source_or_404(source_id))  # a lower limit applies right away
    worker.request_check(source_id)  # picks up a changed URL or title language right away
    worker.wake_downloads()
    return back(f"/sources/{source_id}?saved=1")


@app.post("/sources/{source_id}/delete")
async def delete_source(request: Request, source_id: int):
    _source_or_404(source_id)
    form = await request.form()
    if form.get("delete_files"):
        for row in db.query("SELECT filepath FROM media WHERE source_id = ? AND status = 'done'", (source_id,)):
            worker.delete_files(row["filepath"])
    db.execute("DELETE FROM sources WHERE id = ?", (source_id,))
    return back("/")


@app.post("/sources/{source_id}/check")
def check_source(source_id: int):
    _source_or_404(source_id)
    worker.request_check(source_id)
    return back(f"/sources/{source_id}")


@app.post("/sources/{source_id}/toggle")
async def toggle_source(request: Request, source_id: int):
    _source_or_404(source_id)
    db.execute("UPDATE sources SET enabled = 1 - enabled WHERE id = ?", (source_id,))
    worker.wake_downloads()
    return back(request.headers.get("referer") or f"/sources/{source_id}")


@app.post("/sources/{source_id}/clear-queue")
def clear_queue(source_id: int):
    db.execute("UPDATE media SET status = 'skipped' WHERE source_id = ? AND status = 'pending'", (source_id,))
    return back(f"/sources/{source_id}")


def _sync_message(results):
    relinked, missing, requeued = (sum(r[i] for r in results) for i in range(3))
    if not (relinked or missing or requeued):
        return "✓ Schijf gesynchroniseerd: alles klopt"
    parts = [f"{missing} verwijderd" if missing else "", f"{requeued} opnieuw in de wachtrij" if requeued else "",
             f"{relinked} verplaatst en teruggevonden" if relinked else ""]
    return "✓ Schijf gesynchroniseerd: " + ", ".join(p for p in parts if p)


@app.post("/sources/{source_id}/sync")
def sync_source(source_id: int):
    msg = _sync_message([worker.sync_disk(_source_or_404(source_id))])
    return back(f"/sources/{source_id}?" + urllib.parse.urlencode({"msg": msg}))


@app.post("/sync")
def sync_all():
    msg = _sync_message([worker.sync_disk(s) for s in db.query("SELECT * FROM sources")])
    return back("/?" + urllib.parse.urlencode({"msg": msg}))


@app.post("/sources/{source_id}/retry")
def retry_errors(source_id: int):
    db.execute("UPDATE media SET status = 'pending', error = NULL WHERE source_id = ? AND status = 'error'",
               (source_id,))
    worker.wake_downloads()
    return back(f"/sources/{source_id}")


@app.post("/sources/{source_id}/download-skipped")
def download_skipped(source_id: int):
    db.execute("UPDATE media SET status = 'pending' WHERE source_id = ? AND status = 'skipped'", (source_id,))
    worker.wake_downloads()
    return back(f"/sources/{source_id}")


def _apply_media_action(m, action):
    """Returns True when the item changed; actions that don't fit the status are ignored."""
    if action == "download" and m["status"] in ("skipped", "error", "deleted"):
        db.execute("UPDATE media SET status = 'pending', error = NULL WHERE id = ?", (m["id"],))
    elif action == "skip" and m["status"] in ("pending", "error"):
        db.execute("UPDATE media SET status = 'skipped' WHERE id = ?", (m["id"],))
    elif action == "delete" and m["status"] == "done":
        worker.delete_files(m["filepath"])
        db.execute("UPDATE media SET status = 'deleted' WHERE id = ?", (m["id"],))
    else:
        return False
    return True


def _back_to_source(source_id, form, msg=""):
    params = {k: v for k, v in (("status", str(form.get("status", ""))), ("msg", msg)) if v}
    return back(f"/sources/{source_id}" + ("?" + urllib.parse.urlencode(params) if params else ""))


@app.post("/sources/{source_id}/bulk")
async def bulk_media(request: Request, source_id: int):
    _source_or_404(source_id)
    form = await request.form()
    action = str(form.get("action", ""))
    ids = [int(i) for i in form.getlist("ids") if str(i).isdigit()]
    changed = 0
    for media_id in ids:
        m = db.one("SELECT * FROM media WHERE id = ? AND source_id = ?", (media_id, source_id))
        if m and _apply_media_action(m, action):
            changed += 1
    if action == "download" and changed:
        worker.wake_downloads()
    label = {"download": "in de wachtrij gezet", "skip": "overgeslagen", "delete": "verwijderd"}.get(action, "bijgewerkt")
    skipped = len(ids) - changed
    msg = f"✓ {changed} video's {label}" + (f" ({skipped} niet van toepassing)" if skipped else "")
    return _back_to_source(source_id, form, msg)


@app.post("/media/{media_id}/{action}")
async def media_action(request: Request, media_id: int, action: str):
    m = db.one("SELECT * FROM media WHERE id = ?", (media_id,))
    if not m:
        raise HTTPException(404)
    if _apply_media_action(m, action) and action == "download":
        worker.wake_downloads()
    return _back_to_source(m["source_id"], await request.form())


@app.get("/queue")
def queue(request: Request):
    pending = db.query(
        "SELECT m.*, s.name AS source_name FROM media m JOIN sources s ON s.id = m.source_id "
        "WHERE m.status = 'pending' AND s.enabled = 1 "
        "ORDER BY COALESCE(m.upload_date, '99999999'), m.id LIMIT 200")
    recent = db.query(
        "SELECT m.*, s.name AS source_name FROM media m JOIN sources s ON s.id = m.source_id "
        "WHERE m.status IN ('done', 'error') "
        "ORDER BY COALESCE(m.downloaded_at, m.created_at) DESC LIMIT 50")
    return render(request, "queue.html", pending=pending, recent=recent)


@app.get("/import")
def import_page(request: Request, msg: str = ""):
    return render(request, "import.html", channels=None, d=subs.get_defaults(), msg=msg,
                  has_cookies=os.path.exists(worker.COOKIES_FILE), sync=subs.sync_enabled(),
                  last_sync=subs.last_sync())


@app.post("/import/fetch")
async def import_fetch(request: Request):
    form = await request.form()
    try:
        upload = form.get("takeout")
        if upload is not None and getattr(upload, "filename", ""):
            channels = subs.parse_takeout(await upload.read())
        else:
            channels = subs.fetch_account()
    except Exception as e:  # noqa: BLE001 - show the reason on the page
        return back("/import?" + urllib.parse.urlencode({"msg": f"✗ {e}"[:300]}))
    return render(request, "import.html", channels=subs.mark_existing(channels), d=subs.get_defaults(),
                  msg="", has_cookies=os.path.exists(worker.COOKIES_FILE), sync=subs.sync_enabled(),
                  last_sync=subs.last_sync())


@app.post("/import/add")
async def import_add(request: Request):
    form = await request.form()
    settings = _settings_from_form(form)
    subs.save_defaults(settings)
    channels = []
    for value in form.getlist("channel"):
        cid, _, rest = str(value).partition("|")
        url, _, title = rest.partition("|")
        channels.append({"id": cid, "url": url, "title": title})
    added = subs.add_channels(channels, settings)
    return back("/?" + urllib.parse.urlencode({"msg": f"✓ {added} kanalen toegevoegd"}))


@app.post("/import/cookies")
async def import_cookies(request: Request):
    form = await request.form()
    upload = form.get("cookies")
    data = await upload.read() if upload is not None and getattr(upload, "filename", "") else b""
    if b"youtube.com" not in data:
        return back("/import?" + urllib.parse.urlencode({"msg": "✗ Dit lijkt geen cookies.txt met YouTube-cookies."}))
    subs.save_cookies(data)
    return back("/import?" + urllib.parse.urlencode({"msg": "✓ Cookies opgeslagen"}))


@app.post("/import/settings")
async def import_settings(request: Request):
    form = await request.form()
    subs.save_defaults(_settings_from_form(form))
    subs.set_sync(bool(form.get("sync")))
    return back("/import?" + urllib.parse.urlencode({"msg": "✓ Opgeslagen"}))


@app.get("/notifications")
def notifications(request: Request, test: str = ""):
    return render(request, "notifications.html", s=notify.get_settings(), test=test)


async def _save_notification_form(request):
    form = await request.form()
    values = {}
    for key in notify.DEFAULTS:
        if key.endswith("_enabled") or key.startswith("on_"):
            values[key] = "1" if form.get(key) else "0"  # unchecked boxes are absent
        else:
            values[key] = str(form.get(key, "")).strip()
    notify.save_settings(values)


@app.post("/notifications")
async def save_notifications(request: Request):
    await _save_notification_form(request)
    return back("/notifications?test=saved")


@app.post("/notifications/test/{service}")
async def test_notification(request: Request, service: str):
    if service not in notify.SERVICES:
        raise HTTPException(404)
    await _save_notification_form(request)
    result = notify.send_now("TubeKeeper test", "Als je dit leest werken de meldingen 🎉",
                             "https://github.com/RichrdJ/tubekeeper", only=service)
    err = result.get(service)
    return back("/notifications?" + urllib.parse.urlencode({"test": f"{service}:{'ok' if err is None else err[:200]}"}))


# --------------------------------------------------------------------------- api

@app.get("/api/status")
def api_status():
    # Same rule as the queue page: paused sources don't count
    pending = db.one("SELECT COUNT(*) AS n FROM media m JOIN sources s ON s.id = m.source_id "
                     "WHERE m.status = 'pending' AND s.enabled = 1")["n"]
    return {"indexing": worker.state["indexing"], "current": worker.state["current"], "pending": pending}


@app.get("/healthz")
def healthz():
    return {"ok": True}
