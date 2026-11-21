"""Menu framework: keyboards, panels (messages that are edited in place), paging and text-input state.
A callback is   name|arg|arg   (max 64 bytes).  Screens are functions  fn(ctx, *args)  registered with @screen("name")."""
import logging
import random

import buttons
import core
import fmt
from tgapi import TgError

log = logging.getLogger("ui")
SCREENS, INPUTS, HOOKS = {}, {}, {}
SEP = "|"
TIPS = [
    "Send me any text, photo, video or file and it becomes a new draft.",
    "Colored buttons: add {green}, {blue} or {red} at the end of a button line.",
    "Use {date} and {channel} in your text; they are filled in when the post goes out.",
    "A header and footer can be added to every post in Settings.",
    "Several channels? Select them all in the draft and publish once.",
    "Albums cannot carry buttons, so the bot adds a small message with them.",
    "Vote buttons count taps: write  👍 - vote  in the quick button format.",
    "Auto-delete removes a post from the channel after the time you choose.",
]


def screen(name):
    def deco(fn):
        SCREENS[name] = fn
        return fn
    return deco


def inputs(name):
    def deco(fn):
        INPUTS[name] = fn
        return fn
    return deco


esc = fmt.esc


def snip(html, n=34):
    return esc(fmt.shorten(fmt.html_text(html), n)) or "<i>no text</i>"


def plain_snip(html, n=30):
    return fmt.shorten(fmt.html_text(html), n) or "(no text)"


class Kb:
    """Inline keyboard builder. Colors are left out when the user switched them off."""

    def __init__(self, colors=True):
        self.rows, self.colors = [], colors

    def btn(self, text, *data, style="", url=None):
        d = {"text": str(text)[:64] or "•"}
        if url:
            d["url"] = url
        else:
            d["callback_data"] = SEP.join(str(x) for x in data)[:64]
        if style and self.colors:
            d["style"] = style
        return d

    def row(self, *items):
        items = [i for i in items if i]
        if items:
            self.rows.append(items)
        return self

    def markup(self):
        return {"inline_keyboard": [r for r in self.rows if r]}


def paged(items, p, size):
    pages = max(1, -(-len(items) // size))
    try:
        p = min(max(0, int(p)), pages - 1)
    except (TypeError, ValueError):
        p = 0
    return items[p * size:(p + 1) * size], p, pages


def pager(kb, name, args, p, pages):
    if pages > 1:
        kb.row(kb.btn("◀️", name, *args, p - 1) if p > 0 else kb.btn("·", "x"), kb.btn("%d / %d" % (p + 1, pages), "x"),
               kb.btn("▶️", name, *args, p + 1) if p < pages - 1 else kb.btn("·", "x"))


class Ctx:
    """Everything one update needs: the user, the panel message and the answer to the tapped button."""

    def __init__(self, user, msg=None, cq=None):
        self.u, self.uid, self.cq = user, user["_id"], cq
        self.msg = msg if msg and msg.get("date", 1) != 0 else None            # date == 0 means Telegram no longer has the message
        self.answered = False
        self.fresh_next = False                                              # the next panel goes to the bottom of the chat (commands)

    # ---- helpers
    @property
    def tg(self):
        return core.tg

    def cfg(self, key):
        return core.ucfg(self.u, key)

    def kb(self):
        return Kb(bool(self.cfg("ui_colors")))

    @property
    def size(self):
        return int(self.cfg("page_size"))

    def tip(self):
        return "\n\n<i>💡 %s</i>" % esc(random.choice(TIPS)) if self.cfg("ui_hints") else ""

    def fts(self, ts):
        return fmt.fmt_ts(ts, core.tz_of(self.u), self.cfg("time12"), self.cfg("date_fmt"))

    def toast(self, text="", alert=False):
        if self.cq and not self.answered:
            self.answered = True
            core.tg.ok("answerCallbackQuery", callback_query_id=self.cq["id"], text=(text[:200] or None), show_alert=True if alert else None)

    # ---- state
    def ask(self, name, text, kb=None, **args):
        self.u["st"] = dict(args, n=name)
        self.show(text, kb)

    def clear(self):
        self.u["st"] = {}

    def forget_input(self, msg):
        core.tg.ok("deleteMessage", chat_id=self.uid, message_id=msg["message_id"])

    # ---- the panel
    def show(self, text, kb=None, fresh=None):
        if fresh is None:
            fresh, self.fresh_next = self.fresh_next, False
        markup = kb.markup() if isinstance(kb, Kb) else kb
        text = text[:4050]
        panel = self.u.get("panel") or {}
        target = None
        if not fresh:
            target = {"c": self.msg["chat"]["id"], "m": self.msg["message_id"]} if self.msg else (panel or None)
        if target:
            try:
                core.tg("editMessageText", chat_id=target["c"], message_id=target["m"], text=text, parse_mode="HTML",
                        link_preview_options={"is_disabled": True}, reply_markup=markup)
                self.u["panel"] = target
                return target
            except TgError as e:
                if e.kind == "same":
                    self.u["panel"] = target
                    return target
                log.info("panel edit failed (%s), sending a new one", e)
        m = core.tg("sendMessage", chat_id=self.uid, text=text, parse_mode="HTML", link_preview_options={"is_disabled": True}, reply_markup=markup)
        old = {panel.get("m"), self.msg["message_id"] if self.msg else None} - {None, m["message_id"]}
        for mid in old:
            core.tg.ok("deleteMessage", chat_id=self.uid, message_id=mid)
        self.u["panel"] = {"c": self.uid, "m": m["message_id"]}
        return self.u["panel"]

    def say(self, text, kb=None):
        """A separate message (not the panel)."""
        return core.tg.ok("sendMessage", chat_id=self.uid, text=text[:4050], parse_mode="HTML", link_preview_options={"is_disabled": True},
                          reply_markup=kb.markup() if isinstance(kb, Kb) else kb)

    # ---- the draft preview (the real post, shown above the draft panel)
    def drop_preview(self):
        prev = self.u.get("prev") or {}
        for mid in prev.get("ms", []):
            core.tg.ok("deleteMessage", chat_id=self.uid, message_id=mid)
        self.u["prev"] = {}

    def stale(self):
        if self.u.get("prev"):
            self.u["prev"]["stale"] = True


def style_name(s):
    return buttons.STYLE_NAME.get(s or "", "default")


def yes_no(v):
    return "ON" if v else "OFF"
