"""Push notifications via Pushover, Prowl, ntfy and Discord."""
import base64
import json
import logging
import threading
import urllib.parse
import urllib.request

from . import db

log = logging.getLogger("tubekeeper")

# Setting keys and their defaults; everything lives in the settings table as text
DEFAULTS = {
    "pushover_enabled": "0", "pushover_user": "", "pushover_token": "",
    "prowl_enabled": "0", "prowl_apikey": "",
    "ntfy_enabled": "0", "ntfy_server": "https://ntfy.sh", "ntfy_topic": "", "ntfy_token": "",
    "discord_enabled": "0", "discord_webhook": "",
    "on_download": "1", "on_download_error": "1", "on_index_error": "1",
}
EVENTS = {
    "download": "on_download",
    "download_error": "on_download_error",
    "index_error": "on_index_error",
}


def get_settings():
    rows = db.query("SELECT key, value FROM settings")
    return DEFAULTS | {r["key"]: r["value"] for r in rows if r["key"] in DEFAULTS}


def save_settings(values):
    with db.tx() as c:
        for key in DEFAULTS:
            if key in values:
                c.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                          "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, values[key]))


def _post(url, data=None, json_body=None, headers=None):
    headers = dict(headers or {})
    if json_body is not None:
        body = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    elif isinstance(data, dict):
        body = urllib.parse.urlencode(data).encode()
    else:
        body = (data or "").encode()
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def _send_pushover(s, title, message, url):
    data = {"token": s["pushover_token"], "user": s["pushover_user"], "title": title, "message": message}
    if url:
        data["url"] = url
    _post("https://api.pushover.net/1/messages.json", data)


def _send_prowl(s, title, message, url):
    data = {"apikey": s["prowl_apikey"], "application": "TubeKeeper", "event": title, "description": message}
    if url:
        data["url"] = url
    _post("https://api.prowlapp.com/publicapi/add", data)


def _send_ntfy(s, title, message, url):
    # Header values must be latin-1; RFC 2047 encoding lets titles contain any character
    headers = {"Title": "=?UTF-8?B?" + base64.b64encode(title.encode()).decode() + "?="}
    if url:
        headers["Click"] = url
    if s["ntfy_token"]:
        headers["Authorization"] = f"Bearer {s['ntfy_token']}"
    _post(f"{s['ntfy_server'].rstrip('/')}/{s['ntfy_topic']}", message, headers=headers)


def _send_discord(s, title, message, url):
    text = f"**{title}**\n{message}" + (f"\n{url}" if url else "")
    _post(s["discord_webhook"], json_body={"content": text[:2000], "username": "TubeKeeper"})


SERVICES = {
    "pushover": (_send_pushover, ("pushover_user", "pushover_token")),
    "prowl": (_send_prowl, ("prowl_apikey",)),
    "ntfy": (_send_ntfy, ("ntfy_topic",)),
    "discord": (_send_discord, ("discord_webhook",)),
}


def send_now(title, message, url=None, only=None):
    """Send synchronously; returns {service: error or None}. Used by the test button."""
    s = get_settings()
    results = {}
    for name, (fn, required) in SERVICES.items():
        if only and name != only:
            continue
        if not only and s[f"{name}_enabled"] != "1":
            continue
        if not all(s[k] for k in required):
            results[name] = "niet volledig ingevuld"
            continue
        try:
            fn(s, title, message, url)
            results[name] = None
        except Exception as e:  # noqa: BLE001 - reported back to the user
            log.warning("Notification via %s failed: %s", name, e)
            results[name] = str(e)
    return results


def notify(event, title, message, url=None):
    """Fire-and-forget; never blocks or breaks the downloader."""
    def run():
        try:
            if get_settings()[EVENTS[event]] == "1":
                send_now(title, message, url)
        except Exception:  # noqa: BLE001
            log.exception("Notification error")
    threading.Thread(target=run, daemon=True).start()
