"""Post Studio - the working parts: users, channels, drafts, building and sending posts, votes, schedule, auto-delete.
No menus here (see screens.py); everything is plain functions so it can be tested without Telegram menus."""
import copy
import logging
import threading
import time

import buttons
import config
import fmt
from tgapi import TgError

log = logging.getLogger("core")
tg = None
db = None
VOTE_LOCK = threading.Lock()
NO_TEXT = ("video_note", "sticker", "copy")
MEDIA_KINDS = ("photo", "video", "animation", "document", "audio", "voice", "video_note", "sticker")
SNAP_KEYS = ("kind", "text", "media", "rows", "opts", "src", "bn")
LPO = {"off": {"is_disabled": True}, "on": None, "large": {"prefer_large_media": True}, "small": {"prefer_small_media": True},
       "above": {"show_above_text": True}}
GAP = {"nl": "\n", "blank": "\n\n", "line": "\n" + config.DIVIDER + "\n"}


class ComposeError(Exception):
    pass


def init(tg_, db_):
    global tg, db
    tg, db = tg_, db_
    _meta["d"] = None


def now():
    return int(time.time())


# ----------------------------------------------------------------------------------------------------------------------
# users and access
# ----------------------------------------------------------------------------------------------------------------------
def get_user(uid, name=""):
    u = db.get("users", uid)
    if u is None:
        u = {"_id": uid, "name": name, "cfg": {"tz": config.Config.DEFAULT_TZ}, "st": {}, "panel": {}, "prev": {}, "cur": 0, "joined": now(), "seen": now()}
        db.put("users", u)
    return u


def save_user(u):
    db.put("users", u)


def ucfg(u, key):
    return config.effective(key, u.get("cfg"))


def tz_of(u):
    return config.tz_minutes(ucfg(u, "tz"))


_meta = {"t": 0, "d": None}


def meta():
    if _meta["d"] is None or now() - _meta["t"] > 5:
        _meta["d"] = db.get("meta", "bot") or {"_id": "bot", "allowed": [], "public": False}
        _meta["t"] = now()
    return _meta["d"]


def save_meta(d):
    db.put("meta", d)
    _meta["d"], _meta["t"] = d, now()


def is_owner(uid):
    return uid in config.Config.OWNER_IDS


def allowed(uid):
    m = meta()
    return is_owner(uid) or uid in m.get("allowed", []) or bool(m.get("public"))


# ----------------------------------------------------------------------------------------------------------------------
# channels
# ----------------------------------------------------------------------------------------------------------------------
def get_channel(chat_id):
    try:
        return db.get("channels", int(chat_id))
    except (TypeError, ValueError):
        return None


def my_channels(uid):
    rows = db.find("channels", {"owners": uid})
    rows.sort(key=lambda c: (c.get("title") or "").lower())
    return rows


def save_channel(ch):
    db.put("channels", ch)


def rights_from_member(m, chat_type="channel"):
    st = m.get("status")
    creator, admin = st == "creator", st in ("administrator", "creator")
    isch = chat_type == "channel"

    def r(k, default=False):
        return creator or bool(m.get(k, default if admin else False))
    return {"admin": admin, "post": admin and (r("can_post_messages") if isch else True), "edit": admin and r("can_edit_messages", not isch),
            "delete": admin and r("can_delete_messages"), "pin": admin and (r("can_edit_messages") if isch else r("can_pin_messages"))}


def upsert_channel(chat, owners, rights=None, ok=True):
    ch = get_channel(chat["id"]) or {"_id": int(chat["id"]), "owners": [], "cfg": {}, "n": 0, "added": now()}
    ch["title"] = chat.get("title") or ch.get("title") or str(chat["id"])
    ch["un"] = chat.get("username") or ""
    ch["type"] = chat.get("type", ch.get("type", "channel"))
    for o in owners:
        if o not in ch["owners"]:
            ch["owners"].append(o)
    ch["ok"] = ok
    if rights is not None:
        ch["can"] = rights
    save_channel(ch)
    return ch


def link_for(ch, mid):
    if ch.get("un"):
        return "https://t.me/%s/%d" % (ch["un"], mid)
    cid = str(ch["_id"])
    return "https://t.me/c/%s/%d" % (cid[4:], mid) if cid.startswith("-100") else ""


def refresh_channel(ch, bot_id):
    """Re-reads title, username and the bot's rights from Telegram. Returns the updated channel (ok=False when the bot is out)."""
    try:
        chat = tg("getChat", chat_id=ch["_id"])
        m = tg("getChatMember", chat_id=ch["_id"], user_id=bot_id)
    except TgError as e:
        ch["ok"] = False
        ch["err"] = e.friendly()
        save_channel(ch)
        return ch
    ch["title"], ch["un"], ch["type"] = chat.get("title") or ch["title"], chat.get("username") or "", chat.get("type", ch.get("type"))
    ch["can"] = rights_from_member(m, ch["type"])
    ch["ok"] = bool(ch["can"]["admin"])
    ch["err"] = "" if ch["ok"] else "The bot is not an admin."
    save_channel(ch)
    return ch


# ----------------------------------------------------------------------------------------------------------------------
# drafts and templates
# ----------------------------------------------------------------------------------------------------------------------
def new_draft(uid, **kw):
    d = {"_id": db.incr("counters", "draft", "v"), "uid": uid, "kind": "text", "text": "", "media": [], "rows": [], "opts": {}, "chans": [],
         "bn": 0, "created": now(), "updated": now()}
    d.update(kw)
    db.put("drafts", d)
    return d


def get_draft(did):
    try:
        return db.get("drafts", int(did))
    except (TypeError, ValueError):
        return None


def save_draft(d):
    d["updated"] = now()
    db.put("drafts", d)


def user_drafts(uid):
    return db.find("drafts", {"uid": uid}, sort=[("updated", -1)])


def prune_drafts(uid, keep=60):
    for d in user_drafts(uid)[keep:]:
        db.delete("drafts", d["_id"])


def snap(d):
    return copy.deepcopy({k: d.get(k) for k in SNAP_KEYS if k in d})


def draft_from(uid, s, **extra):
    base = {k: copy.deepcopy(v) for k, v in (s or {}).items() if k in SNAP_KEYS}
    base.update(extra)
    return new_draft(uid, **base)


# ----------------------------------------------------------------------------------------------------------------------
# building a post
# ----------------------------------------------------------------------------------------------------------------------
def eff(key, d, ch, u):
    return config.effective(key, (d or {}).get("opts"), (ch or {}).get("cfg"), (u or {}).get("cfg"))


def eff_text(key, ch, u):
    return config.effective(key, (ch or {}).get("cfg"), (u or {}).get("cfg"))


def var_values(ch, u, n, ts):
    tz = tz_of(u)
    df, t12 = ucfg(u, "date_fmt"), ucfg(u, "time12")
    loc = fmt.local_dt(ts, tz)
    return {"date": fmt.fmt_date(ts, tz, df), "time": fmt.fmt_clock(ts, tz, t12), "datetime": fmt.fmt_ts(ts, tz, t12, df),
            "day": fmt.DAYS[loc.weekday()].title(), "year": str(loc.year), "channel": fmt.esc(ch.get("title", "")),
            "channel_username": ("@" + ch["un"]) if ch.get("un") else "", "n": str(n)}


def compose(d, ch, u, n=None, ts=None):
    """Everything that will be sent, as a plan. Raises ComposeError when the post cannot be sent."""
    ts = ts or now()
    kind = d.get("kind", "text")
    ch = ch or {"title": "Your channel", "un": "", "cfg": {}, "n": 0}

    def ev(k):
        return eff(k, d, ch, u)

    def et(k):
        return eff_text(k, ch, u)
    body, warnings = d.get("text") or "", []
    if kind in NO_TEXT:
        if body:
            warnings.append("This kind of message cannot carry text, so the text is not sent.")
        body = ""
    else:
        if ev("hf_use"):
            body = GAP[et("hf_gap")].join(p for p in (et("header"), body, et("footer")) if p)
        if ev("variables"):
            body = fmt.expand(body, var_values(ch, u, n if n is not None else int(ch.get("n", 0)) + 1, ts))
        if ev("emoji_mode") == "plain":
            body = fmt.strip_custom_emoji(body)
        ln, limit = fmt.html_len(body), (config.TEXT_LIMIT if kind == "text" else config.CAPTION_LIMIT)
        if kind == "text" and ln == 0:
            raise ComposeError("The post has no text yet.")
        if ln > limit:
            raise ComposeError("The text is %d characters; Telegram allows %d%s. Shorten it or switch off the header/footer." % (
                ln, limit, "" if kind == "text" else " under a picture or video"))
    if kind == "album" and not d.get("media"):
        raise ComposeError("The album has no media.")
    if kind in MEDIA_KINDS and not d.get("media"):
        raise ComposeError("The post has no media.")
    rows = copy.deepcopy(d.get("rows") or [])
    if ev("def_use") and et("def_buttons"):
        drows, errs, _ = buttons.parse_quick(et("def_buttons"), 1, id_prefix="D")
        if errs:
            warnings.append("Default buttons were skipped: " + errs[0])
        else:
            rows = buttons.merge_default(rows, drows, ev("def_pos"))
    if buttons.count(rows) > config.BUTTON_MAX:
        raise ComposeError("Too many buttons (limit %d)." % config.BUTTON_MAX)
    icons = bool(ev("btn_icons")) and ev("emoji_mode") != "plain"
    vote = {"counts": bool(ev("vote_counts")), "toggle": bool(ev("vote_toggle")), "multi": bool(ev("vote_multi")), "alert": bool(ev("vote_alert")),
            "dot": bool(ev("btn_dot")), "icons": icons}
    return {"kind": kind, "text": body, "rows": rows, "markup": buttons.render(rows, None, vote["counts"], vote["dot"], icons),
            "media": copy.deepcopy(d.get("media") or []), "src": d.get("src"), "lpo": LPO.get(ev("preview")),
            "silent": bool(ev("silent")), "protect": bool(ev("protect")), "spoiler": bool(ev("spoiler")), "above": bool(ev("caption_above")),
            "pin": ev("pin"), "del": int(ev("delete_after") or 0), "album_text": et("album_text") or "👇", "warnings": warnings, "vote": vote}


def has_premium(plan):
    return "<tg-emoji" in plan["text"] or buttons.has_icons(plan["markup"])


def send_plan(chat_id, p, plain=False, reply_to=None):
    """Sends the plan. Returns {"mids": [...], "bmid": id of the message that carries the buttons}."""
    kind, text, markup = p["kind"], p["text"], p["markup"]
    if plain:
        text, markup = fmt.strip_custom_emoji(text), buttons.plain_markup(markup)
    base = {"chat_id": chat_id, "disable_notification": True if p["silent"] else None, "protect_content": True if p["protect"] else None}
    if kind == "text":
        m = tg("sendMessage", text=text, parse_mode="HTML", link_preview_options=p["lpo"], reply_markup=markup, **base)
        return {"mids": [m["message_id"]], "bmid": m["message_id"]}
    if kind == "copy":
        src = p["src"] or {}
        m = tg("copyMessage", from_chat_id=src.get("chat"), message_id=src.get("msg"), reply_markup=markup, **base)
        return {"mids": [m["message_id"]], "bmid": m["message_id"]}
    if kind == "album":
        items = []
        for i, it in enumerate(p["media"][:config.ALBUM_MAX]):
            x = {"type": it["t"], "media": it["id"]}
            if i == 0 and text:
                x.update(caption=text, parse_mode="HTML")
                if p["above"] and it["t"] in ("photo", "video"):
                    x["show_caption_above_media"] = True
            if p["spoiler"] and it["t"] in ("photo", "video"):
                x["has_spoiler"] = True
            items.append(x)
        msgs = tg("sendMediaGroup", media=items, **base)
        mids = [m["message_id"] for m in msgs]
        bmid = mids[-1]
        if markup:
            at = fmt.strip_custom_emoji(p["album_text"]) if plain else p["album_text"]
            m = tg("sendMessage", text=at, parse_mode="HTML", reply_markup=markup, **base)
            mids.append(m["message_id"])
            bmid = m["message_id"]
        return {"mids": mids, "bmid": bmid}
    media = p["media"][0]["id"]
    params = dict(base, reply_markup=markup)
    params[kind] = media
    if kind not in ("video_note", "sticker"):
        params.update(caption=text or None, parse_mode="HTML")
    if kind in ("photo", "video", "animation"):
        if p["spoiler"]:
            params["has_spoiler"] = True
        if p["above"] and text:
            params["show_caption_above_media"] = True
    m = tg({"photo": "sendPhoto", "video": "sendVideo", "animation": "sendAnimation", "document": "sendDocument", "audio": "sendAudio",
            "voice": "sendVoice", "video_note": "sendVideoNote", "sticker": "sendSticker"}[kind], **params)
    return {"mids": [m["message_id"]], "bmid": m["message_id"]}


def _send_safe(chat_id, plan):
    try:
        return send_plan(chat_id, plan)
    except TgError as e:
        if has_premium(plan) and e.kind in ("emoji", "parse", "button", "other"):
            log.info("retrying without Premium emoji: %s", e)
            return send_plan(chat_id, plan, plain=True)
        raise


def needs_live(rows):
    return any(b["k"] in ("vote", "alert") for b in buttons.flat(rows))


def publish_one(d, ch, u, ts=None):
    """Sends the draft to one channel and records it. Returns the post document."""
    ts = ts or now()
    n = int(ch.get("n", 0)) + 1
    plan = compose(d, ch, u, n=n, ts=ts)
    res = _send_safe(ch["_id"], plan)
    mids, bmid = res["mids"], res["bmid"]
    post = {"_id": "%d:%d" % (ch["_id"], mids[0]), "uid": u["_id"], "chat": ch["_id"], "title": ch["title"], "mid": mids[0], "mids": mids, "bmid": bmid,
            "kind": plan["kind"], "text": plan["text"], "rows": plan["rows"], "d": snap(d), "at": ts, "del": ts + plan["del"] if plan["del"] else 0,
            "n": n, "status": "live", "pinned": False, "link": link_for(ch, mids[0]), "warn": plan["warnings"]}
    db.put("posts", post)
    if needs_live(plan["rows"]):
        db.put("live", {"_id": "%d:%d" % (ch["_id"], bmid), "post": post["_id"], "rows": plan["rows"], "counts": {}, "users": {}, "o": plan["vote"],
                        "dirty": 0, "edited": 0})
    db.incr("channels", ch["_id"], "n")
    if plan["pin"] != "off":
        if tg.ok("pinChatMessage", chat_id=ch["_id"], message_id=mids[0], disable_notification=plan["pin"] == "silent") is not None:
            post["pinned"] = True
            db.put("posts", post)
    return post


def publish(d, u, chans, ts=None):
    """-> list of {chat, title, ok, post, link, err, kind}."""
    out = []
    for i, cid in enumerate(chans):
        ch = get_channel(cid)
        if not ch or u["_id"] not in ch.get("owners", []):
            out.append({"chat": cid, "title": str(cid), "ok": False, "err": "This channel is not in your list."})
            continue
        try:
            post = publish_one(d, ch, u, ts)
            out.append({"chat": cid, "title": ch["title"], "ok": True, "post": post["_id"], "link": post["link"], "warn": post["warn"]})
        except ComposeError as e:
            out.append({"chat": cid, "title": ch["title"], "ok": False, "err": str(e), "kind": "compose"})
        except TgError as e:
            out.append({"chat": cid, "title": ch["title"], "ok": False, "err": e.friendly(), "kind": e.kind})
        if i + 1 < len(chans):
            time.sleep(0.05)
    return out


def report_lines(results):
    lines = []
    for r in results:
        t = fmt.esc(r["title"])
        if r["ok"]:
            lines.append('✅ <a href="%s">%s</a>' % (fmt.attr(r["link"]), t) if r.get("link") else "✅ %s" % t)
        else:
            lines.append("❌ %s: %s" % (t, fmt.esc(r.get("err", "failed"))))
    return "\n".join(lines)


# ----------------------------------------------------------------------------------------------------------------------
# changing and removing published posts
# ----------------------------------------------------------------------------------------------------------------------
def _edit(method, **kw):
    try:
        return tg(method, **kw)
    except TgError as e:
        if e.kind == "same":
            return None
        raise


def apply_edit(d, u, post):
    """Changes the already published post so it matches the draft (text, buttons, options). Returns the updated post."""
    ch = get_channel(post["chat"])
    if ch is None:
        raise ComposeError("The channel is not in your list any more.")
    plan = compose(d, ch, u, n=post.get("n"), ts=post.get("at"))
    plain = False
    for attempt in (0, 1):
        text, markup = (fmt.strip_custom_emoji(plan["text"]), buttons.plain_markup(plan["markup"])) if plain else (plan["text"], plan["markup"])
        try:
            kind, cid = plan["kind"], post["chat"]
            if kind == "text":
                _edit("editMessageText", chat_id=cid, message_id=post["bmid"], text=text, parse_mode="HTML", link_preview_options=plan["lpo"], reply_markup=markup)
            elif kind == "album":
                if text != post.get("text"):
                    _edit("editMessageCaption", chat_id=cid, message_id=post["mids"][0], caption=text, parse_mode="HTML")
                has_follow = post["bmid"] not in post["mids"][:-1] and len(post["mids"]) > len(d.get("media") or [])
                if markup and has_follow:
                    _edit("editMessageReplyMarkup", chat_id=cid, message_id=post["bmid"], reply_markup=markup)
                elif markup:
                    m = tg("sendMessage", chat_id=cid, text=plan["album_text"], parse_mode="HTML", reply_markup=markup)
                    post["mids"].append(m["message_id"])
                    _rekey_live(post, m["message_id"])
                elif has_follow:
                    tg.ok("deleteMessage", chat_id=cid, message_id=post["bmid"])
                    post["mids"].remove(post["bmid"])
                    _rekey_live(post, post["mids"][-1])
            elif kind in ("sticker", "video_note", "copy"):
                _edit("editMessageReplyMarkup", chat_id=cid, message_id=post["mid"], reply_markup=markup)
            else:
                _edit("editMessageCaption", chat_id=cid, message_id=post["mid"], caption=text or "", parse_mode="HTML",
                      show_caption_above_media=True if plan["above"] else None, reply_markup=markup)
            break
        except TgError as e:
            if attempt == 0 and has_premium(plan) and e.kind in ("emoji", "parse", "button", "other"):
                plain = True
                continue
            raise
    post.update(text=plan["text"], rows=plan["rows"], d=snap(d), warn=plan["warnings"])
    db.put("posts", post)
    live = db.get("live", "%d:%d" % (post["chat"], post["bmid"]))
    if needs_live(plan["rows"]):
        live = live or {"_id": "%d:%d" % (post["chat"], post["bmid"]), "post": post["_id"], "counts": {}, "users": {}, "dirty": 0, "edited": 0}
        live.update(rows=plan["rows"], o=plan["vote"])
        db.put("live", live)
    elif live:
        db.delete("live", live["_id"])
    return post


def _rekey_live(post, new_bmid):
    old = db.get("live", "%d:%d" % (post["chat"], post["bmid"]))
    if old:
        db.delete("live", old["_id"])
        old["_id"] = "%d:%d" % (post["chat"], new_bmid)
        db.put("live", old)
    post["bmid"] = new_bmid


def delete_post(post):
    """Deletes the messages in the channel. Returns an error text or ''."""
    err = ""
    for mid in post.get("mids") or [post["mid"]]:
        try:
            tg("deleteMessage", chat_id=post["chat"], message_id=mid)
        except TgError as e:
            if e.kind != "gone":
                err = e.friendly()
    if not err:
        post["status"], post["del"] = "deleted", 0
        db.put("posts", post)
        db.delete("live", "%d:%d" % (post["chat"], post["bmid"]))
    return err


def pin_post(post, on=True, silent=False):
    try:
        if on:
            tg("pinChatMessage", chat_id=post["chat"], message_id=post["mid"], disable_notification=silent)
        else:
            tg("unpinChatMessage", chat_id=post["chat"], message_id=post["mid"])
    except TgError as e:
        return e.friendly()
    post["pinned"] = bool(on)
    db.put("posts", post)
    return ""


def user_posts(uid, status="live"):
    return db.find("posts", {"uid": uid, "status": status}, sort=[("at", -1)])


# ----------------------------------------------------------------------------------------------------------------------
# votes and popups
# ----------------------------------------------------------------------------------------------------------------------
def vote(chat, mid, uid, bid):
    """-> (toast text, show as alert)."""
    with VOTE_LOCK:
        live = db.get("live", "%d:%d" % (chat, mid))
        b = buttons.get(live["rows"], bid) if live else None
        if not b or b["k"] != "vote":
            return "This button is no longer active.", True
        o, counts = live["o"], live["counts"]
        mine = live["users"].get(str(uid), [])
        if bid in mine:
            if not o["toggle"]:
                return "You have already voted.", False
            mine.remove(bid)
            counts[bid] = max(0, counts.get(bid, 0) - 1)
            msg = "Your vote was removed."
        else:
            if mine and not o["multi"]:
                if not o["toggle"]:
                    return "You have already voted.", False
                for old in mine:
                    counts[old] = max(0, counts.get(old, 0) - 1)
                mine = []
            mine.append(bid)
            counts[bid] = counts.get(bid, 0) + 1
            msg = "Thanks for voting!"
        if mine:
            live["users"][str(uid)] = mine
        else:
            live["users"].pop(str(uid), None)
        live["dirty"] = now()
        db.put("live", live)
    refresh_live(chat, mid)
    return (msg if o["alert"] else ""), False


def popup(chat, mid, bid):
    live = db.get("live", "%d:%d" % (chat, mid))
    b = buttons.get(live["rows"], bid) if live else None
    return (b["v"], True) if b and b["k"] == "alert" else ("This button is no longer active.", True)


def refresh_live(chat, mid, force=False):
    """Updates the vote numbers on the buttons (at most once every 2 seconds per post)."""
    with VOTE_LOCK:
        live = db.get("live", "%d:%d" % (chat, mid))
        if not live or not live.get("dirty"):
            return
        if not force and now() - live.get("edited", 0) < 3:
            return
        live["dirty"], live["edited"] = 0, now()
        db.put("live", live)
    o = live["o"]
    markup = buttons.render(live["rows"], live["counts"], o["counts"], o["dot"], o["icons"])
    try:
        tg("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup=markup, _tries=1)
    except TgError as e:
        if e.kind == "flood":                                           # try again a little later (the scheduler tick)
            with VOTE_LOCK:
                live = db.get("live", "%d:%d" % (chat, mid))
                if live:
                    live["dirty"], live["edited"] = now(), now() + max(2, int(e.retry_after or 5))
                    db.put("live", live)
        elif e.kind == "emoji" or buttons.has_icons(markup):
            tg.ok("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup=buttons.plain_markup(markup))
        elif e.kind not in ("same", "gone"):
            log.info("vote refresh failed: %s", e)


def tick_live():
    for live in db.find("live", {"dirty": {"$gt": 0}}):
        chat, mid = live["_id"].split(":")
        refresh_live(int(chat), int(mid))


# ----------------------------------------------------------------------------------------------------------------------
# scheduled posts and auto-delete
# ----------------------------------------------------------------------------------------------------------------------
def notify(uid, text, kb=None):
    return tg.ok("sendMessage", chat_id=uid, text=text, parse_mode="HTML", link_preview_options={"is_disabled": True}, reply_markup=kb)


def sched_id():
    return "s%d" % db.incr("counters", "sched", "v")


def make_sched(u, d, chans, when, rep=None, sid=None):
    s = db.get("sched", sid) if sid else None
    s = s or {"_id": sched_id(), "uid": u["_id"], "runs": 0, "fails": 0, "created": now()}
    s.update(d=snap(d), chans=list(chans), when=int(when), rep=rep or {}, status="wait")
    db.put("sched", s)
    return s


def run_sched(s, ts=None):
    """Publishes one scheduled item now and plans the next run. Returns the results."""
    ts = ts or now()
    u = get_user(s["uid"])
    results = publish(s["d"], u, s["chans"], ts)
    ok = any(r["ok"] for r in results)
    late = ts - s["when"]
    rep = s.get("rep") or {}
    s["runs"] = s.get("runs", 0) + (1 if ok else 0)
    s["fails"] = 0 if ok else s.get("fails", 0) + 1
    if rep.get("kind") and s["fails"] < 3:
        s["when"] = fmt.next_repeat(s["when"], rep, tz_of(u), ts)
        s["status"] = "wait"
        db.put("sched", s)
    elif rep.get("kind"):
        s["status"] = "paused"
        db.put("sched", s)
    else:
        db.delete("sched", s["_id"])
    bad = [r for r in results if not r["ok"]]
    if (ucfg(u, "notify_sched") and ok) or (ucfg(u, "notify_errors") and bad):
        head = "🗓 <b>Scheduled post sent</b>" if ok and not bad else ("⚠️ <b>Scheduled post: some channels failed</b>" if ok else "❌ <b>Scheduled post failed</b>")
        if late > 300:
            head += "\n<i>It was %s late (the bot was asleep).</i>" % fmt.fmt_dur(late)
        if s["status"] == "paused":
            head += "\n⏸ Repeating was paused after 3 failures."
        elif rep.get("kind"):
            head += "\n🔁 Next: %s" % fmt.fmt_ts(s["when"], tz_of(u), ucfg(u, "time12"), ucfg(u, "date_fmt"))
        notify(u["_id"], head + "\n\n" + report_lines(results))
    return results


def tick_sched():
    for s in db.find("sched", {"status": "wait", "when": {"$lte": now()}}, sort=[("when", 1)], limit=20):
        s["status"] = "busy"
        db.put("sched", s)
        try:
            run_sched(s)
        except Exception:
            log.exception("scheduled post %s failed", s.get("_id"))
            s["status"] = "paused"
            db.put("sched", s)


def tick_deletes():
    for p in db.find("posts", {"status": "live", "del": {"$gt": 0, "$lte": now()}}, limit=30):
        err = delete_post(p)
        if err:
            p["del"] = now() + 3600                                    # try again in an hour
            db.put("posts", p)
            continue
        u = get_user(p["uid"])
        if ucfg(u, "notify_delete"):
            notify(p["uid"], "🗑 A post in <b>%s</b> was deleted automatically (post #%s)." % (fmt.esc(p["title"]), p.get("n", "?")))


def tick():
    for fn in (tick_live, tick_sched, tick_deletes):
        try:
            fn()
        except Exception:
            log.exception("%s failed", fn.__name__)


def stats():
    return {"users": db.count("users"), "channels": db.count("channels"), "drafts": db.count("drafts"), "posts": db.count("posts", {"status": "live"}),
            "sched": db.count("sched"), "templates": db.count("templates")}
