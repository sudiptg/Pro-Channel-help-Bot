"""Post Studio - web server (Telegram webhook), scheduler thread and start-up.   Start command: python app.py"""
import logging
import os
import threading
import time

from flask import Flask, jsonify, request

import bot
import config
import core
import store
import tgapi

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")
app = Flask(__name__)
STATE = {"ready": False, "mode": "", "error": "", "started": time.time()}


def init(start_scheduler=True):
    """Opens the storage, connects to Telegram and starts the scheduler. Runs in the background so the web server is up at once."""
    C = config.Config
    try:
        if not C.BOT_TOKEN:
            raise RuntimeError("BOT_TOKEN is not set (Render → Environment).")
        if not C.OWNER_IDS:
            raise RuntimeError("OWNER_ID is not set (your numeric Telegram ID).")
        db = store.open_store()
        tg = tgapi.Tg()
        core.init(tg, db)
        STATE["mode"] = bot.startup(tg)
        STATE["ready"], STATE["error"] = True, ""
        if start_scheduler:
            threading.Thread(target=scheduler, daemon=True, name="scheduler").start()
        log.info("ready (%s, storage: %s)", STATE["mode"], db.kind)
    except Exception as e:
        STATE["error"] = str(e)
        log.exception("start-up failed")


def scheduler():
    last = 0.0
    while True:
        time.sleep(2)
        try:
            core.tick_live()
        except Exception:
            log.exception("vote refresh failed")
        if time.time() - last >= config.Config.TICK:
            last = time.time()
            for fn in (core.tick_sched, core.tick_deletes):
                try:
                    fn()
                except Exception:
                    log.exception("%s failed", fn.__name__)


@app.get("/")
def index():
    return "%s is running." % config.Config.APP_NAME


@app.get("/health")
def health():
    ok = STATE["ready"]
    return jsonify({"ok": ok, "mode": STATE["mode"], "error": STATE["error"], "bot": core.tg.me.get("username") if core.tg else None,
                    "storage": core.db.kind if core.db else None, "uptime": int(time.time() - STATE["started"])}), (200 if ok or not STATE["error"] else 503)


@app.post("/<path:secret>")
def webhook(secret):
    C = config.Config
    if secret != C.WEBHOOK_PATH:
        return "not found", 404
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != C.WEBHOOK_HEADER_TOKEN:
        return "forbidden", 403
    upd = request.get_json(silent=True)
    if not isinstance(upd, dict):
        return "bad request", 400
    if not STATE["ready"]:
        return "starting", 503                                              # Telegram retries later
    bot.submit(upd)
    return "ok"


if __name__ == "__main__":
    threading.Thread(target=init, daemon=True, name="init").start()
    app.run(host="0.0.0.0", port=config.Config.PORT, threaded=True)
