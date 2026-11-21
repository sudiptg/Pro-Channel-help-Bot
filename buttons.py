"""Inline buttons: the model, the quick text format, layout moves and the conversion to a Telegram keyboard.

A button is a dict:  {"id": "a", "k": kind, "t": label, "v": value, "s": style, "i": premium emoji id}
kinds: url (opens a link) | vote (counter) | alert (popup text) | copy (copies text) | share (opens the share dialog)
"""
import re
from urllib.parse import quote

import config

KINDS = [("url", "🔗", "Link"), ("vote", "👍", "Vote counter"), ("alert", "💬", "Popup"), ("copy", "📋", "Copy text"), ("share", "📤", "Share")]
KIND_NAME = {k: n for k, _, n in KINDS}
KIND_ICON = {k: i for k, i, _ in KINDS}
STYLE_WORDS = {"blue": "primary", "primary": "primary", "green": "success", "success": "success", "red": "danger", "danger": "danger",
               "plain": "", "default": "", "none": "", "gray": "", "grey": ""}
STYLE_NAME = {"primary": "blue", "success": "green", "danger": "red", "": "default"}
ALERT_MAX, COPY_MAX = 200, 256


class QuickError(ValueError):
    pass


def clean_url(u):
    u = (u or "").strip()
    if not u or any(c.isspace() for c in u):
        return None
    if re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{3,31}", u):
        return "https://t.me/" + u[1:]
    if re.match(r"(?i)^tg://[a-z_]+", u):
        return u
    if re.match(r"(?i)^https?://", u):
        pass
    elif re.match(r"(?i)^(www\.|t\.me/|telegram\.me/)", u) or re.match(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+(/|$|\?|#)", u):
        u = "https://" + u
    else:
        return None
    m = re.match(r"(?i)^https?://([^/?#]+)", u)
    host = m.group(1).rsplit("@", 1)[-1] if m else ""
    if not re.fullmatch(r"[A-Za-z0-9._-]+(:\d+)?", host or "") or ("." not in host and not host.startswith("localhost")):
        return None
    return u[:2000]


def share_url(value):
    link, _, text = (value or "").partition("::")
    return "https://t.me/share/url?url=%s&text=%s" % (quote(link.strip(), safe=""), quote(text.strip(), safe=""))


def check_value(kind, value):
    """Returns (clean value, error)."""
    v = (value or "").strip()
    if kind == "vote":
        return "", None
    if kind == "url":
        u = clean_url(v)
        return (u, None) if u else (None, "That is not a valid link. Use https://… or @username.")
    if kind == "alert":
        if not v:
            return None, "The popup text is empty."
        return (v, None) if len(v) <= ALERT_MAX else (None, "Popup text can be %d characters at most (yours: %d)." % (ALERT_MAX, len(v)))
    if kind == "copy":
        if not v:
            return None, "The text to copy is empty."
        return (v, None) if len(v) <= COPY_MAX else (None, "Copied text can be %d characters at most (yours: %d)." % (COPY_MAX, len(v)))
    if kind == "share":
        link = v.partition("::")[0].strip()
        if not link:
            return None, "Send a link to share (optional: link :: message text)."
        return v, None
    return None, "Unknown button type."


def alloc(draft):
    draft["bn"] = int(draft.get("bn", 0)) + 1
    return format(draft["bn"], "x")


def make(kind, label, value="", style="", icon="", bid="0"):
    return {"id": bid, "k": kind, "t": str(label).strip()[:config.LABEL_MAX], "v": value, "s": style if style in ("primary", "success", "danger") else "",
            "i": str(icon or "")}


# ----------------------------------------------------------------------------------------------------------------------
# quick text format
#   Watch now - https://example.com                    one button per line = one row
#   Ep 1 - https://a.com | Ep 2 - https://b.com        several buttons in one row, separated by  " | "
#   👍 - vote                                           vote counter
#   Get code - popup: Your code is 1234                popup message
#   Copy - copy: SOME-TEXT                             copies text
#   Share - share: https://link :: Look at this        share dialog
#   add a colour at the end:  {blue} {green} {red}     and a Premium emoji icon:  {emoji:5368324170671202286}
# ----------------------------------------------------------------------------------------------------------------------
_TAG = re.compile(r"\s*[\[{(]\s*(emoji\s*[:=]\s*(\d{5,30})|[A-Za-z]+)\s*[\]})]\s*$")
_KW = re.compile(r"^(?P<label>.+?)\s+[-–—]\s+(?P<kw>vote|popup|alert|copy|share)\b\s*(?::\s*(?P<val>.*))?$", re.I | re.S)
_URL = re.compile(r"^(?P<label>.+)\s+[-–—]\s+(?P<url>\S+)$", re.S)


def _tags(part):
    style, icon = None, ""
    while True:
        m = _TAG.search(part)
        if not m:
            break
        word = m.group(1).lower().replace(" ", "")
        if m.group(2):
            icon = m.group(2)
        elif word in STYLE_WORDS:
            style = STYLE_WORDS[word]
        else:
            break
        part = part[:m.start()]
    return part.strip(), style, icon


def parse_part(part, default_style=""):
    part, style, icon = _tags(part)
    m = _KW.match(part)
    if m:
        kind = {"popup": "alert", "alert": "alert"}.get(m.group("kw").lower(), m.group("kw").lower())
        label, val = m.group("label").strip(), (m.group("val") or "").strip().replace("\\n", "\n")
    else:
        m = _URL.match(part)
        if not m:
            raise QuickError('"%s" needs a target. Use:  Text - https://link' % part[:30])
        kind, label, val = "url", m.group("label").strip(), m.group("url")
    if not label:
        raise QuickError("A button has no text.")
    val, err = check_value(kind, val)
    if err:
        raise QuickError('"%s": %s' % (label[:24], err))
    return make(kind, label, val, default_style if style is None else style, icon)


def parse_quick(text, nxt=1, default_style="", id_prefix=""):
    """-> (rows, errors, next_counter). Nothing is returned as rows when there is any error."""
    rows, errors, n = [], [], nxt
    for ln, line in enumerate(str(text or "").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        row = []
        for part in re.split(r"\s+\|\s+", line):
            if not part.strip():
                continue
            try:
                b = parse_part(part, default_style)
            except QuickError as e:
                errors.append("Line %d: %s" % (ln, e))
                continue
            b["id"] = id_prefix + format(n, "x")
            n += 1
            row.append(b)
        if len(row) > config.ROW_MAX:
            errors.append("Line %d: at most %d buttons fit in one row." % (ln, config.ROW_MAX))
        if row:
            rows.append(row)
    if sum(len(r) for r in rows) > config.BUTTON_MAX:
        errors.append("Too many buttons (limit %d)." % config.BUTTON_MAX)
    return (rows if not errors else []), errors, n


def part_text(b):
    k, v = b["k"], b.get("v", "")
    tgt = {"url": v, "vote": "vote", "alert": "popup: " + v.replace("\n", "\\n"), "copy": "copy: " + v.replace("\n", "\\n"),
           "share": "share: " + v}.get(k, v)
    s = "%s - %s" % (b["t"], tgt)
    if b.get("s"):
        s += " {%s}" % STYLE_NAME[b["s"]]
    if b.get("i"):
        s += " {emoji:%s}" % b["i"]
    return s


def to_quick(rows):
    return "\n".join(" | ".join(part_text(b) for b in row) for row in rows if row)


# ----------------------------------------------------------------------------------------------------------------------
# layout
# ----------------------------------------------------------------------------------------------------------------------
def flat(rows):
    return [b for r in rows for b in r]


def count(rows):
    return sum(len(r) for r in rows)


def locate(rows, bid):
    for r, row in enumerate(rows):
        for c, b in enumerate(row):
            if b["id"] == bid:
                return r, c
    return None


def get(rows, bid):
    pos = locate(rows, bid)
    return rows[pos[0]][pos[1]] if pos else None


def tidy(rows):
    return [r for r in rows if r]


def remove(rows, bid):
    pos = locate(rows, bid)
    if pos:
        rows[pos[0]].pop(pos[1])
    return tidy(rows)


def add(rows, b, new_row=True):
    if new_row or not rows or len(rows[-1]) >= config.ROW_MAX:
        rows.append([b])
    else:
        rows[-1].append(b)
    return rows


def move(rows, bid, where):
    """where: left | right | up | down. Returns the new rows."""
    pos = locate(rows, bid)
    if not pos:
        return rows
    r, c = pos
    row = rows[r]
    if where == "left":
        if c > 0:
            row[c - 1], row[c] = row[c], row[c - 1]
        elif r > 0 and len(rows[r - 1]) < config.ROW_MAX:
            rows[r - 1].append(row.pop(0))
    elif where == "right":
        if c < len(row) - 1:
            row[c + 1], row[c] = row[c], row[c + 1]
        elif r < len(rows) - 1 and len(rows[r + 1]) < config.ROW_MAX:
            rows[r + 1].insert(0, row.pop())
    elif where == "up":
        if len(row) > 1:
            rows.insert(r, [row.pop(c)])
        elif r > 0:
            rows[r - 1], rows[r] = rows[r], rows[r - 1]
    elif where == "down":
        if len(row) > 1:
            rows.insert(r + 1, [row.pop(c)])
        elif r < len(rows) - 1:
            rows[r + 1], rows[r] = rows[r], rows[r + 1]
    return tidy(rows)


def set_style_all(rows, style):
    for b in flat(rows):
        b["s"] = style
    return rows


# ----------------------------------------------------------------------------------------------------------------------
# Telegram keyboard
# ----------------------------------------------------------------------------------------------------------------------
def render(rows, counts=None, show_counts=True, dot=False, icons=True):
    out = []
    for row in rows or []:
        r = []
        for b in row:
            t = b["t"]
            if b["k"] == "vote" and show_counts:
                n = (counts or {}).get(b["id"], 0)
                if n:
                    t = "%s %d" % (t, n)
            if dot and b.get("s"):
                t = config.STYLE_DOT[b["s"]] + " " + t
            d = {"text": t[:config.LABEL_MAX] or "•"}
            k = b["k"]
            if k == "url":
                d["url"] = b["v"]
            elif k == "vote":
                d["callback_data"] = "v:" + b["id"]
            elif k == "alert":
                d["callback_data"] = "a:" + b["id"]
            elif k == "copy":
                d["copy_text"] = {"text": b["v"]}
            elif k == "share":
                d["url"] = share_url(b["v"])
            if b.get("s"):
                d["style"] = b["s"]
            if icons and b.get("i"):
                d["icon_custom_emoji_id"] = b["i"]
            r.append(d)
        if r:
            out.append(r)
    return {"inline_keyboard": out} if out else None


def plain_markup(markup):
    """The same keyboard without Premium emoji icons (used when Telegram refuses them)."""
    if not markup:
        return markup
    return {"inline_keyboard": [[{k: v for k, v in b.items() if k != "icon_custom_emoji_id"} for b in row] for row in markup["inline_keyboard"]]}


def has_icons(markup):
    return bool(markup) and any("icon_custom_emoji_id" in b for row in markup["inline_keyboard"] for b in row)


def merge_default(rows, default_rows, pos="bottom"):
    if not default_rows:
        return rows
    return (default_rows + rows) if pos == "top" else (rows + default_rows)
