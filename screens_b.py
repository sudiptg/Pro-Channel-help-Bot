"""Screens, part 2: published posts, scheduled posts, templates, channels, settings (user / channel / post), help, admin."""
import json
import logging
import re

import buttons
import config
import core
import fmt
from screens_a import KIND_ICON, KIND_NAME, default_chans, draft_screen, first_channel, menu, rep_label
from tgapi import TgError
from ui import esc, inputs, paged, pager, plain_snip, screen, snip

log = logging.getLogger("screens")


# ----------------------------------------------------------------------------------------------------------------------
# published posts
# ----------------------------------------------------------------------------------------------------------------------
def get_post(ctx, pid):
    p = core.db.get("posts", pid)
    return p if p and p["uid"] == ctx.uid else None


@screen("pl")
def posts_list(ctx, p="0"):
    rows = core.user_posts(ctx.uid)
    page, p, pages = paged(rows, p, ctx.size)
    kb = ctx.kb()
    for post in page:
        kb.row(kb.btn("%s %s · %s · %s" % (KIND_ICON.get(post["kind"], "📝"), post["title"][:14], fmt.fmt_date(post["at"], core.tz_of(ctx.u), "dmy")[:6], plain_snip(post["text"], 16)), "po", post["_id"]))
    pager(kb, "pl", [], p, pages)
    kb.row(kb.btn("✍️ New post", "nw", style="success"), kb.btn("🏠 Menu", "m"))
    ctx.show("<b>📚 My posts</b>\n" + ("Tap a post to edit, pin, copy or delete it." if rows else "Nothing published yet."), kb)


@screen("po")
def post_view(ctx, pid="", note=""):
    post = get_post(ctx, pid)
    if not post or post["status"] != "live":
        ctx.toast("That post is not available any more.", True)
        return posts_list(ctx)
    kb = ctx.kb()
    lines = ["<b>%s Post #%s</b> in <b>%s</b>" % (KIND_ICON.get(post["kind"], "📝"), post.get("n", "?"), esc(post["title"])),
             "🕒 " + ctx.fts(post["at"]), "💬 " + snip(post["text"], 80)]
    if post.get("pinned"):
        lines.append("📌 Pinned")
    if post.get("del"):
        lines.append("🗑 Auto-delete %s" % fmt.fmt_rel(post["del"], core.now()))
    live = core.db.get("live", "%d:%d" % (post["chat"], post["bmid"]))
    if live:
        votes = ["%s %d" % (esc(b["t"]), live["counts"].get(b["id"], 0)) for b in buttons.flat(live["rows"]) if b["k"] == "vote"]
        if votes:
            lines.append("📊 " + " · ".join(votes) + " · %d voter%s" % (len(live["users"]), "" if len(live["users"]) == 1 else "s"))
    if note:
        lines.append("\n" + note)
    kb.row(kb.btn("✏️ Edit", "pe", pid, style="primary"), kb.btn("📋 Duplicate", "pc", pid))
    kb.row(kb.btn("📌 Unpin" if post.get("pinned") else "📌 Pin", "pp", pid), kb.btn("🔗 Open", url=post["link"]) if post.get("link") else None)
    kb.row(kb.btn("🗑 Delete", "pd", pid, style="danger"), kb.btn("⬅️ Back", "pl", 0))
    ctx.show("\n".join(lines), kb)


@screen("pe")
def post_edit(ctx, pid=""):
    post = get_post(ctx, pid)
    if not post or post["status"] != "live":
        return ctx.toast("That post is not available any more.", True)
    for x in core.user_drafts(ctx.uid):
        if x.get("edit_of") == pid:
            core.db.delete("drafts", x["_id"])
    d = core.draft_from(ctx.uid, post["d"], edit_of=pid, chans=[post["chat"]])
    draft_screen(ctx, d, force=True)


@screen("pc")
def post_copy(ctx, pid=""):
    post = get_post(ctx, pid)
    if not post:
        return ctx.toast("That post is not available any more.", True)
    d = core.draft_from(ctx.uid, post["d"], chans=default_chans(ctx))
    draft_screen(ctx, d, force=True)


@screen("pp")
def post_pin(ctx, pid=""):
    post = get_post(ctx, pid)
    if not post:
        return ctx.toast("That post is not available any more.", True)
    err = core.pin_post(post, not post.get("pinned"))
    post_view(ctx, pid, ("⚠️ " + esc(err)) if err else "")


@screen("pd")
def post_delete(ctx, pid="", sure=""):
    post = get_post(ctx, pid)
    if not post:
        return ctx.toast("That post is not available any more.", True)
    if sure != "1":
        kb = ctx.kb()
        kb.row(kb.btn("🗑 Yes, delete from the channel", "pd", pid, 1, style="danger"), kb.btn("⬅️ No", "po", pid))
        return ctx.show("<b>🗑 Delete this post from %s?</b>\nIt disappears for all subscribers." % esc(post["title"]), kb)
    err = core.delete_post(post)
    if err:
        return post_view(ctx, pid, "⚠️ " + esc(err))
    ctx.toast("Deleted.")
    posts_list(ctx)


# ----------------------------------------------------------------------------------------------------------------------
# scheduled posts
# ----------------------------------------------------------------------------------------------------------------------
def get_sched(ctx, sid):
    s = core.db.get("sched", sid)
    return s if s and s["uid"] == ctx.uid else None


@screen("sl")
def sched_list(ctx, p="0"):
    rows = core.db.find("sched", {"uid": ctx.uid}, sort=[("when", 1)])
    page, p, pages = paged(rows, p, ctx.size)
    kb = ctx.kb()
    for s in page:
        kb.row(kb.btn("%s%s %s · %s" % ("⏸ " if s["status"] == "paused" else "", "🔁" if s.get("rep") else "🕒", fmt.fmt_ts(s["when"], core.tz_of(ctx.u), ctx.cfg("time12"), ctx.cfg("date_fmt"))[:17],
                                          plain_snip(s["d"].get("text"), 14)), "so", s["_id"]))
    pager(kb, "sl", [], p, pages)
    kb.row(kb.btn("✍️ New post", "nw", style="success"), kb.btn("🏠 Menu", "m"))
    ctx.show("<b>🗓 Scheduled posts</b>\n" + ("Tap one to change the time, edit or cancel it." if rows else "Nothing scheduled. Make a post and tap 🕒 Schedule."), kb)


@screen("so")
def sched_view(ctx, sid="", note=""):
    s = get_sched(ctx, sid)
    if not s:
        ctx.toast("That scheduled post no longer exists.", True)
        return sched_list(ctx)
    names = ", ".join(esc(c["title"]) for c in map(core.get_channel, s["chans"]) if c)
    kb = ctx.kb()
    text = "<b>🗓 Scheduled%s</b>\n🕒 %s (%s)\n📢 %s\n🔁 %s\n💬 %s\n▶️ Sent %d time%s" % (
        " · ⏸ paused" if s["status"] == "paused" else "", ctx.fts(s["when"]), fmt.fmt_rel(s["when"], core.now()), names or "—", rep_label(s.get("rep")),
        snip(s["d"].get("text"), 80), s.get("runs", 0), "" if s.get("runs", 0) == 1 else "s")
    if note:
        text += "\n\n" + note
    kb.row(kb.btn("▶️ Post now", "sn", sid, style="success"), kb.btn("✏️ Edit", "se", sid, style="primary"))
    kb.row(kb.btn("▶️ Resume" if s["status"] == "paused" else "⏸ Pause", "sp", sid), kb.btn("🗑 Cancel", "sx", sid, style="danger"))
    kb.row(kb.btn("⬅️ Back", "sl", 0))
    ctx.show(text, kb)


@screen("sn")
def sched_now(ctx, sid=""):
    s = get_sched(ctx, sid)
    if not s:
        return ctx.toast("That scheduled post no longer exists.", True)
    results = core.publish(s["d"], ctx.u, s["chans"])
    if any(r["ok"] for r in results) and not s.get("rep"):
        core.db.delete("sched", sid)
    ctx.toast("Done.")
    kb = ctx.kb()
    kb.row(kb.btn("🗓 Scheduled", "sl", 0), kb.btn("🏠 Menu", "m"))
    ctx.show("▶️ <b>Posted now</b>\n\n" + core.report_lines(results), kb)


@screen("se")
def sched_edit(ctx, sid=""):
    s = get_sched(ctx, sid)
    if not s:
        return ctx.toast("That scheduled post no longer exists.", True)
    for x in core.user_drafts(ctx.uid):
        if x.get("sched_of") == sid:
            core.db.delete("drafts", x["_id"])
    d = core.draft_from(ctx.uid, s["d"], chans=s["chans"], when=s["when"], rep=s.get("rep") or {}, sched_of=sid)
    draft_screen(ctx, d, force=True)


@screen("sp")
def sched_pause(ctx, sid=""):
    s = get_sched(ctx, sid)
    if s:
        s["status"] = "wait" if s["status"] == "paused" else "paused"
        if s["status"] == "wait":
            s["fails"] = 0
            if s["when"] <= core.now():
                s["when"] = core.now() + 60
        core.db.put("sched", s)
    sched_view(ctx, sid)


@screen("sx")
def sched_cancel(ctx, sid="", sure=""):
    s = get_sched(ctx, sid)
    if not s:
        return sched_list(ctx)
    if sure != "1":
        kb = ctx.kb()
        kb.row(kb.btn("🗑 Yes, cancel it", "sx", sid, 1, style="danger"), kb.btn("⬅️ No", "so", sid))
        return ctx.show("<b>🗑 Cancel this scheduled post?</b>", kb)
    core.db.delete("sched", sid)
    ctx.toast("Cancelled.")
    sched_list(ctx)


# ----------------------------------------------------------------------------------------------------------------------
# templates
# ----------------------------------------------------------------------------------------------------------------------
def get_tpl(ctx, tid):
    try:
        t = core.db.get("templates", int(tid))
    except (TypeError, ValueError):
        return None
    return t if t and t["uid"] == ctx.uid else None


@screen("tl")
def tpl_list(ctx, p="0"):
    rows = sorted(core.db.find("templates", {"uid": ctx.uid}), key=lambda t: t.get("name", "").lower())
    page, p, pages = paged(rows, p, ctx.size)
    kb = ctx.kb()
    for t in page:
        kb.row(kb.btn("%s %s" % (KIND_ICON.get(t["d"].get("kind"), "📝"), t["name"][:40]), "to", t["_id"]))
    pager(kb, "tl", [], p, pages)
    kb.row(kb.btn("🏠 Menu", "m"))
    ctx.show("<b>📋 Templates</b>\n" + ("Tap a template to use it." if rows else "No templates yet. Open a draft and tap 📋 Save template."), kb)


@screen("to")
def tpl_view(ctx, tid=""):
    t = get_tpl(ctx, tid)
    if not t:
        return tpl_list(ctx)
    kb = ctx.kb()
    kb.row(kb.btn("🟢 Use template", "tu", tid, style="success"), kb.btn("✏️ Rename", "tr", tid))
    kb.row(kb.btn("🗑 Delete", "tx", tid, style="danger"), kb.btn("⬅️ Back", "tl", 0))
    ctx.show("<b>📋 %s</b>\n%s %s · 🔘 %d button%s\n💬 %s" % (esc(t["name"]), KIND_ICON.get(t["d"].get("kind"), "📝"), KIND_NAME.get(t["d"].get("kind"), ""),
             buttons.count(t["d"].get("rows") or []), "" if buttons.count(t["d"].get("rows") or []) == 1 else "s", snip(t["d"].get("text"), 100)), kb)


@screen("tu")
def tpl_use(ctx, tid=""):
    t = get_tpl(ctx, tid)
    if not t:
        return tpl_list(ctx)
    draft_screen(ctx, core.draft_from(ctx.uid, t["d"], chans=default_chans(ctx)), force=True)


@screen("tr")
def tpl_rename(ctx, tid=""):
    kb = ctx.kb()
    kb.row(kb.btn("⬅️ Back", "to", tid))
    ctx.ask("trename", "<b>✏️ Rename template</b>\nSend the new name.", kb, t=tid)


@inputs("trename")
def got_trename(ctx, msg, st):
    t = get_tpl(ctx, st.get("t"))
    name = (msg.get("text") or "").strip()[:40]
    if not t or not name:
        return ctx.say("⚠️ Please send a name.")
    t["name"] = name
    core.db.put("templates", t)
    ctx.forget_input(msg)
    ctx.clear()
    tpl_view(ctx, t["_id"])


@screen("tx")
def tpl_delete(ctx, tid="", sure=""):
    t = get_tpl(ctx, tid)
    if not t:
        return tpl_list(ctx)
    if sure != "1":
        kb = ctx.kb()
        kb.row(kb.btn("🗑 Yes, delete", "tx", tid, 1, style="danger"), kb.btn("⬅️ No", "to", tid))
        return ctx.show("<b>🗑 Delete template “%s”?</b>" % esc(t["name"]), kb)
    core.db.delete("templates", t["_id"])
    tpl_list(ctx)


# ----------------------------------------------------------------------------------------------------------------------
# channels
# ----------------------------------------------------------------------------------------------------------------------
def get_chan(ctx, cid):
    ch = core.get_channel(cid)
    return ch if ch and ctx.uid in ch.get("owners", []) else None


@screen("cl")
def chan_list(ctx, p="0"):
    rows = core.my_channels(ctx.uid)
    page, p, pages = paged(rows, p, ctx.size)
    kb = ctx.kb()
    for c in page:
        kb.row(kb.btn(("✅ " if c.get("ok", True) else "⚠️ ") + c["title"][:40], "co", c["_id"], style="" if c.get("ok", True) else "danger"))
    pager(kb, "cl", [], p, pages)
    kb.row(kb.btn("➕ Add channel", "ca", style="success"), kb.btn("🏠 Menu", "m"))
    ctx.show("<b>📢 Channels</b>\n" + ("Tap a channel for its settings." if rows else "No channels yet.\nAdd the bot as an <b>admin</b> of your channel (it needs to post, edit and delete messages). The channel appears here by itself.") +
             ("\n\n⚠️ = the bot is not an admin there any more." if any(not c.get("ok", True) for c in rows) else ""), kb)


@screen("co")
def chan_view(ctx, cid="", note=""):
    ch = get_chan(ctx, cid)
    if not ch:
        ctx.toast("That channel is not in your list.", True)
        return chan_list(ctx)
    can = ch.get("can") or {}
    mark = lambda k: "✅" if can.get(k) else "❌"
    text = "<b>📢 %s</b>%s\nStatus: %s\nBot rights: %s Post  %s Edit  %s Delete  %s Pin\n📨 Posts sent: <b>%d</b>  ·  ⚙️ Own settings: <b>%d</b>" % (
        esc(ch["title"]), (" · @" + esc(ch["un"])) if ch.get("un") else "", "✅ ready" if ch.get("ok", True) else "⚠️ " + esc(ch.get("err") or "the bot is not an admin"),
        mark("post"), mark("edit"), mark("delete"), mark("pin"), ch.get("n", 0), len([v for v in (ch.get("cfg") or {}).values() if v is not None]))
    if note:
        text += "\n\n" + note
    kb = ctx.kb()
    kb.row(kb.btn("📝 New post here", "cn", cid, style="success"), kb.btn("⚙️ Settings", "gm", "c%s" % cid, style="primary"))
    kb.row(kb.btn("🔄 Check rights", "cr", cid), kb.btn("🧪 Send test", "ct", cid))
    kb.row(kb.btn("🗑 Remove", "cx", cid, style="danger"), kb.btn("⬅️ Back", "cl", 0))
    ctx.show(text, kb)


@screen("cn")
def chan_new_post(ctx, cid=""):
    ch = get_chan(ctx, cid)
    if not ch:
        return ctx.toast("That channel is not in your list.", True)
    kb = ctx.kb()
    kb.row(kb.btn("⬅️ Cancel", "co", cid, style="danger"))
    ctx.ask("new", "<b>✍️ New post for %s</b>\nSend the post now: text, photo, video, file, audio, voice or an album." % esc(ch["title"]), kb, chat=int(cid))


@screen("cr")
def chan_refresh(ctx, cid=""):
    ch = get_chan(ctx, cid)
    if not ch:
        return ctx.toast("That channel is not in your list.", True)
    core.refresh_channel(ch, core.tg.me.get("id"))
    chan_view(ctx, cid, "🔄 Checked just now.")


@screen("ct")
def chan_test(ctx, cid=""):
    ch = get_chan(ctx, cid)
    if not ch:
        return ctx.toast("That channel is not in your list.", True)
    try:
        m = core.tg("sendMessage", chat_id=ch["_id"], text="✅ %s test message (deleted now)." % esc(config.Config.APP_NAME), parse_mode="HTML")
        core.tg("deleteMessage", chat_id=ch["_id"], message_id=m["message_id"])
        note = "🧪 <b>Test passed:</b> the bot can post and delete here."
    except TgError as e:
        note = "🧪 <b>Test failed:</b> " + esc(e.friendly())
    chan_view(ctx, cid, note)


@screen("cx")
def chan_remove(ctx, cid="", sure=""):
    ch = get_chan(ctx, cid)
    if not ch:
        return chan_list(ctx)
    if sure != "1":
        kb = ctx.kb()
        kb.row(kb.btn("🗑 Yes, remove", "cx", cid, 1, style="danger"), kb.btn("⬅️ No", "co", cid))
        return ctx.show("<b>🗑 Remove %s from your list?</b>\nPosts already in the channel stay. Scheduled posts for it will fail." % esc(ch["title"]), kb)
    ch["owners"] = [o for o in ch["owners"] if o != ctx.uid]
    if ch["owners"]:
        core.save_channel(ch)
    else:
        core.db.delete("channels", ch["_id"])
    chan_list(ctx)


@screen("ca")
def chan_add(ctx):
    kb = ctx.kb()
    kb.row(kb.btn("⬅️ Back", "cl", 0))
    me = core.tg.me.get("username", "")
    ctx.ask("addch", "<b>➕ Add a channel</b>\n<b>1.</b> Open your channel → Administrators → Add administrator → search <b>@%s</b>.\n"
            "<b>2.</b> Allow: <i>Post messages, Edit messages, Delete messages</i>.\n\nThe channel then appears here automatically.\n\n"
            "Or send me the channel <code>@username</code> / ID, or forward any post from it, and I will check it." % esc(me), kb)


def chat_ref(msg):
    fo = msg.get("forward_origin") or {}
    if fo.get("type") == "channel":
        return fo["chat"]["id"]
    if msg.get("forward_from_chat"):
        return msg["forward_from_chat"]["id"]
    t = (msg.get("text") or "").strip()
    m = re.search(r"(?:t\.me/|@)([A-Za-z][A-Za-z0-9_]{3,})", t)
    if re.fullmatch(r"-?\d{6,}", t):
        return int(t)
    return ("@" + m.group(1)) if m else None


@inputs("addch")
def got_channel(ctx, msg, st):
    ref = chat_ref(msg)
    if ref is None:
        return ctx.say("⚠️ Send the channel @username, its ID, or forward a post from it.")
    try:
        chat = core.tg("getChat", chat_id=ref)
        me = core.tg("getChatMember", chat_id=chat["id"], user_id=core.tg.me["id"])
    except TgError:
        return ctx.say("⚠️ I cannot see that channel. Add me as an admin first, then try again.")
    if me.get("status") not in ("administrator", "creator"):
        return ctx.say("⚠️ I am not an admin of <b>%s</b> yet. Add me as an admin, then try again." % esc(chat.get("title", "")))
    if not core.is_owner(ctx.uid):
        try:
            who = core.tg("getChatMember", chat_id=chat["id"], user_id=ctx.uid)
        except TgError:
            who = {}
        if who.get("status") not in ("administrator", "creator"):
            return ctx.say("⚠️ You are not an admin of that channel.")
    ch = core.upsert_channel(chat, [ctx.uid], core.rights_from_member(me, chat.get("type", "channel")))
    ctx.forget_input(msg)
    ctx.clear()
    chan_view(ctx, ch["_id"], "✅ Channel added.")


# ----------------------------------------------------------------------------------------------------------------------
# settings (three levels: u = my defaults, c<chat> = one channel, d<id> = one post)
# ----------------------------------------------------------------------------------------------------------------------
class Level:
    def __init__(self, ctx, lvl):
        self.ctx, self.lvl, self.kind = ctx, lvl, lvl[:1]
        self.obj = self.layer = None
        if self.kind == "u":
            self.obj, self.layer = ctx.u, ctx.u.setdefault("cfg", {})
            self.chain, self.inherit = [self.layer], False
            self.title, self.sub, self.back = "My defaults", "Applies to all your channels unless a channel or a single post says otherwise.", ("m",)
            self.keys = [s.key for s in config.SETTINGS]
        elif self.kind == "c":
            ch = core.get_channel(lvl[1:]) if lvl[1:].lstrip("-").isdigit() else None
            if ch and ctx.uid in ch.get("owners", []):
                self.obj, self.layer = ch, ch.setdefault("cfg", {})
            self.chain, self.inherit = [self.layer, ctx.u.get("cfg")], True
            self.title = esc(ch["title"]) if ch else "?"
            self.sub, self.back = "Only for this channel. It overrides your defaults.", ("co", lvl[1:])
            self.keys = [s.key for s in config.SETTINGS if s.scope == "all"]
        else:
            d = core.get_draft(lvl[1:])
            if d and d["uid"] == ctx.uid:
                self.obj, self.layer = d, d.setdefault("opts", {})
            self.chain, self.inherit = [self.layer, (first_channel(d) or {}).get("cfg") if d else None, ctx.u.get("cfg")], True
            self.title, self.sub, self.back = "Options for this post", "Only for this post. It overrides channel and default settings.", ("d", lvl[1:])
            self.keys = config.OPTION_KEYS

    def ok(self):
        return self.obj is not None

    def get(self, key):
        return config.effective(key, *self.chain)

    def own(self, key):
        return key in self.layer and self.layer[key] is not None

    def save(self):
        if self.kind == "u":
            core.save_user(self.ctx.u)
        elif self.kind == "c":
            core.save_channel(self.obj)
        else:
            core.save_draft(self.obj)
            self.ctx.stale()


def level(ctx, lvl):
    lv = Level(ctx, lvl)
    return lv if lv.ok() else None


def _bad(ctx):
    ctx.toast("That is not available any more.", True)
    return menu(ctx)


@screen("gm")
def settings_menu(ctx, lvl="u"):
    lv = level(ctx, lvl)
    if not lv:
        return _bad(ctx)
    if lv.kind == "d":
        return settings_list(ctx, lvl, "*", "0")
    kb = ctx.kb()
    for gid, icon, name in config.GROUPS:
        n = len([k for k in lv.keys if config.BY_KEY[k].group == gid])
        if n:
            kb.row(kb.btn("%s %s (%d)" % (icon, name, n), "gg", lvl, gid, 0))
    if lv.inherit:
        kb.row(kb.btn("↺ Reset all to default", "gr", lvl, style="danger"))
    kb.row(kb.btn("⬅️ Back", *lv.back))
    extra = ""
    if lv.kind == "u":
        extra = "\n\nTip: every channel has its own ⚙️ Settings (open the channel in 📢 Channels)."
    ctx.show("<b>⚙️ %s</b>\n%s%s\n\n<b>%d</b> settings here." % (lv.title, lv.sub, extra, len(lv.keys)), kb)


def _value_text(lv, s):
    v = s.show(lv.get(s.key))
    return v if (not lv.inherit or lv.own(s.key)) else "(%s)" % v


@screen("gg")
def settings_list(ctx, lvl="u", group="*", p="0"):
    lv = level(ctx, lvl)
    if not lv:
        return _bad(ctx)
    keys = [k for k in lv.keys if group == "*" or config.BY_KEY[k].group == group]
    page, p, pages = paged(keys, p, max(ctx.size, 6))
    kb = ctx.kb()
    for k in page:
        s = config.BY_KEY[k]
        val = lv.get(k)
        style = "success" if s.kind == "bool" and val else ""
        if s.kind == "bool" or (s.kind == "choice" and len(s.choices) <= 5):
            main = kb.btn("%s: %s" % (s.label, _value_text(lv, s)), "gv", lvl, k, group, p, style=style)
        elif s.kind == "choice":
            main = kb.btn("%s: %s" % (s.label, _value_text(lv, s)), "gp", lvl, k, 0)
        else:
            main = kb.btn("%s: %s" % (s.label, _value_text(lv, s)), "ga", lvl, k, group, p)
        kb.row(main, kb.btn("↺", "gc", lvl, k, group, p) if lv.inherit and lv.own(k) else None)
    pager(kb, "gg", [lvl, group], p, pages)
    kb.row(kb.btn("⬅️ Back", *(lv.back if group == "*" or lv.kind == "d" else ("gm", lvl))))
    gname = next((n for g, i, n in config.GROUPS if g == group), "Options")
    icon = next((i for g, i, n in config.GROUPS if g == group), "⚙️")
    legend = "\n<i>(value) = taken from the level above · ↺ = go back to it</i>" if lv.inherit else ""
    ctx.show("<b>%s %s</b> · %s\n%s%s\nTap a setting to change it." % (icon, esc(gname), lv.title, lv.sub, legend), kb)


@screen("gv")
def settings_toggle(ctx, lvl="u", key="", group="*", p="0"):
    lv, s = level(ctx, lvl), config.BY_KEY.get(key)
    if not lv or not s:
        return _bad(ctx)
    cur = lv.get(key)
    if s.kind == "bool":
        new = not cur
    else:
        vals = [v for v, _ in s.choices]
        new = vals[(vals.index(cur) + 1) % len(vals)] if cur in vals else vals[0]
    lv.layer[key] = new
    lv.save()
    ctx.toast(("%s: %s. %s" % (s.label, s.show(new), s.hint))[:200])
    settings_list(ctx, lvl, group, p)


@screen("gp")
def settings_pick(ctx, lvl="u", key="", p="0"):
    lv, s = level(ctx, lvl), config.BY_KEY.get(key)
    if not lv or not s:
        return _bad(ctx)
    page, p, pages = paged(list(enumerate(s.choices)), p, 8)
    cur, kb = lv.get(key), ctx.kb()
    for i, (v, lab) in page:
        kb.row(kb.btn(("✅ " if v == cur else "") + lab, "gs", lvl, key, i, p, style="success" if v == cur else ""))
    pager(kb, "gp", [lvl, key], p, pages)
    if lv.inherit and lv.own(key):
        kb.row(kb.btn("↺ Use the level above", "gc", lvl, key, "*", 0, "pick"))
    group = s.group if lv.kind != "d" else "*"
    siblings = [k for k in lv.keys if group == "*" or config.BY_KEY[k].group == group]
    kb.row(kb.btn("⬅️ Back", "gg", lvl, group, siblings.index(key) // max(ctx.size, 6) if key in siblings else 0))
    ctx.show("<b>%s</b>\n%s\nNow: <b>%s</b>" % (esc(s.label), esc(s.hint), esc(s.show(cur))), kb)


@screen("gs")
def settings_set(ctx, lvl="u", key="", i="0", p="0"):
    lv, s = level(ctx, lvl), config.BY_KEY.get(key)
    if not lv or not s:
        return _bad(ctx)
    try:
        lv.layer[key] = s.choices[int(i)][0]
    except (IndexError, ValueError):
        return _bad(ctx)
    lv.save()
    ctx.toast("%s: %s" % (s.label, s.show(lv.layer[key])))
    settings_pick(ctx, lvl, key, p)


@screen("gc")
def settings_clear(ctx, lvl="u", key="", group="*", p="0", back=""):
    lv = level(ctx, lvl)
    if not lv or key not in config.BY_KEY:
        return _bad(ctx)
    lv.layer.pop(key, None)
    lv.save()
    ctx.toast("Back to the level above.")
    if back == "pick":
        return settings_pick(ctx, lvl, key, 0)
    settings_list(ctx, lvl, group, p)


@screen("gr")
def settings_reset(ctx, lvl="u"):
    lv = level(ctx, lvl)
    if not lv or not lv.inherit:
        return _bad(ctx)
    lv.layer.clear()
    lv.save()
    ctx.toast("All overrides removed.")
    settings_menu(ctx, lvl)


@screen("ga")
def settings_ask(ctx, lvl="u", key="", group="*", p="0"):
    lv, s = level(ctx, lvl), config.BY_KEY.get(key)
    if not lv or not s:
        return _bad(ctx)
    kb = ctx.kb()
    kb.row(kb.btn("🧹 Make it empty", "gz", lvl, key, group, p, style="danger"), kb.btn("↺ Level above", "gc", lvl, key, group, p) if lv.inherit and lv.own(key) else None)
    kb.row(kb.btn("⬅️ Back", "gg", lvl, group, p))
    cur = lv.get(key)
    extra = ("\n\n<b>Quick format</b>, one line = one row:\n<code>Join - https://t.me/yourchannel {green}</code>" if key == "def_buttons" else
             "\n\nHTML tags and {variables} work here." if key in ("header", "footer") else "")
    ctx.ask("setv", "<b>%s</b>\n%s%s\n\nNow: %s\nSend the new value." % (esc(s.label), esc(s.hint), extra, ("<code>%s</code>" % esc(str(cur)[:300])) if cur else "<i>empty</i>"),
            kb, lvl=lvl, key=key, group=group, p=p)


@screen("gz")
def settings_empty(ctx, lvl="u", key="", group="*", p="0"):
    lv = level(ctx, lvl)
    if not lv or key not in config.BY_KEY:
        return _bad(ctx)
    lv.layer[key] = ""
    lv.save()
    settings_list(ctx, lvl, group, p)


@inputs("setv")
def got_setting(ctx, msg, st):
    lv, s = level(ctx, st.get("lvl")), config.BY_KEY.get(st.get("key"))
    if not lv or not s:
        ctx.clear()
        return menu(ctx, fresh=True)
    raw = msg.get("text")
    if raw is None:
        return ctx.say("⚠️ Please send text.")
    if s.key == "def_buttons":
        rows, errs, _ = buttons.parse_quick(raw, 1, "", "D")
        if errs:
            return ctx.say("⚠️ " + esc(errs[0]))
        value = raw.strip()
    else:
        value = fmt.entities_to_html(raw, msg.get("entities"))
        if fmt.html_len(value) > 600:
            return ctx.say("⚠️ That is too long (600 characters at most).")
    lv.layer[s.key] = value
    lv.save()
    ctx.forget_input(msg)
    ctx.clear()
    ctx.u["panel"] = ctx.u.get("panel") or {}
    settings_list(ctx, st["lvl"], st.get("group", "*"), st.get("p", 0))


# ----------------------------------------------------------------------------------------------------------------------
# help
# ----------------------------------------------------------------------------------------------------------------------
HELP = {
    "start": ("🚀 Getting started",
              "<b>1.</b> Add this bot as an <b>administrator</b> of your channel (rights: post, edit, delete messages). The channel shows up in 📢 Channels by itself.\n"
              "<b>2.</b> Send me the post: text, photo, video, GIF, file, voice, or several pictures for an album. A forwarded message works too.\n"
              "<b>3.</b> Tap 🔘 Buttons and add links, vote counters, popups… give them colors.\n"
              "<b>4.</b> Check the preview above the panel, choose 📢 Channels, tap 🚀 Publish now (or 🕒 Schedule).\n\n"
              "Published posts can be edited, pinned, copied or deleted in 📚 My posts."),
    "buttons": ("🔘 Buttons and colors",
                "<b>Types</b>: 🔗 link · 👍 vote counter · 💬 popup message · 📋 copy text · 📤 share.\n\n"
                "<b>Colors</b>: 🔵 blue · 🟢 green · 🔴 red (or default). They show in Telegram apps that support colored buttons.\n\n"
                "<b>Quick format</b> (📥 Paste many), one line = one row:\n"
                "<code>Watch now - https://example.com {green}\nEp 1 - https://a.com | Ep 2 - https://b.com {blue}\n👍 - vote\n"
                "Get code - popup: Your code is 1234\nCopy - copy: CODE-123\nShare - share: https://example.com :: Look at this</code>\n\n"
                "Albums cannot carry buttons, so the bot sends a small message with them under the album."),
    "text": ("✍️ Text and formatting",
             "Use Telegram's own formatting (bold, italic, underline, strike, spoiler, quote, code, links). It is kept exactly.\n"
             "Prefer tags? Use <b>HTML mode</b> in ✏️ Text.\n\n<b>Variables</b> (replaced when the post goes out):\n" +
             "\n".join("<code>{%s}</code> %s" % (k, v) for k, v in config.VARIABLES.items()) +
             "\n\n<b>Header / footer</b> and <b>default buttons</b> can be added to every post in ⚙️ Settings → Post defaults."),
    "time": ("🕒 Schedule and auto-delete",
             "In 🕒 Schedule send a time like <code>in 2h</code>, <code>tomorrow 9am</code>, <code>fri 8:30pm</code>, <code>25 nov 6pm</code> or <code>2026-12-01 18:00</code>.\n"
             "Posts can <b>repeat</b> (every few hours, daily, weekly, monthly) and can <b>delete themselves</b> after a time.\n"
             "Set your time zone in ⚙️ Settings → Time & dates.\n\n"
             "<i>On Render's free plan the bot sleeps without traffic; use a free uptime monitor on the /health address so scheduled posts go out on time.</i>"),
    "premium": ("😀 Premium emoji",
                "Premium (custom) emoji are kept in your text. In <b>channels</b>, Telegram allows them only for bots that own a Fragment username; "
                "otherwise the bot automatically sends the normal emoji instead. The same goes for button icons.\n"
                "Turn it off in Options → Premium emoji."),
    "faq": ("🛟 Problems",
            "• <b>Channel not listed</b>: add the bot as an admin again, or use ➕ Add channel.\n• <b>“not enough rights”</b>: give the bot Post / Edit / Delete messages.\n"
            "• <b>Buttons not colored</b>: update Telegram; older apps show normal buttons.\n• <b>Scheduled post late</b>: the free server was asleep; use an uptime monitor.\n"
            "• <b>Caption too long</b>: pictures allow 1024 characters, text posts 4096."),
}


@screen("h")
def help_menu(ctx, topic=""):
    kb = ctx.kb()
    if topic in HELP:
        kb.row(kb.btn("⬅️ Back", "h"))
        return ctx.show("<b>%s</b>\n\n%s" % (HELP[topic][0], HELP[topic][1]), kb)
    keys = list(HELP)
    for k in range(0, len(keys), 2):
        kb.row(*[kb.btn(HELP[x][0], "h", x, style="primary" if x == "start" else "") for x in keys[k:k + 2]])
    kb.row(kb.btn("🏠 Menu", "m"))
    ctx.show("<b>❓ Help</b>\nChoose a topic.", kb)


# ----------------------------------------------------------------------------------------------------------------------
# admin (owners only)
# ----------------------------------------------------------------------------------------------------------------------
def owner_only(fn):
    def wrapper(ctx, *a):
        if not core.is_owner(ctx.uid):
            return ctx.toast("Only the owner can do this.", True)
        return fn(ctx, *a)
    wrapper.__name__ = fn.__name__
    return wrapper


@screen("ad")
@owner_only
def admin(ctx, note=""):
    m, st = core.meta(), core.stats()
    kb = ctx.kb()
    kb.row(kb.btn("👥 Users (%d)" % len(m.get("allowed", [])), "adu", 0), kb.btn("🌐 Public: " + ("ON" if m.get("public") else "OFF"), "adp", style="success" if m.get("public") else ""))
    kb.row(kb.btn("💾 Backup", "adb", style="primary"), kb.btn("♻️ Restore", "adr"))
    kb.row(kb.btn("🔄 Refresh", "ad"), kb.btn("🏠 Menu", "m"))
    text = ("<b>👑 Admin</b>\nStorage: <b>%s</b>%s\n👤 Users %d · 📢 Channels %d · 📝 Drafts %d · 📚 Posts %d · 🗓 Scheduled %d · 📋 Templates %d\n"
            "🌐 Public mode: <b>%s</b> (%s)") % (
        "MongoDB" if core.db.kind == "mongo" else "local file ⚠️", ("\n<i>%s</i>" % esc(core.db.error)) if getattr(core.db, "error", "") else "",
        st["users"], st["channels"], st["drafts"], st["posts"], st["sched"], st["templates"], "ON" if m.get("public") else "OFF",
        "anyone can use the bot" if m.get("public") else "only you and the users you add")
    ctx.show(text + (("\n\n" + note) if note else ""), kb)


@screen("adp")
@owner_only
def admin_public(ctx):
    m = core.meta()
    m["public"] = not m.get("public")
    core.save_meta(m)
    admin(ctx)


@screen("adu")
@owner_only
def admin_users(ctx, p="0"):
    allowed = core.meta().get("allowed", [])
    page, p, pages = paged(allowed, p, ctx.size)
    kb = ctx.kb()
    for uid in page:
        u = core.db.get("users", uid)
        kb.row(kb.btn("🗑 %s (%d)" % ((u or {}).get("name") or "user", uid), "adux", uid, style="danger"))
    pager(kb, "adu", [], p, pages)
    kb.row(kb.btn("➕ Add user", "adua", style="success"), kb.btn("⬅️ Back", "ad"))
    ctx.show("<b>👥 Allowed users</b>\nOwners: %s\n%s" % (", ".join("<code>%d</code>" % o for o in config.Config.OWNER_IDS),
             "Tap a user to remove access." if allowed else "No extra users. Add team members who may use the bot."), kb)


@screen("adua")
@owner_only
def admin_user_add(ctx):
    kb = ctx.kb()
    kb.row(kb.btn("⬅️ Back", "adu", 0))
    ctx.ask("adduser", "<b>➕ Add a user</b>\nSend the person's numeric Telegram ID, or forward one of their messages.", kb)


@inputs("adduser")
def got_adduser(ctx, msg, st):
    if not core.is_owner(ctx.uid):
        return
    fo = msg.get("forward_origin") or {}
    uid = (fo.get("sender_user") or {}).get("id") or (msg.get("forward_from") or {}).get("id")
    t = (msg.get("text") or "").strip()
    if not uid and re.fullmatch(r"\d{5,}", t):
        uid = int(t)
    if not uid:
        return ctx.say("⚠️ Send a numeric ID, or forward a message from that person (their privacy settings may hide it; then send the ID).")
    m = core.meta()
    if uid not in m["allowed"]:
        m["allowed"].append(uid)
        core.save_meta(m)
    ctx.forget_input(msg)
    ctx.clear()
    admin_users(ctx)


@screen("adux")
@owner_only
def admin_user_remove(ctx, uid="0"):
    m = core.meta()
    m["allowed"] = [x for x in m["allowed"] if str(x) != str(uid)]
    core.save_meta(m)
    admin_users(ctx)


@screen("adb")
@owner_only
def admin_backup(ctx):
    data = json.dumps(core.db.dump(), ensure_ascii=False, default=str).encode("utf-8")
    try:
        core.tg("sendDocument", chat_id=ctx.uid, files={"document": ("post_studio_backup.json", data, "application/json")},
                caption="💾 Backup of all data (%d KB). Keep it safe." % (len(data) // 1024 + 1))
        ctx.toast("Backup sent.")
    except TgError as e:
        ctx.toast(e.friendly(), True)


@screen("adr")
@owner_only
def admin_restore(ctx):
    kb = ctx.kb()
    kb.row(kb.btn("⬅️ Back", "ad"))
    ctx.ask("restore", "<b>♻️ Restore</b>\nSend a backup file (<code>post_studio_backup.json</code>). Existing items with the same ID are replaced.", kb)


@inputs("restore")
def got_restore(ctx, msg, st):
    if not core.is_owner(ctx.uid):
        return
    doc = msg.get("document")
    if not doc:
        return ctx.say("⚠️ Send the backup file as a document.")
    try:
        info = core.tg("getFile", file_id=doc["file_id"])
        data = json.loads(core.tg.download(info["file_path"]).decode("utf-8"))
        core.db.restore(data)
        core.meta()
        core._meta["d"] = None
        note = "✅ Restored %d items." % sum(len(v) for v in data.values() if isinstance(v, list))
    except Exception as e:
        note = "⚠️ That file could not be restored: %s" % esc(str(e)[:100])
    ctx.clear()
    admin(ctx, note)
