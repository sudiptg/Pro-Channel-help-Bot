"""Post Studio - update routing: messages, button taps, channel membership changes and the buttons of published posts."""
import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import buttons
import config
import core
import screens_a
import screens_b  # noqa: F401  (registers the screens)
import ui
from tgapi import TgError
from ui import Ctx

log = logging.getLogger("bot")
COMMANDS = [("start", "Open the menu"), ("new", "Create a new post"), ("drafts", "My drafts"), ("posts", "My published posts"),
            ("scheduled", "Scheduled posts"), ("channels", "My channels"), ("settings", "Settings"), ("help", "Help"), ("cancel", "Cancel the current step")]
ALLOWED_UPDATES = ["message", "callback_query", "my_chat_member"]
_locks, _guard = {}, threading.Lock()
_seen = OrderedDict()
_denied = {}
_timers = {}
_pool = None


def ulock(uid):
    with _guard:
        return _locks.setdefault(uid, threading.RLock())


def seen(update_id):
    with _guard:
        if update_id in _seen:
            return True
        _seen[update_id] = 1
        if len(_seen) > 5000:
            _seen.popitem(last=False)
    return False


def _route(name, *args):
    def run(ctx):
        return ui.SCREENS[name](ctx, *args)
    return run


ROUTES = {"/start": screens_a.menu, "/menu": screens_a.menu, "/new": _route("nw"), "/drafts": _route("dl", 0), "/posts": _route("pl", 0),
          "/scheduled": _route("sl", 0), "/channels": _route("cl", 0), "/settings": _route("gm", "u"), "/help": _route("h"), "/admin": _route("ad")}


# ----------------------------------------------------------------------------------------------------------------------
# messages
# ----------------------------------------------------------------------------------------------------------------------
def deny(uid):
    if time.time() - _denied.get(uid, 0) > 600:
        _denied[uid] = time.time()
        core.tg.ok("sendMessage", chat_id=uid, text=config.Config.DENY_TEXT)


def on_message(msg):
    if (msg.get("chat") or {}).get("type") != "private":
        return
    frm = msg.get("from") or {}
    uid = frm.get("id")
    if not uid or frm.get("is_bot"):
        return
    if not core.allowed(uid):
        return deny(uid)
    with ulock(uid):
        u = core.get_user(uid, frm.get("first_name", ""))
        u["seen"] = core.now()
        if frm.get("first_name") and u.get("name") != frm["first_name"]:
            u["name"] = frm["first_name"]
        ctx = Ctx(u)
        try:
            route_message(ctx, msg)
        except TgError as e:
            log.warning("telegram error: %s", e)
            ctx.say("⚠️ " + ui.esc(e.friendly()))
        except Exception:
            log.exception("message handler failed")
            ctx.say("⚠️ Something went wrong. Please try again, or send /start.")
        finally:
            core.save_user(ctx.u)


def route_message(ctx, msg):
    text = msg.get("text") or ""
    if text.startswith("/"):
        cmd = text.split()[0].split("@")[0].lower()
        ctx.clear()
        ctx.forget_input(msg)
        if cmd == "/cancel":
            return screens_a.menu(ctx, fresh=True)
        fn = ROUTES.get(cmd)
        if fn is None:
            return ctx.say("I do not know that command. Send /start to open the menu.")
        if cmd == "/admin" and not core.is_owner(ctx.uid):
            return
        ctx.fresh_next = True
        return fn(ctx)
    st = ctx.u.get("st") or {}
    if st.get("n") in ui.INPUTS:
        return ui.INPUTS[st["n"]](ctx, msg, st)
    if not ctx.cfg("media_autostart"):
        return ctx.say("Tap <b>✍️ New post</b> in the menu to start a post. (/start)")
    if (msg.get("forward_origin") or msg.get("forward_from_chat") or msg.get("forward_from")) and not ctx.cfg("fwd_to_draft"):
        return ctx.say("Forwarded messages are ignored. You can turn this on in ⚙️ Settings → Behavior.")
    d = screens_a.intake(ctx, msg, "new")
    if d:
        screens_a.after_intake(ctx, msg, d)


def defer_show(uid, did, delay=1.3):
    """An album arrives as separate messages: show the draft once, shortly after the last picture."""
    def run():
        with ulock(uid):
            d = core.get_draft(did)
            if not d:
                return
            ctx = Ctx(core.get_user(uid))
            try:
                screens_a.draft_screen(ctx, d, force=True)
            except Exception:
                log.exception("showing the album draft failed")
            finally:
                core.save_user(ctx.u)
    with _guard:
        old = _timers.pop(uid, None)
        if old:
            old.cancel()
        t = threading.Timer(delay, run)
        t.daemon = True
        _timers[uid] = t
        t.start()


ui.HOOKS["defer"] = defer_show


# ----------------------------------------------------------------------------------------------------------------------
# button taps
# ----------------------------------------------------------------------------------------------------------------------
def on_callback(cq):
    data = cq.get("data") or ""
    if data[:2] in ("v:", "a:"):
        return on_post_button(cq)
    frm = cq.get("from") or {}
    uid = frm.get("id")
    if not core.allowed(uid):
        return core.tg.ok("answerCallbackQuery", callback_query_id=cq["id"], text=config.Config.DENY_TEXT, show_alert=True)
    with ulock(uid):
        u = core.get_user(uid, frm.get("first_name", ""))
        ctx = Ctx(u, cq.get("message"), cq)
        try:
            ctx.clear()
            name, *args = data.split(ui.SEP)
            fn = ui.SCREENS.get(name)
            if fn is None:
                ctx.toast("This button is out of date. Send /start.", True)
            else:
                fn(ctx, *args)
        except TgError as e:
            log.warning("telegram error in %s: %s", data, e)
            ctx.toast(e.friendly(), True)
        except Exception:
            log.exception("button %s failed", data)
            ctx.toast("Something went wrong. Please try again.", True)
        finally:
            if not ctx.answered:
                ctx.toast()
            core.save_user(ctx.u)


def on_post_button(cq):
    """Vote and popup buttons on a published post (anyone in the channel may tap), and on the preview in the bot chat."""
    kind, _, bid = (cq.get("data") or "").partition(":")
    msg = cq.get("message")
    cid = cq["id"]
    if not msg or msg.get("date") == 0:
        return core.tg.ok("answerCallbackQuery", callback_query_id=cid, text="This post is no longer available.", show_alert=True)
    chat = msg["chat"]
    if chat.get("type") == "private":
        text, alert = "This is only a preview. Votes are counted after the post is published.", False
        if kind == "a":
            u = core.db.get("users", cq["from"]["id"]) or {}
            d = core.get_draft(u.get("cur"))
            b = buttons.get(d["rows"], bid) if d else None
            text, alert = (b["v"], True) if b and b["k"] == "alert" else ("This popup shows its text when the post is published.", False)
    elif kind == "v":
        text, alert = core.vote(chat["id"], msg["message_id"], cq["from"]["id"], bid)
    else:
        text, alert = core.popup(chat["id"], msg["message_id"], bid)
    core.tg.ok("answerCallbackQuery", callback_query_id=cid, text=text[:200] or None, show_alert=True if alert else None)


# ----------------------------------------------------------------------------------------------------------------------
# the bot joins or leaves a channel
# ----------------------------------------------------------------------------------------------------------------------
def on_member(upd):
    chat, frm, new = upd.get("chat") or {}, upd.get("from") or {}, upd.get("new_chat_member") or {}
    ctype, status, uid = chat.get("type"), new.get("status"), frm.get("id")
    if ctype not in ("channel", "supergroup", "group") or not uid:
        return
    if status in ("administrator", "creator"):
        if not core.allowed(uid):
            log.info("unknown user %s added the bot to %s: leaving", uid, chat.get("id"))
            return core.tg.ok("leaveChat", chat_id=chat["id"])
        existing = core.get_channel(chat["id"])
        ch = core.upsert_channel(chat, [uid], core.rights_from_member(new, ctype), True)
        ch["err"] = ""
        core.save_channel(ch)
        for o in ch["owners"]:
            u = core.get_user(o)
            if core.ucfg(u, "notify_added"):
                can = ch["can"]
                miss = [n for k, n in (("post", "Post messages"), ("edit", "Edit messages"), ("delete", "Delete messages")) if not can.get(k)]
                text = "%s <b>%s</b> %s." % ("✅", ui.esc(ch["title"]), "is ready" if not existing else "was updated")
                if miss:
                    text += "\n⚠️ The bot still needs the right to: <b>%s</b>." % ", ".join(miss)
                kb = Ctx(u).kb()
                kb.row(kb.btn("📝 New post here", "cn", ch["_id"], style="success"), kb.btn("📢 Channel", "co", ch["_id"]))
                core.notify(o, text, kb.markup())
    else:
        ch = core.get_channel(chat["id"])
        if not ch:
            return
        ch["ok"], ch["err"] = False, "The bot was removed from the channel." if status in ("left", "kicked") else "The bot is not an admin any more."
        core.save_channel(ch)
        for o in ch.get("owners", []):
            if core.ucfg(core.get_user(o), "notify_errors"):
                core.notify(o, "⚠️ <b>%s</b>: %s" % (ui.esc(ch["title"]), ch["err"]))


# ----------------------------------------------------------------------------------------------------------------------
# entry points
# ----------------------------------------------------------------------------------------------------------------------
def handle_update(upd):
    if upd.get("update_id") is not None and seen(upd["update_id"]):
        return
    try:
        if "message" in upd:
            on_message(upd["message"])
        elif "callback_query" in upd:
            on_callback(upd["callback_query"])
        elif "my_chat_member" in upd:
            on_member(upd["my_chat_member"])
    except Exception:
        log.exception("update failed")


def submit(upd):
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=config.Config.WORKERS, thread_name_prefix="upd")
    _pool.submit(handle_update, upd)


def startup(tg, polling=None):
    """Learns the bot's identity, sets the command list and connects Telegram (webhook, or polling for local tests)."""
    C = config.Config
    tg.me = tg("getMe")
    log.info("bot is @%s", tg.me.get("username"))
    tg.ok("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
    tg.ok("setMyShortDescription", short_description="Make channel posts with colored buttons, schedule them and manage everything.")
    polling = C.USE_POLLING if polling is None else polling
    if polling:
        tg.ok("deleteWebhook")
        threading.Thread(target=poll_loop, args=(tg,), daemon=True, name="poller").start()
        return "polling"
    if not C.PUBLIC_URL:
        log.error("No public address: set WEBHOOK_BASE (Render sets RENDER_EXTERNAL_URL itself) or USE_POLLING=1.")
        return "no-url"
    tg("setWebhook", url="%s/%s" % (C.PUBLIC_URL, C.WEBHOOK_PATH), secret_token=C.WEBHOOK_HEADER_TOKEN, allowed_updates=ALLOWED_UPDATES,
       max_connections=20, drop_pending_updates=False)
    return "webhook"


def poll_loop(tg):
    offset = 0
    while True:
        try:
            for upd in tg("getUpdates", offset=offset, timeout=25, allowed_updates=ALLOWED_UPDATES, _timeout=40):
                offset = max(offset, upd["update_id"] + 1)
                submit(upd)
        except Exception as e:
            log.warning("polling: %s", e)
            time.sleep(3)
