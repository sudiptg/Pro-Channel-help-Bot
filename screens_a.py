"""Screens, part 1: main menu, new posts, drafts, text / media / buttons editing, channels of a draft, schedule, publishing."""
import logging

import buttons
import config
import core
import fmt
import ui
from tgapi import TgError
from ui import esc, inputs, paged, pager, plain_snip, screen, snip, style_name

log = logging.getLogger("screens")

KIND_ICON = {"text": "📝", "photo": "🖼", "video": "🎬", "animation": "🎞", "document": "📎", "audio": "🎵", "voice": "🎤", "video_note": "⭕",
             "sticker": "💟", "album": "🗂", "copy": "📨"}
KIND_NAME = {"text": "Text", "photo": "Photo", "video": "Video", "animation": "GIF", "document": "File", "audio": "Audio", "voice": "Voice",
             "video_note": "Video note", "sticker": "Sticker", "album": "Album", "copy": "Copied message"}
MEDIA_FIELDS = ["photo", "video", "animation", "audio", "voice", "video_note", "sticker", "document"]
QUICK_TIMES = [("1h", "In 1 hour", "in 1h"), ("3h", "In 3 hours", "in 3h"), ("t8", "Tonight 8 PM", "8pm"), ("m9", "Tomorrow 9 AM", "tomorrow 9am"),
               ("m6", "Tomorrow 6 PM", "tomorrow 6pm"), ("s10", "Saturday 10 AM", "sat 10am")]
REPEATS = [("0", "Once", {}), ("6h", "Every 6 hours", {"kind": "interval", "secs": 21600}), ("12h", "Every 12 hours", {"kind": "interval", "secs": 43200}),
           ("d", "Every day", {"kind": "daily"}), ("w", "Every week", {"kind": "weekly"}), ("m", "Every month", {"kind": "monthly"})]
FORMAT_HELP = "<b>Formatting:</b> select text in the message box and use Telegram's own bold / italic / link / spoiler / quote tools. They are kept."


def need_draft(fn):
    def wrapper(ctx, did="0", *a):
        d = core.get_draft(did)
        if not d or d["uid"] != ctx.uid:
            ctx.toast("This draft no longer exists.", True)
            return menu(ctx)
        return fn(ctx, d, *a)
    wrapper.__name__ = fn.__name__
    return wrapper


def input_draft(fn):
    def wrapper(ctx, msg, st):
        d = core.get_draft(st.get("d"))
        if not d or d["uid"] != ctx.uid:
            ctx.clear()
            return menu(ctx, fresh=True)
        return fn(ctx, msg, st, d)
    wrapper.__name__ = fn.__name__
    return wrapper


def touch(ctx, d):
    core.save_draft(d)
    ctx.stale()


# ----------------------------------------------------------------------------------------------------------------------
# main menu
# ----------------------------------------------------------------------------------------------------------------------
@screen("m")
def menu(ctx, *a, fresh=False):
    uid = ctx.uid
    ctx.drop_preview()                                                   # leaving a draft: its preview message is not needed any more
    n_ch, n_dr = len(core.my_channels(uid)), len(core.user_drafts(uid))
    n_sc, n_po = core.db.count("sched", {"uid": uid}), core.db.count("posts", {"uid": uid, "status": "live"})
    text = "<b>📣 %s</b>\nCreate channel posts with colored buttons, schedule them and manage everything from one place.\n\n" % esc(config.Config.APP_NAME)
    text += "📢 Channels <b>%d</b>  ·  📝 Drafts <b>%d</b>  ·  🗓 Scheduled <b>%d</b>  ·  📚 Posts <b>%d</b>" % (n_ch, n_dr, n_sc, n_po)
    if n_ch == 0:
        text += "\n\n👉 Start by adding the bot as an <b>admin</b> of your channel. It will show up here by itself."
    if core.is_owner(uid) and getattr(core.db, "error", ""):
        text += "\n\n⚠️ <i>%s</i>" % esc(core.db.error)
    text += ctx.tip()
    kb = ctx.kb()
    kb.row(kb.btn("✍️ New post", "nw", style="success"), kb.btn("📢 Channels", "cl", 0, style="primary"))
    kb.row(kb.btn("📝 Drafts (%d)" % n_dr, "dl", 0), kb.btn("🗓 Scheduled (%d)" % n_sc, "sl", 0))
    kb.row(kb.btn("📚 My posts", "pl", 0), kb.btn("📋 Templates", "tl", 0))
    kb.row(kb.btn("⚙️ Settings", "gm", "u"), kb.btn("❓ Help", "h"))
    if core.is_owner(uid):
        kb.row(kb.btn("👑 Admin", "ad"))
    ctx.show(text, kb, fresh=fresh)


@screen("x")
def noop(ctx, *a):
    ctx.toast()


# ----------------------------------------------------------------------------------------------------------------------
# reading what the user sends
# ----------------------------------------------------------------------------------------------------------------------
def parse_content(msg):
    if msg.get("text") is not None:
        body, ents = msg["text"], msg.get("entities")
    else:
        body, ents = msg.get("caption") or "", msg.get("caption_entities")
    html = fmt.entities_to_html(body, ents)
    for kind in MEDIA_FIELDS:
        v = msg.get(kind)
        if v:
            fid = v[-1]["file_id"] if kind == "photo" else v["file_id"]
            return {"kind": kind, "text": html, "item": {"t": kind, "id": fid, "m": msg["message_id"]}}
    if msg.get("text") is not None:
        return {"kind": "text", "text": html, "item": None}
    for key in ("poll", "location", "venue", "contact", "dice", "game"):
        if msg.get(key):
            return {"kind": "copy", "text": "", "item": None, "src": {"chat": msg["chat"]["id"], "msg": msg["message_id"]}}
    return None


def can_group(items):
    kinds = {i["t"] for i in items}
    if kinds - {"photo", "video", "document", "audio"}:
        return False
    return kinds <= {"photo", "video"} or len(kinds) == 1


def default_chans(ctx):
    mine = {c["_id"] for c in core.my_channels(ctx.uid)}
    if len(mine) == 1:
        return list(mine)
    return [c for c in (ctx.u.get("last") or []) if c in mine] if not ctx.cfg("ask_channels") else []


def intake(ctx, msg, mode="new", d=None):
    """Turns a message into a draft (mode new), or puts its media into a draft (replace / add).
    Returns the draft, or None after telling the user why not."""
    c = parse_content(msg)
    if not c:
        ctx.say("⚠️ I cannot use that kind of message. Send text, a picture, a video, a file or a voice message.")
        return None
    mg, item = msg.get("media_group_id"), c["item"]
    if ctx.cfg("clean_chat") and c["kind"] != "copy":
        ctx.forget_input(msg)                                            # the bot keeps the file id, the message itself is not needed
    if mg:                                                               # the next picture of an album that is arriving
        for x in core.user_drafts(ctx.uid)[:12]:
            if x.get("mg") == mg:
                d, mode = x, "group"
                break
    if mode == "new":
        d = core.new_draft(ctx.uid, kind=c["kind"], text=c["text"], media=[item] if item else [], chans=default_chans(ctx), mg=mg or "")
        if c.get("src"):
            d["src"] = c["src"]
            core.save_draft(d)
        core.prune_drafts(ctx.uid)
        return d
    if not item:
        ctx.say("⚠️ Send a picture, a video or a file here. (Text is changed with the ✏️ Text button.)")
        return None
    if mode == "group":
        items = sorted(d["media"] + [item], key=lambda i: i.get("m", 0)) if all(i["id"] != item["id"] for i in d["media"]) else d["media"]
        if not can_group(items):
            ctx.say("⚠️ That file type cannot be in the same album (photos and videos mix; files and audio only with their own kind).")
            return d
        d["media"], d["kind"] = items, "album" if len(items) > 1 else items[0]["t"]
        if c["text"] and not d["text"]:
            d["text"] = c["text"]
    elif mode == "replace" or d["kind"] == "text":
        d["media"], d["kind"], d["mg"] = [item], item["t"], mg or ""
        if c["text"]:
            d["text"] = c["text"]
    else:                                                                # add to an album / single media
        items = d["media"] + [item]
        if len(items) > config.ALBUM_MAX:
            ctx.say("⚠️ An album can hold %d items." % config.ALBUM_MAX)
            return d
        if not can_group(items):
            ctx.say("⚠️ That file type cannot be in the same album (photos and videos mix; files and audio only with their own kind).")
            return d
        d["media"], d["kind"], d["mg"] = items, "album", mg or ""
    touch(ctx, d)
    return d


def after_intake(ctx, msg, d):
    """Shows the draft (an album waits a moment for its remaining pictures)."""
    ctx.clear()
    if msg.get("media_group_id"):
        ui.HOOKS["defer"](ctx.uid, d["_id"])
    else:
        draft_screen(ctx, d, force=True)


@screen("nw")
def new_post(ctx):
    kb = ctx.kb()
    kb.row(kb.btn("⬅️ Cancel", "m", style="danger"))
    ctx.ask("new", "<b>✍️ New post</b>\nSend the post now: text, photo, video, GIF, file, audio, voice, sticker, or several pictures as an album.\n"
            "A message forwarded from any chat works too.\n\n" + FORMAT_HELP, kb)


@inputs("new")
def got_new(ctx, msg, st):
    d = intake(ctx, msg, "new")
    if d:
        if st.get("chat"):
            d["chans"] = [int(st["chat"])]
            core.save_draft(d)
        after_intake(ctx, msg, d)


# ----------------------------------------------------------------------------------------------------------------------
# the draft screen: the real post (preview) + a control panel
# ----------------------------------------------------------------------------------------------------------------------
def first_channel(d):
    for cid in d.get("chans", []):
        ch = core.get_channel(cid)
        if ch:
            return ch
    return None


def send_preview(ctx, d):
    ch = first_channel(d)
    mids, warns = [], []
    try:
        plan = core.compose(d, ch, ctx.u)
        plan.update(silent=True, protect=False)
        res = core._send_safe(ctx.uid, plan)
        mids, warns = res["mids"], plan["warnings"]
    except core.ComposeError as e:
        m = ctx.say("⚠️ <b>Preview:</b> %s" % esc(e))
        mids = [m["message_id"]] if m else []
    except TgError as e:
        m = ctx.say("⚠️ <b>Preview failed:</b> %s" % esc(e.friendly()))
        mids = [m["message_id"]] if m else []
        warns = [e.friendly()]
    ctx.u["prev"] = {"c": ctx.uid, "ms": mids, "d": d["_id"], "stale": False, "warn": warns}


def opt_summary(d):
    parts = []
    for k, v in sorted((d.get("opts") or {}).items()):
        s = config.BY_KEY.get(k)
        if s and v is not None:
            parts.append("%s: %s" % (s.label, s.show(v)))
    return ", ".join(parts)


def draft_panel(ctx, d, warns, note=""):
    chans = [core.get_channel(c) for c in d.get("chans", [])]
    names = ", ".join(esc(c["title"]) for c in chans if c)
    nb = buttons.count(d.get("rows") or [])
    col = sum(1 for b in buttons.flat(d.get("rows") or []) if b.get("s"))
    kind = d["kind"] if d["kind"] != "album" else "album"
    head = "📝 <b>Draft #%d</b> · %s %s" % (d["_id"], KIND_ICON.get(kind, "📝"), KIND_NAME.get(kind, kind))
    if kind == "album":
        head += " (%d)" % len(d["media"])
    lines = [head]
    if d.get("edit_of"):
        lines.insert(0, "✏️ <b>Editing a published post</b>")
    elif d.get("sched_of"):
        lines.insert(0, "🗓 <b>Editing a scheduled post</b>")
    if not d.get("edit_of"):
        lines.append("📢 " + (names or "⚠️ <i>no channel selected</i>"))
    lines.append("🔘 %d button%s%s" % (nb, "" if nb == 1 else "s", " · 🎨 %d colored" % col if col else ""))
    if d.get("when") and not d.get("edit_of"):
        lines.append("🕒 " + ctx.fts(d["when"]) + (" · 🔁 repeats" if d.get("rep") else ""))
    o = opt_summary(d)
    if o:
        lines.append("⚙️ " + esc(o))
    for w in warns or []:
        lines.append("⚠️ " + esc(w))
    if note:
        lines.append("\n" + note)
    kb, i = ctx.kb(), d["_id"]
    if d.get("edit_of"):
        kb.row(kb.btn("✅ Apply changes", "de", i, style="success"))
    elif d.get("sched_of"):
        kb.row(kb.btn("✅ Save schedule", "dsok", i, style="success"), kb.btn("🕒 Time", "ds", i, style="primary"))
    else:
        kb.row(kb.btn("🚀 Publish now", "dp", i, style="success"), kb.btn("🕒 Schedule", "ds", i, style="primary"))
    media_btn = kb.btn("🖼 Add media" if d["kind"] == "text" else "🖼 Media", "dm", i) if not d.get("edit_of") else None
    kb.row(kb.btn("✏️ Text", "dt", i), media_btn)
    kb.row(kb.btn("🔘 Buttons (%d)" % nb, "db", i, style="primary" if nb == 0 else ""), kb.btn("⚙️ Options", "do", i))
    if not d.get("edit_of"):
        kb.row(kb.btn("📢 Channels (%d)" % len([c for c in chans if c]), "dc", i, 0, style="" if chans else "danger"), kb.btn("📋 Save template", "dz", i))
    kb.row(kb.btn("❌ Cancel editing" if d.get("edit_of") or d.get("sched_of") else "🗑 Discard", "dx", i, style="danger"), kb.btn("🏠 Menu", "m"))
    return "\n".join(lines), kb


def draft_screen(ctx, d, force=False, note=""):
    prev = ctx.u.get("prev") or {}
    ctx.u["cur"] = d["_id"]
    if force or prev.get("d") != d["_id"] or prev.get("stale") or not prev.get("ms"):
        ctx.drop_preview()
        send_preview(ctx, d)
        text, kb = draft_panel(ctx, d, ctx.u["prev"].get("warn"), note)
        ctx.show(text, kb, fresh=True)
    else:
        text, kb = draft_panel(ctx, d, prev.get("warn"), note)
        ctx.show(text, kb)


@screen("d")
@need_draft
def open_draft(ctx, d, with_preview="0"):
    draft_screen(ctx, d, force=with_preview == "1")


@screen("dl")
def drafts_list(ctx, p="0"):
    rows = core.user_drafts(ctx.uid)
    page, p, pages = paged(rows, p, ctx.size)
    kb = ctx.kb()
    for d in page:
        ch = ", ".join(c["title"] for c in map(core.get_channel, d.get("chans", [])) if c)
        kb.row(kb.btn("%s #%d · %s" % (KIND_ICON.get(d["kind"], "📝"), d["_id"], plain_snip(d.get("text"), 24)), "d", d["_id"], 1))
    pager(kb, "dl", [], p, pages)
    kb.row(kb.btn("✍️ New post", "nw", style="success"), kb.btn("🏠 Menu", "m"))
    ctx.show("<b>📝 Drafts</b>\n" + ("Tap a draft to continue." if rows else "No drafts yet. Send me anything to start one."), kb)


# ----------------------------------------------------------------------------------------------------------------------
# text
# ----------------------------------------------------------------------------------------------------------------------
@screen("dt")
@need_draft
def edit_text(ctx, d, html="0"):
    if d["kind"] in core.NO_TEXT:
        return ctx.toast("This kind of message cannot have text.", True)
    kb, i = ctx.kb(), d["_id"]
    if html == "1":
        kb.row(kb.btn("⬅️ Back", "dt", i))
        text = ("<b>🧾 HTML mode</b>\nSend the text with HTML tags:\n<code>&lt;b&gt;bold&lt;/b&gt; &lt;i&gt;italic&lt;/i&gt; &lt;u&gt;underline&lt;/u&gt; "
                "&lt;s&gt;strike&lt;/s&gt; &lt;code&gt;code&lt;/code&gt; &lt;tg-spoiler&gt;spoiler&lt;/tg-spoiler&gt; &lt;blockquote&gt;quote&lt;/blockquote&gt; "
                "&lt;a href=\"https://…\"&gt;link&lt;/a&gt; &lt;tg-emoji emoji-id=\"ID\"&gt;🙂&lt;/tg-emoji&gt;</code>")
    else:
        kb.row(kb.btn("🧾 HTML mode", "dt", i, 1), kb.btn("🧹 Clear text", "dtc", i, style="danger"))
        kb.row(kb.btn("⬅️ Back", "d", i))
        cur = ("\n\nNow: " + snip(d.get("text"), 120)) if d.get("text") else ""
        text = "<b>✏️ %s</b>\nSend the new text.%s\n\n%s\n<i>Tip: {date}, {time}, {channel} … are filled in when the post goes out.</i>" % (
            "Post text" if d["kind"] == "text" else "Caption", cur, FORMAT_HELP)
    ctx.ask("text", text, kb, d=d["_id"], html=1 if html == "1" else 0)


@inputs("text")
@input_draft
def got_text(ctx, msg, st, d):
    src = msg.get("text") if msg.get("text") is not None else msg.get("caption")
    if src is None:
        return ctx.say("⚠️ Please send text.")
    if st.get("html"):
        html, err = fmt.sanitize_html(src)
        if err:
            return ctx.say("⚠️ " + esc(err))
    else:
        html = fmt.entities_to_html(src, msg.get("entities") if msg.get("text") is not None else msg.get("caption_entities"))
    d["text"] = html
    touch(ctx, d)
    ctx.forget_input(msg)
    ctx.clear()
    draft_screen(ctx, d, force=True)


@screen("dtc")
@need_draft
def clear_text(ctx, d):
    if d["kind"] == "text":
        return ctx.toast("A text post needs text. Send new text instead.", True)
    d["text"] = ""
    touch(ctx, d)
    draft_screen(ctx, d, force=True)


# ----------------------------------------------------------------------------------------------------------------------
# media
# ----------------------------------------------------------------------------------------------------------------------
@screen("dm")
@need_draft
def media_menu(ctx, d):
    kb, i = ctx.kb(), d["_id"]
    if d["kind"] == "text":
        text = "<b>🖼 Add media</b>\nA picture, video or file can go on top of the text (the text becomes its caption, max 1024 characters)."
        kb.row(kb.btn("➕ Send a file", "dmr", i, style="success"))
    else:
        text = "<b>🖼 Media</b> · %s %s (%d)\nReplace it, add more files (album) or remove it." % (KIND_ICON.get(d["kind"]), KIND_NAME.get(d["kind"]), len(d["media"]))
        kb.row(kb.btn("🔄 Replace", "dmr", i, style="primary"), kb.btn("➕ Add to album", "dma", i))
        if d["kind"] == "album":
            kb.row(kb.btn("↩️ Remove last item", "dml", i))
        kb.row(kb.btn("🗑 Remove media", "dmx", i, style="danger"))
    kb.row(kb.btn("⬅️ Back", "d", i))
    ctx.show(text, kb)


def _ask_media(ctx, d, mode, text):
    kb = ctx.kb()
    kb.row(kb.btn("⬅️ Back", "dm", d["_id"]))
    ctx.ask("media", text, kb, d=d["_id"], mode=mode)


@screen("dmr")
@need_draft
def media_replace(ctx, d):
    _ask_media(ctx, d, "replace", "<b>🔄 Send the new picture, video or file</b>\nSeveral pictures at once become an album.")


@screen("dma")
@need_draft
def media_add(ctx, d):
    if d["kind"] == "text":
        return media_replace(ctx, d["_id"])
    _ask_media(ctx, d, "add", "<b>➕ Send more pictures, videos or files</b>\nUp to %d in one album." % config.ALBUM_MAX)


@inputs("media")
@input_draft
def got_media(ctx, msg, st, d):
    d2 = intake(ctx, msg, st.get("mode", "replace"), d)
    if d2:
        ctx.forget_input(msg) if not msg.get("media_group_id") else None
        after_intake(ctx, msg, d2)


@screen("dmx")
@need_draft
def media_remove(ctx, d):
    if not fmt.html_len(d.get("text")):
        return ctx.toast("Add some text first, then the media can be removed.", True)
    d["media"], d["kind"], d["mg"] = [], "text", ""
    touch(ctx, d)
    draft_screen(ctx, d, force=True)


@screen("dml")
@need_draft
def media_last(ctx, d):
    if len(d["media"]) > 1:
        d["media"].pop()
        d["kind"] = "album" if len(d["media"]) > 1 else d["media"][0]["t"]
        touch(ctx, d)
    draft_screen(ctx, d, force=True)


# ----------------------------------------------------------------------------------------------------------------------
# buttons
# ----------------------------------------------------------------------------------------------------------------------
def editor_label(ctx, b):
    t = b["t"]
    if b["k"] != "url" and not t.startswith(buttons.KIND_ICON[b["k"]]):
        t = buttons.KIND_ICON[b["k"]] + " " + t
    if not ctx.cfg("ui_colors") and b.get("s"):
        t = config.STYLE_DOT[b["s"]] + " " + t
    return t


@screen("db")
@need_draft
def btn_editor(ctx, d, note=""):
    kb, i = ctx.kb(), d["_id"]
    rows = d.get("rows") or []
    for row in rows:
        kb.row(*[kb.btn(editor_label(ctx, b), "dbe", i, b["id"], style=b.get("s", "")) for b in row])
    n = buttons.count(rows)
    kb.row(kb.btn("➕ Add button", "dba", i, style="success"), kb.btn("📥 Paste many", "dbq", i, style="primary"))
    if n:
        kb.row(kb.btn("🎨 Paint all", "dbp", i), kb.btn("📄 As text", "dbt", i), kb.btn("🧹 Clear", "dbc", i, style="danger"))
    kb.row(kb.btn("⬅️ Back", "d", i))
    text = ("<b>🔘 Buttons</b> · %d in %d row%s\n" % (n, len(rows), "" if len(rows) == 1 else "s")) + (
        "Tap a button to change it, move it or delete it." if n else "No buttons yet. Add one, or paste many at once.\n\n"
        "<b>Quick format</b> (one line = one row):\n<code>Watch now - https://example.com {green}\nEp 1 - https://a.com | Ep 2 - https://b.com\n👍 - vote</code>")
    ctx.show(text + (("\n\n" + note) if note else ""), kb)


@screen("dba")
@need_draft
def btn_add_menu(ctx, d):
    kb, i = ctx.kb(), d["_id"]
    kb.row(kb.btn("🔗 Link", "dbk", i, "url", style="primary"), kb.btn("👍 Vote counter", "dbk", i, "vote", style="success"))
    kb.row(kb.btn("💬 Popup message", "dbk", i, "alert"), kb.btn("📋 Copy text", "dbk", i, "copy"))
    kb.row(kb.btn("📤 Share", "dbk", i, "share"))
    kb.row(kb.btn("⬅️ Back", "db", i))
    ctx.show("<b>➕ New button</b>\nWhat should the button do?\n\n"
             "🔗 <b>Link</b>: opens a website or Telegram link\n👍 <b>Vote</b>: counts taps (👍 12)\n💬 <b>Popup</b>: shows a message when tapped\n"
             "📋 <b>Copy</b>: copies a text (codes, links)\n📤 <b>Share</b>: opens the share dialog", kb)


@screen("dbk")
@need_draft
def btn_add_kind(ctx, d, kind="url"):
    if buttons.count(d["rows"]) >= config.BUTTON_MAX:
        return ctx.toast("You reached the limit of %d buttons." % config.BUTTON_MAX, True)
    kb = ctx.kb()
    kb.row(kb.btn("⬅️ Back", "dba", d["_id"]))
    hint = {"vote": "e.g. 👍", "url": "e.g. Watch now", "alert": "e.g. Get code", "copy": "e.g. Copy link", "share": "e.g. Share"}[kind]
    ctx.ask("bl", "<b>%s New %s button</b>\nSend the button text (%s).\n\nShortcut: send a whole line like <code>Watch now - https://example.com {green}</code>."
            % (buttons.KIND_ICON[kind], buttons.KIND_NAME[kind].lower(), hint), kb, d=d["_id"], k=kind)


def add_button(ctx, d, kind, label, value):
    b = buttons.make(kind, label, value, ctx.cfg("btn_style"), "", buttons.alloc(d))
    buttons.add(d["rows"], b, ctx.cfg("btn_place") == "row")
    return b


@inputs("bl")
@input_draft
def got_label(ctx, msg, st, d):
    text = (msg.get("text") or "").strip()
    if not text:
        return ctx.say("⚠️ Please send the button text.")
    if re_line(text):
        rows, errs, nxt = buttons.parse_quick(text, d.get("bn", 0) + 1, ctx.cfg("btn_style"))
        if not errs:
            d["bn"] = nxt - 1
            for row in rows:
                for j, b in enumerate(row):
                    buttons.add(d["rows"], b, j == 0 or ctx.cfg("btn_place") == "row")
            return finish_button(ctx, d, msg)
        if " - " in text and any(k in text.lower() for k in ("http", "vote", "popup", "copy", "share", "www", ".")):
            return ctx.say("⚠️ " + esc(errs[0]))
    if st["k"] == "vote":
        add_button(ctx, d, "vote", text, "")
        return finish_button(ctx, d, msg)
    kb = ctx.kb()
    kb.row(kb.btn("⬅️ Back", "dba", d["_id"]))
    ask = {"url": "the link (https://… or @username)", "alert": "the popup text (up to %d characters)" % buttons.ALERT_MAX,
           "copy": "the text to copy (up to %d characters)" % buttons.COPY_MAX, "share": "the link to share (optional: <code>link :: message text</code>)"}[st["k"]]
    ctx.forget_input(msg)
    ctx.ask("bv", "<b>%s %s</b>\nNow send %s." % (buttons.KIND_ICON[st["k"]], esc(text), ask), kb, d=d["_id"], k=st["k"], t=text)


def re_line(text):
    return " - " in text or " – " in text or " — " in text


def finish_button(ctx, d, msg, note=""):
    touch(ctx, d)
    ctx.forget_input(msg)
    ctx.clear()
    btn_editor(ctx, d["_id"], note)


@inputs("bv")
@input_draft
def got_value(ctx, msg, st, d):
    v, err = buttons.check_value(st["k"], msg.get("text") or "")
    if err:
        return ctx.say("⚠️ " + esc(err))
    add_button(ctx, d, st["k"], st["t"], v)
    finish_button(ctx, d, msg)


@screen("dbq")
@need_draft
def btn_paste(ctx, d):
    kb = ctx.kb()
    kb.row(kb.btn("⬅️ Back", "db", d["_id"]))
    ctx.ask("bq", "<b>📥 Paste buttons</b>\nSend all buttons in one message. One line = one row; ` | ` puts buttons side by side.\n\n"
            "<code>Watch now - https://example.com {green}\nEp 1 - https://a.com | Ep 2 - https://b.com {blue}\n👍 - vote\n"
            "Get code - popup: Your code is 1234\nCopy - copy: SOME-CODE\nShare - share: https://example.com</code>\n\n"
            "Colors: <code>{blue} {green} {red} {plain}</code>   Premium icon: <code>{emoji:ID}</code>", kb, d=d["_id"])


@inputs("bq")
@input_draft
def got_paste(ctx, msg, st, d):
    rows, errs, nxt = buttons.parse_quick(msg.get("text") or "", d.get("bn", 0) + 1, ctx.cfg("btn_style"))
    if errs:
        return ctx.say("⚠️ <b>Could not read the buttons:</b>\n" + "\n".join(esc(e) for e in errs[:6]))
    if not rows:
        return ctx.say("⚠️ I found no buttons in that message.")
    if buttons.count(rows) + buttons.count(d["rows"]) > config.BUTTON_MAX and d["rows"]:
        return ctx.say("⚠️ Too many buttons together. Use ♻️ replace or send fewer.")
    ctx.u["tmp"] = {"rows": rows, "bn": nxt - 1, "d": d["_id"]}
    ctx.forget_input(msg)
    ctx.clear()
    if not d["rows"]:
        return paste_apply(ctx, d, "r")
    kb = ctx.kb()
    kb.row(kb.btn("➕ Add below", "dbqa", d["_id"], "a", style="success"), kb.btn("♻️ Replace all", "dbqa", d["_id"], "r", style="primary"))
    kb.row(kb.btn("❌ Cancel", "db", d["_id"], style="danger"))
    ctx.show("<b>📥 %d button%s ready.</b>\nAdd them below the existing buttons, or replace everything?" % (buttons.count(rows), "" if buttons.count(rows) == 1 else "s"), kb, fresh=True)


def paste_apply(ctx, d, how):
    tmp = ctx.u.get("tmp") or {}
    if tmp.get("d") != d["_id"] or not tmp.get("rows"):
        return btn_editor(ctx, d["_id"], "Nothing to add.")
    d["rows"] = tmp["rows"] if how == "r" else d["rows"] + tmp["rows"]
    d["bn"] = tmp["bn"]
    ctx.u["tmp"] = {}
    touch(ctx, d)
    btn_editor(ctx, d["_id"], "✅ Buttons added.")


@screen("dbqa")
@need_draft
def paste_choice(ctx, d, how="a"):
    paste_apply(ctx, d, how)


@screen("dbe")
@need_draft
def btn_edit(ctx, d, bid=""):
    b = buttons.get(d["rows"], bid)
    if not b:
        return btn_editor(ctx, d["_id"])
    i, kb = d["_id"], ctx.kb()
    value = {"url": b["v"], "vote": "counts taps", "alert": b["v"], "copy": b["v"], "share": b["v"]}[b["k"]]
    text = "<b>%s Button</b> “%s”\nType: %s\n%s: %s\nColor: <b>%s</b>%s" % (
        buttons.KIND_ICON[b["k"]], esc(b["t"]), buttons.KIND_NAME[b["k"]], {"url": "Link", "vote": "Action", "alert": "Popup", "copy": "Copies", "share": "Shares"}[b["k"]],
        esc(fmt.shorten(value, 80)), style_name(b.get("s")), ("\nPremium icon: %s" % b["i"]) if b.get("i") else "")
    kb.row(kb.btn("✏️ Text", "dbl", i, bid), kb.btn("🔗 Target", "dbv", i, bid) if b["k"] != "vote" else None, kb.btn("🎨 Color", "dbc2", i, bid, style=b.get("s", "")))
    kb.row(kb.btn("😀 Premium icon", "dbi", i, bid), kb.btn("⬅️", "dbm", i, bid, "left"), kb.btn("➡️", "dbm", i, bid, "right"))
    kb.row(kb.btn("⬆️ Up", "dbm", i, bid, "up"), kb.btn("⬇️ Down", "dbm", i, bid, "down"), kb.btn("🗑 Delete", "dbd", i, bid, style="danger"))
    kb.row(kb.btn("↩️ Back to buttons", "db", i))
    ctx.show(text, kb)


@screen("dbm")
@need_draft
def btn_move(ctx, d, bid="", where="left"):
    d["rows"] = buttons.move(d["rows"], bid, where)
    touch(ctx, d)
    btn_edit(ctx, d["_id"], bid)


@screen("dbd")
@need_draft
def btn_delete(ctx, d, bid=""):
    d["rows"] = buttons.remove(d["rows"], bid)
    touch(ctx, d)
    btn_editor(ctx, d["_id"], "🗑 Button deleted.")


@screen("dbl")
@need_draft
def btn_edit_label(ctx, d, bid=""):
    kb = ctx.kb()
    kb.row(kb.btn("⬅️ Back", "dbe", d["_id"], bid))
    ctx.ask("bel", "<b>✏️ Button text</b>\nSend the new text.", kb, d=d["_id"], b=bid)


@inputs("bel")
@input_draft
def got_new_label(ctx, msg, st, d):
    b = buttons.get(d["rows"], st["b"])
    t = (msg.get("text") or "").strip()
    if not t or not b:
        return ctx.say("⚠️ Please send the button text.")
    b["t"] = t[:config.LABEL_MAX]
    touch(ctx, d)
    ctx.forget_input(msg)
    ctx.clear()
    btn_edit(ctx, d["_id"], b["id"])


@screen("dbv")
@need_draft
def btn_edit_value(ctx, d, bid=""):
    b = buttons.get(d["rows"], bid)
    kb = ctx.kb()
    kb.row(kb.btn("⬅️ Back", "dbe", d["_id"], bid))
    ctx.ask("bev", "<b>🔗 New target</b>\nSend the new %s." % {"url": "link", "alert": "popup text", "copy": "text to copy", "share": "link to share"}.get(b["k"] if b else "url"),
            kb, d=d["_id"], b=bid)


@inputs("bev")
@input_draft
def got_new_value(ctx, msg, st, d):
    b = buttons.get(d["rows"], st["b"])
    v, err = buttons.check_value(b["k"], msg.get("text") or "") if b else (None, "The button is gone.")
    if err:
        return ctx.say("⚠️ " + esc(err))
    b["v"] = v
    touch(ctx, d)
    ctx.forget_input(msg)
    ctx.clear()
    btn_edit(ctx, d["_id"], b["id"])


@screen("dbc2")
@need_draft
def btn_color(ctx, d, bid=""):
    kb, i = ctx.kb(), d["_id"]
    kb.row(kb.btn("🔵 Blue", "dbs", i, bid, "primary", style="primary"), kb.btn("🟢 Green", "dbs", i, bid, "success", style="success"))
    kb.row(kb.btn("🔴 Red", "dbs", i, bid, "danger", style="danger"), kb.btn("⚪ Default", "dbs", i, bid, "none"))
    kb.row(kb.btn("⬅️ Back", "dbe", i, bid))
    ctx.show("<b>🎨 Button color</b>\nChoose a color. Colors need an up-to-date Telegram app; older apps show the normal button.", kb)


@screen("dbs")
@need_draft
def btn_set_color(ctx, d, bid="", style="none"):
    b = buttons.get(d["rows"], bid)
    if b:
        b["s"] = "" if style == "none" else style
        touch(ctx, d)
    btn_edit(ctx, d["_id"], bid)


@screen("dbi")
@need_draft
def btn_icon(ctx, d, bid=""):
    kb = ctx.kb()
    kb.row(kb.btn("🚫 Remove icon", "dbis", d["_id"], bid, style="danger"), kb.btn("⬅️ Back", "dbe", d["_id"], bid))
    ctx.ask("bei", "<b>😀 Premium emoji icon</b>\nSend one message that contains a <b>Premium (custom) emoji</b>. It is shown before the button text.\n\n"
            "<i>Telegram allows this only for bots that own a Fragment username; otherwise the button is sent without the icon automatically.</i>", kb, d=d["_id"], b=bid)


@inputs("bei")
@input_draft
def got_icon(ctx, msg, st, d):
    b = buttons.get(d["rows"], st["b"])
    ids = [e["custom_emoji_id"] for e in (msg.get("entities") or []) if e.get("type") == "custom_emoji"]
    if not ids or not b:
        return ctx.say("⚠️ I did not find a Premium emoji in that message.")
    b["i"] = ids[0]
    touch(ctx, d)
    ctx.forget_input(msg)
    ctx.clear()
    btn_edit(ctx, d["_id"], b["id"])


@screen("dbis")
@need_draft
def btn_icon_off(ctx, d, bid=""):
    b = buttons.get(d["rows"], bid)
    if b:
        b["i"] = ""
        touch(ctx, d)
    btn_edit(ctx, d["_id"], bid)


@screen("dbp")
@need_draft
def btn_paint(ctx, d):
    kb, i = ctx.kb(), d["_id"]
    kb.row(kb.btn("🔵 All blue", "dbpa", i, "primary", style="primary"), kb.btn("🟢 All green", "dbpa", i, "success", style="success"))
    kb.row(kb.btn("🔴 All red", "dbpa", i, "danger", style="danger"), kb.btn("⚪ All default", "dbpa", i, "none"))
    kb.row(kb.btn("⬅️ Back", "db", i))
    ctx.show("<b>🎨 Paint all buttons</b>\nGive every button the same color.", kb)


@screen("dbpa")
@need_draft
def btn_paint_all(ctx, d, style="none"):
    buttons.set_style_all(d["rows"], "" if style == "none" else style)
    touch(ctx, d)
    btn_editor(ctx, d["_id"], "🎨 Done.")


@screen("dbt")
@need_draft
def btn_as_text(ctx, d):
    ctx.say("<b>📄 Buttons as text</b> (copy, change, and paste back with 📥):\n\n<pre>%s</pre>" % esc(buttons.to_quick(d["rows"])))
    ctx.toast("Sent below.")


@screen("dbc")
@need_draft
def btn_clear(ctx, d, sure=""):
    if sure != "1":
        kb = ctx.kb()
        kb.row(kb.btn("🧹 Yes, remove all", "dbc", d["_id"], 1, style="danger"), kb.btn("⬅️ No", "db", d["_id"]))
        return ctx.show("<b>🧹 Remove all %d buttons?</b>" % buttons.count(d["rows"]), kb)
    d["rows"] = []
    touch(ctx, d)
    btn_editor(ctx, d["_id"], "🧹 All buttons removed.")


# ----------------------------------------------------------------------------------------------------------------------
# options and channels of a draft
# ----------------------------------------------------------------------------------------------------------------------
@screen("do")
@need_draft
def draft_options(ctx, d):
    ui.SCREENS["gg"](ctx, "d%d" % d["_id"], "*", "0")


@screen("dc")
@need_draft
def draft_channels(ctx, d, p="0"):
    chans = core.my_channels(ctx.uid)
    page, p, pages = paged(chans, p, ctx.size)
    kb, i = ctx.kb(), d["_id"]
    for c in page:
        on = c["_id"] in d["chans"]
        kb.row(kb.btn(("✅ " if on else "⬜ ") + c["title"][:40] + ("" if c.get("ok", True) else " ⚠️"), "dct", i, c["_id"], p, style="success" if on else ""))
    pager(kb, "dc", [i], p, pages)
    if chans:
        kb.row(kb.btn("✅ Select all", "dca", i, style="primary"), kb.btn("⬜ None", "dcn", i))
    else:
        kb.row(kb.btn("➕ How to add a channel", "ca"))
    kb.row(kb.btn("✔️ Done", "d", i, style="success"))
    ctx.show("<b>📢 Channels for this post</b>\nTap to select where it will be published. <b>%d</b> selected." % len(d["chans"]) +
             ("" if chans else "\n\nYou have no channels yet."), kb)


@screen("dct")
@need_draft
def channel_toggle(ctx, d, cid="0", p="0"):
    c = int(cid)
    d["chans"] = [x for x in d["chans"] if x != c] if c in d["chans"] else d["chans"] + [c]
    touch(ctx, d)
    draft_channels(ctx, d["_id"], p)


@screen("dca")
@need_draft
def channels_all(ctx, d):
    d["chans"] = [c["_id"] for c in core.my_channels(ctx.uid)]
    touch(ctx, d)
    draft_channels(ctx, d["_id"])


@screen("dcn")
@need_draft
def channels_none(ctx, d):
    d["chans"] = []
    touch(ctx, d)
    draft_channels(ctx, d["_id"])


# ----------------------------------------------------------------------------------------------------------------------
# schedule
# ----------------------------------------------------------------------------------------------------------------------
def rep_label(rep):
    for _, lab, r in REPEATS:
        if r == (rep or {}):
            return lab
    if (rep or {}).get("kind") == "interval":
        return "Every %s" % fmt.fmt_dur(rep["secs"])
    return "Once"


@screen("ds")
@need_draft
def schedule(ctx, d, note=""):
    kb, i, tz = ctx.kb(), d["_id"], core.tz_of(ctx.u)
    now = core.now()
    when = d.get("when") or 0
    for k in range(0, len(QUICK_TIMES), 2):
        kb.row(*[kb.btn(lab, "dst", i, code) for code, lab, _ in QUICK_TIMES[k:k + 2]])
    kb.row(kb.btn("🔁 Repeat: " + rep_label(d.get("rep")), "dsr", i), kb.btn("🗑 Auto-delete: " + config.BY_KEY["delete_after"].show(core.eff("delete_after", d, first_channel(d), ctx.u)), "gp", "d%d" % i, "delete_after", 0))
    if when and when > now:
        kb.row(kb.btn("✅ Confirm schedule", "dsok", i, style="success"))
    kb.row(kb.btn("⬅️ Back", "d", i))
    text = "<b>🕒 Schedule</b>\nNow: %s (UTC%s)\n📅 When: %s\n🔁 Repeat: %s" % (
        ctx.fts(now), _tz_text(tz), ("<b>%s</b> (%s)" % (ctx.fts(when), fmt.fmt_rel(when, now))) if when else "<i>not set</i>", rep_label(d.get("rep")))
    text += "\n\nSend a date and time, or tap a quick time.\n<i>Examples: in 2h · tomorrow 9am · fri 8:30pm · 25 nov 6pm · 2026-12-01 18:00</i>"
    if note:
        text += "\n\n" + note
    ctx.u["st"] = {"n": "when", "d": i}
    ctx.show(text, kb)


def _tz_text(tz):
    return "%s%02d:%02d" % ("+" if tz >= 0 else "-", abs(tz) // 60, abs(tz) % 60)


@screen("dst")
@need_draft
def schedule_quick(ctx, d, code=""):
    phrase = next((p for c, _, p in QUICK_TIMES if c == code), None)
    ts = fmt.parse_when(phrase or "", core.now(), core.tz_of(ctx.u))
    if ts:
        d["when"] = ts
        core.save_draft(d)
    schedule(ctx, d["_id"])


@inputs("when")
@input_draft
def got_when(ctx, msg, st, d):
    ts = fmt.parse_when(msg.get("text") or "", core.now(), core.tz_of(ctx.u))
    if not ts:
        return ctx.say("⚠️ I could not understand that time. Try: <code>in 2h</code>, <code>tomorrow 9am</code>, <code>25 nov 6pm</code>.")
    if ts <= core.now():
        return ctx.say("⚠️ That time is in the past.")
    d["when"] = ts
    core.save_draft(d)
    ctx.forget_input(msg)
    schedule(ctx, d["_id"])


@screen("dsr")
@need_draft
def schedule_repeat(ctx, d):
    kb, i = ctx.kb(), d["_id"]
    for k in range(0, len(REPEATS), 2):
        kb.row(*[kb.btn(("✅ " if r == (d.get("rep") or {}) else "") + lab, "dsrs", i, code, style="primary" if r == (d.get("rep") or {}) else "") for code, lab, r in REPEATS[k:k + 2]])
    kb.row(kb.btn("⏱ Custom interval", "dsrc", i), kb.btn("⬅️ Back", "ds", i))
    ctx.show("<b>🔁 Repeat</b>\nHow often should this post go out? The first time is the date you set.", kb)


@screen("dsrs")
@need_draft
def schedule_repeat_set(ctx, d, code="0"):
    rep = next((dict(r) for c, _, r in REPEATS if c == code), {})
    if rep.get("kind") == "monthly" and d.get("when"):
        rep["day"] = fmt.local_dt(d["when"], core.tz_of(ctx.u)).day
    d["rep"] = rep
    core.save_draft(d)
    schedule(ctx, d["_id"])


@screen("dsrc")
@need_draft
def schedule_repeat_custom(ctx, d):
    kb = ctx.kb()
    kb.row(kb.btn("⬅️ Back", "dsr", d["_id"]))
    ctx.ask("rep", "<b>⏱ Custom interval</b>\nSend how often, e.g. <code>90m</code>, <code>4h</code>, <code>2d</code>, <code>1w</code>. (At least 5 minutes.)", kb, d=d["_id"])


@inputs("rep")
@input_draft
def got_rep(ctx, msg, st, d):
    secs = fmt.parse_duration(msg.get("text") or "")
    if not secs or secs < 300:
        return ctx.say("⚠️ Send something like <code>90m</code>, <code>4h</code> or <code>2d</code> (at least 5 minutes).")
    d["rep"] = {"kind": "interval", "secs": secs}
    core.save_draft(d)
    ctx.forget_input(msg)
    ctx.clear()
    schedule(ctx, d["_id"])


@screen("dsok")
@need_draft
def schedule_confirm(ctx, d):
    if not d.get("chans"):
        ctx.toast("Choose at least one channel first.", True)
        return draft_channels(ctx, d["_id"])
    if not d.get("when") or d["when"] <= core.now():
        ctx.toast("Set a time in the future first.", True)
        return schedule(ctx, d["_id"])
    try:
        core.compose(d, first_channel(d), ctx.u)
    except core.ComposeError as e:
        return ctx.toast(str(e), True)
    s = core.make_sched(ctx.u, d, d["chans"], d["when"], d.get("rep"), d.get("sched_of"))
    ctx.u["last"] = d["chans"]
    if not ctx.cfg("keep_draft") or d.get("sched_of"):
        ctx.drop_preview()
        core.db.delete("drafts", d["_id"])
    names = ", ".join(esc(c["title"]) for c in map(core.get_channel, s["chans"]) if c)
    kb = ctx.kb()
    kb.row(kb.btn("🗓 Scheduled posts", "sl", 0, style="primary"), kb.btn("✍️ New post", "nw", style="success"))
    kb.row(kb.btn("🏠 Menu", "m"))
    ctx.clear()
    ctx.show("✅ <b>Scheduled</b>\n🕒 %s (%s)\n📢 %s%s" % (ctx.fts(s["when"]), fmt.fmt_rel(s["when"], core.now()), names or "—",
             ("\n🔁 " + rep_label(s["rep"])) if s.get("rep") else "") + "\n\n<i>Render's free plan sleeps without traffic. Keep the bot awake with a free uptime monitor so scheduled posts go out on time.</i>", kb, fresh=True)


# ----------------------------------------------------------------------------------------------------------------------
# publishing
# ----------------------------------------------------------------------------------------------------------------------
@screen("dp")
@need_draft
def publish_ask(ctx, d):
    if not d.get("chans"):
        ctx.toast("Choose at least one channel first.", True)
        return draft_channels(ctx, d["_id"])
    try:
        core.compose(d, first_channel(d), ctx.u)
    except core.ComposeError as e:
        return ctx.toast(str(e), True)
    if not ctx.cfg("confirm_publish"):
        return do_publish(ctx, d)
    names = ", ".join(esc(c["title"]) for c in map(core.get_channel, d["chans"]) if c)
    kb = ctx.kb()
    kb.row(kb.btn("✅ Yes, publish now", "dpp", d["_id"], style="success"))
    kb.row(kb.btn("🕒 Schedule instead", "ds", d["_id"], style="primary"), kb.btn("⬅️ Back", "d", d["_id"]))
    ctx.show("<b>🚀 Publish now?</b>\n📢 %s\n%s" % (names, "\n".join("⚠️ " + esc(w) for w in (ctx.u.get("prev") or {}).get("warn", []))), kb)


@screen("dpp")
@need_draft
def publish_go(ctx, d):
    do_publish(ctx, d)


def do_publish(ctx, d):
    ctx.toast("Publishing…")
    ctx.show("⏳ <b>Publishing to %d channel%s…</b>" % (len(d["chans"]), "" if len(d["chans"]) == 1 else "s"))
    results = core.publish(d, ctx.u, d["chans"])
    ok = [r for r in results if r["ok"]]
    kb = ctx.kb()
    links = [kb.btn("🔗 " + r["title"][:28], url=r["link"]) for r in ok if r.get("link")]
    for k in range(0, len(links), 2):
        kb.row(*links[k:k + 2])
    if len(ok) == 1:
        kb.row(kb.btn("✏️ Edit", "pe", ok[0]["post"]), kb.btn("🗑 Delete", "pd", ok[0]["post"], style="danger"))
    if ok:
        ctx.u["last"] = d["chans"]
        if not ctx.cfg("keep_draft"):
            ctx.drop_preview()
            core.db.delete("drafts", d["_id"])
    else:
        kb.row(kb.btn("✏️ Back to the draft", "d", d["_id"], 1, style="primary"))
    kb.row(kb.btn("✍️ New post", "nw", style="success"), kb.btn("📚 My posts", "pl", 0))
    kb.row(kb.btn("🏠 Menu", "m"))
    head = "✅ <b>Published</b>" if ok and len(ok) == len(results) else ("⚠️ <b>Published to %d of %d</b>" % (len(ok), len(results)) if ok else "❌ <b>Nothing was published</b>")
    ctx.clear()
    ctx.show(head + "\n\n" + core.report_lines(results), kb, fresh=True)


@screen("de")
@need_draft
def apply_edit(ctx, d):
    post = core.db.get("posts", d.get("edit_of"))
    if not post or post["status"] != "live":
        return ctx.toast("That post no longer exists.", True)
    try:
        core.apply_edit(d, ctx.u, post)
    except core.ComposeError as e:
        return ctx.toast(str(e), True)
    except TgError as e:
        return ctx.toast(e.friendly(), True)
    ctx.drop_preview()
    core.db.delete("drafts", d["_id"])
    kb = ctx.kb()
    if post.get("link"):
        kb.row(kb.btn("🔗 Open post", url=post["link"]))
    kb.row(kb.btn("📚 My posts", "pl", 0, style="primary"), kb.btn("🏠 Menu", "m"))
    ctx.clear()
    ctx.show("✅ <b>Post updated</b> in %s." % esc(post["title"]), kb, fresh=True)


@screen("dx")
@need_draft
def discard_ask(ctx, d, sure=""):
    if sure != "1":
        kb = ctx.kb()
        kb.row(kb.btn("🗑 Yes, discard", "dx", d["_id"], 1, style="danger"), kb.btn("⬅️ No", "d", d["_id"]))
        return ctx.show("<b>🗑 %s?</b>" % ("Cancel editing" if d.get("edit_of") or d.get("sched_of") else "Discard this draft"), kb)
    ctx.drop_preview()
    core.db.delete("drafts", d["_id"])
    ctx.clear()
    menu(ctx, fresh=True)


@screen("dz")
@need_draft
def template_ask(ctx, d):
    kb = ctx.kb()
    kb.row(kb.btn("⬅️ Back", "d", d["_id"]))
    ctx.ask("tname", "<b>📋 Save as template</b>\nSend a name for the template. It keeps the text, media, buttons and options (not the channels).", kb, d=d["_id"])


@inputs("tname")
@input_draft
def got_tname(ctx, msg, st, d):
    name = (msg.get("text") or "").strip()[:40]
    if not name:
        return ctx.say("⚠️ Please send a name.")
    core.db.put("templates", {"_id": core.db.incr("counters", "tpl", "v"), "uid": ctx.uid, "name": name, "d": core.snap(d), "at": core.now()})
    ctx.forget_input(msg)
    ctx.clear()
    draft_screen(ctx, d, note="✅ Template <b>%s</b> saved." % esc(name))
