import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import db, worker

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
    return datetime.fromisoformat(value).astimezone().strftime("%d-%m-%Y %H:%M")


def _fmt_date(value):
    if not value or len(value) != 8:
        return "—"
    return f"{value[6:8]}-{value[4:6]}-{value[0:4]}"


def _iso_date(value):
    """YYYYMMDD -> YYYY-MM-DD for <input type=date>."""
    return f"{value[0:4]}-{value[4:6]}-{value[6:8]}" if value else ""


templates.env.filters.update(dt=_fmt_dt, date=_fmt_date, isodate=_iso_date)
templates.env.globals.update(qualities=QUALITIES, audio_formats=AUDIO_FORMATS)


def render(request, name, **ctx):
    return templates.TemplateResponse(request, name, ctx)


def back(url):
    return RedirectResponse(url, status_code=303)


def _source_or_404(source_id):
    src = db.one("SELECT * FROM sources WHERE id = ?", (source_id,))
    if not src:
        raise HTTPException(404, "Bron niet gevonden")
    return src


async def _source_from_form(request):
    form = await request.form()
    url = worker.normalize_url(str(form.get("url", "")))
    if not url.startswith("http"):
        raise HTTPException(400, "Ongeldige URL")
    keep_last = str(form.get("keep_last", "")).strip()
    return {
        "name": str(form.get("name", "")).strip() or url,
        "url": url,
        "kind": "audio" if form.get("kind") == "audio" else "video",
        "quality": form.get("quality") if form.get("quality") in QUALITIES else "1080",
        "audio_format": form.get("audio_format") if form.get("audio_format") in AUDIO_FORMATS else "m4a",
        "interval_minutes": max(5, int(form.get("interval_minutes") or 60)),
        "only_after": str(form.get("only_after", "")).replace("-", "") or None,
        "keep_last": int(keep_last) if keep_last.isdigit() and int(keep_last) > 0 else None,
        "sub_langs": str(form.get("sub_langs", "")).strip(),
        "backfill": 1 if form.get("backfill") else 0,
        "enabled": 1 if form.get("enabled") else 0,
    }


# --------------------------------------------------------------------------- pages

@app.get("/")
def index(request: Request):
    sources = db.query(
        """SELECT s.*,
                  COUNT(m.id)                   AS n_total,
                  SUM(m.status = 'done')        AS n_done,
                  SUM(m.status = 'pending')     AS n_pending,
                  SUM(m.status = 'error')       AS n_error
           FROM sources s LEFT JOIN media m ON m.source_id = s.id
           GROUP BY s.id ORDER BY s.name COLLATE NOCASE"""
    )
    return render(request, "index.html", sources=sources)


@app.post("/sources")
async def create_source(request: Request):
    s = await _source_from_form(request)
    cur = db.execute(
        "INSERT INTO sources (name, url, kind, quality, audio_format, interval_minutes, only_after, "
        "keep_last, sub_langs, backfill, enabled, created_at) "
        "VALUES (:name, :url, :kind, :quality, :audio_format, :interval_minutes, :only_after, "
        ":keep_last, :sub_langs, :backfill, :enabled, :created_at)",
        s | {"created_at": worker.now_iso()},
    )
    worker.request_check(cur.lastrowid)
    return back(f"/sources/{cur.lastrowid}")


@app.get("/sources/{source_id}")
def source_detail(request: Request, source_id: int, status: str = ""):
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
                  counts=counts, status=status)


@app.post("/sources/{source_id}")
async def update_source(request: Request, source_id: int):
    _source_or_404(source_id)
    s = await _source_from_form(request)
    db.execute(
        "UPDATE sources SET name = :name, url = :url, kind = :kind, quality = :quality, "
        "audio_format = :audio_format, interval_minutes = :interval_minutes, only_after = :only_after, "
        "keep_last = :keep_last, sub_langs = :sub_langs, backfill = :backfill, enabled = :enabled "
        "WHERE id = :id",
        s | {"id": source_id},
    )
    worker.wake_downloads()
    return back(f"/sources/{source_id}")


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


@app.post("/media/{media_id}/{action}")
def media_action(media_id: int, action: str):
    m = db.one("SELECT * FROM media WHERE id = ?", (media_id,))
    if not m:
        raise HTTPException(404)
    if action == "download" and m["status"] != "downloading":
        db.execute("UPDATE media SET status = 'pending', error = NULL WHERE id = ?", (media_id,))
        worker.wake_downloads()
    elif action == "skip" and m["status"] in ("pending", "error"):
        db.execute("UPDATE media SET status = 'skipped' WHERE id = ?", (media_id,))
    elif action == "delete" and m["status"] == "done":
        worker.delete_files(m["filepath"])
        db.execute("UPDATE media SET status = 'deleted' WHERE id = ?", (media_id,))
    return back(f"/sources/{m['source_id']}")


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


# --------------------------------------------------------------------------- api

@app.get("/api/status")
def api_status():
    pending = db.one("SELECT COUNT(*) AS n FROM media WHERE status = 'pending'")["n"]
    return {"indexing": worker.state["indexing"], "current": worker.state["current"], "pending": pending}


@app.get("/healthz")
def healthz():
    return {"ok": True}
