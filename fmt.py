"""Text helpers: Telegram entities -> HTML, HTML checking, {variables}, dates and times."""
import bisect
import html as _html
import re
from datetime import datetime, timedelta

EPOCH = datetime(1970, 1, 1)


def esc(s):
    return _html.escape("" if s is None else str(s), quote=False)


def attr(s):
    return _html.escape("" if s is None else str(s), quote=True)


def u16len(s):
    return sum(2 if ord(c) > 0xFFFF else 1 for c in s or "")


def shorten(s, n=40):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# ----------------------------------------------------------------------------------------------------------------------
# entities -> HTML   (Telegram offsets count UTF-16 code units)
# ----------------------------------------------------------------------------------------------------------------------
_SIMPLE = {
    "bold": ("<b>", "</b>"), "italic": ("<i>", "</i>"), "underline": ("<u>", "</u>"), "strikethrough": ("<s>", "</s>"),
    "spoiler": ("<tg-spoiler>", "</tg-spoiler>"), "code": ("<code>", "</code>"), "blockquote": ("<blockquote>", "</blockquote>"),
    "expandable_blockquote": ("<blockquote expandable>", "</blockquote>"),
}


def entities_to_html(text, entities, custom_emoji=True):
    text = text or ""
    pos = [0]
    for ch in text:
        pos.append(pos[-1] + (2 if ord(ch) > 0xFFFF else 1))

    def py(off):
        return min(bisect.bisect_left(pos, off), len(text))

    ents = []
    for e in entities or []:
        t = e.get("type")
        a, b = py(e.get("offset", 0)), py(e.get("offset", 0) + e.get("length", 0))
        if b <= a:
            continue
        if t in _SIMPLE:
            o, c = _SIMPLE[t]
        elif t == "pre":
            lang = e.get("language")
            o, c = ("<pre>", "</pre>") if not lang else ('<pre><code class="language-%s">' % attr(lang), "</code></pre>")
        elif t == "text_link" and e.get("url"):
            o, c = '<a href="%s">' % attr(e["url"]), "</a>"
        elif t == "text_mention" and e.get("user"):
            o, c = '<a href="tg://user?id=%s">' % int(e["user"]["id"]), "</a>"
        elif t == "custom_emoji" and custom_emoji and e.get("custom_emoji_id"):
            o, c = '<tg-emoji emoji-id="%s">' % attr(e["custom_emoji_id"]), "</tg-emoji>"
        else:
            continue
        ents.append([a, b, o, c])
    points = sorted({0, len(text)} | {e[0] for e in ents} | {e[1] for e in ents})
    out, stack = [], []
    for i, p in enumerate(points):
        if any(e[1] == p for e in stack):
            reopen = []
            while any(e[1] == p for e in stack):
                top = stack.pop()
                out.append(top[3])
                if top[1] != p:
                    reopen.append(top)
            for e in reversed(reopen):
                out.append(e[2])
                stack.append(e)
        for e in sorted([x for x in ents if x[0] == p], key=lambda x: -x[1]):
            out.append(e[2])
            stack.append(e)
        if i + 1 < len(points):
            out.append(esc(text[p:points[i + 1]]))
    return "".join(out)


def plain_to_html(text):
    return esc(text)


# ----------------------------------------------------------------------------------------------------------------------
# HTML checking (for the "HTML" input mode and header/footer texts)
# ----------------------------------------------------------------------------------------------------------------------
_ALLOWED = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre", "tg-spoiler", "span", "tg-emoji",
            "blockquote", "tg-time"}
_TOKEN = re.compile(
    r"<(/?)([A-Za-z][A-Za-z0-9-]*)((?:\s+[A-Za-z-]+(?:\s*=\s*(?:\"[^\"<>]*\"|'[^'<>]*'))?)*)\s*>"
    r"|(&(?:amp|lt|gt|quot|apos|#\d{1,7}|#x[0-9A-Fa-f]{1,6});)|([&<>])|([^&<>]+)")
_ATTR = re.compile(r"\s+([A-Za-z-]+)(?:\s*=\s*(\"[^\"<>]*\"|'[^'<>]*'))?")


def sanitize_html(src):
    """Returns (clean_html, None) or (None, 'what is wrong'). Stray & < > become entities."""
    out, stack = [], []
    for m in _TOKEN.finditer(src or ""):
        closing, name, attrs, ent, stray, txt = m.groups()
        if txt is not None or ent is not None:
            out.append(m.group(0))
        elif stray is not None:
            out.append({"&": "&amp;", "<": "&lt;", ">": "&gt;"}[stray])
        else:
            name = name.lower()
            if name not in _ALLOWED:
                out.append(esc(m.group(0)))
                continue
            if closing:
                if not stack or stack[-1] != name:
                    return None, "The tag </%s> is closed in the wrong place." % name
                stack.pop()
                out.append("</%s>" % name)
                continue
            found = {k.lower(): (v or "")[1:-1] for k, v in _ATTR.findall(attrs or "")}
            if name == "a" and not found.get("href"):
                return None, 'A link needs an address: <a href="https://…">text</a>'
            if name == "tg-emoji" and not found.get("emoji-id"):
                return None, 'A custom emoji needs an id: <tg-emoji emoji-id="…">😀</tg-emoji>'
            if name == "span" and found.get("class") != "tg-spoiler":
                return None, 'Only <span class="tg-spoiler"> is allowed.'
            bad = set(found) - {"href", "emoji-id", "class", "expandable", "unix", "format"}
            if bad:
                return None, "The attribute %s is not allowed." % sorted(bad)[0]
            stack.append(name)
            out.append("<%s%s>" % (name, "".join(' %s="%s"' % (k, attr(v)) if v or k in ("href", "emoji-id", "class", "unix", "format") else " " + k
                                                 for k, v in found.items())))
    if stack:
        return None, "The tag <%s> is never closed." % stack[-1]
    return "".join(out), None


_TAG_RE = re.compile(r"<[^>]+>")


def html_text(h):
    return _html.unescape(_TAG_RE.sub("", h or ""))


def html_len(h):
    return u16len(html_text(h))


def strip_custom_emoji(h):
    return re.sub(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", r"\1", h or "", flags=re.S)


def has_custom_emoji(h):
    return "<tg-emoji" in (h or "")


# ----------------------------------------------------------------------------------------------------------------------
# {variables}
# ----------------------------------------------------------------------------------------------------------------------
_VAR = re.compile(r"\{([a-z_]+)\}")


def expand(text, values):
    return _VAR.sub(lambda m: values[m.group(1)] if m.group(1) in values else m.group(0), text or "")


# ----------------------------------------------------------------------------------------------------------------------
# time zone helpers (a fixed UTC offset in minutes, India has no daylight saving)
# ----------------------------------------------------------------------------------------------------------------------
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def local_dt(ts, tz):
    return EPOCH + timedelta(seconds=int(ts) + tz * 60)


def to_ts(local, tz):
    return int((local - EPOCH).total_seconds() - tz * 60)


def fmt_date(ts, tz, date_fmt="dmy"):
    d = local_dt(ts, tz)
    if date_fmt == "iso":
        return d.strftime("%Y-%m-%d")
    if date_fmt == "dmy2":
        return d.strftime("%d/%m/%Y")
    if date_fmt == "mdy":
        return d.strftime("%m/%d/%Y")
    return "%d %s %d" % (d.day, MONTHS[d.month - 1][:3].title(), d.year)


def fmt_clock(ts, tz, time12=True):
    d = local_dt(ts, tz)
    if time12:
        h = d.hour % 12 or 12
        return "%d:%02d %s" % (h, d.minute, "AM" if d.hour < 12 else "PM")
    return "%02d:%02d" % (d.hour, d.minute)


def fmt_ts(ts, tz=330, time12=True, date_fmt="dmy"):
    return "%s, %s" % (fmt_date(ts, tz, date_fmt), fmt_clock(ts, tz, time12))


def fmt_dur(secs):
    secs = int(abs(secs))
    if secs < 60:
        return "%ds" % secs
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    parts = []
    if d:
        parts.append("%dd" % d)
    if h:
        parts.append("%dh" % h)
    if m and not d:
        parts.append("%dm" % m)
    return " ".join(parts[:2]) or "0m"


def fmt_rel(ts, now):
    diff = int(ts - now)
    if abs(diff) < 45:
        return "now"
    return ("in " + fmt_dur(diff)) if diff > 0 else (fmt_dur(diff) + " ago")


_UNITS = {"w": 604800, "week": 604800, "weeks": 604800, "d": 86400, "day": 86400, "days": 86400,
          "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
          "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60, "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1}
_DUR = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]+)")


def parse_duration(text):
    s = str(text or "").lower().strip()
    if not s:
        return None
    total, end = 0.0, 0
    for m in _DUR.finditer(s):
        gap = s[end:m.start()].strip(" ,&")
        if gap and gap != "and":
            return None
        if m.group(2) not in _UNITS:
            return None
        total += float(m.group(1)) * _UNITS[m.group(2)]
        end = m.end()
    if s[end:].strip(" ,&") or total <= 0:
        return None
    return int(total)


_TIME = re.compile(r"(?:^|\s|t)(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?$")


def _month(word):
    w = word.strip(".").lower()
    if len(w) < 3:
        return None
    for i, name in enumerate(MONTHS):
        if name.startswith(w) or (w == "sept" and i == 8):
            return i + 1
    return None


def _weekday(word):
    w = word.strip(".").lower()
    if len(w) < 3:
        return None
    for i, name in enumerate(DAYS):
        if name.startswith(w):
            return i
    return None


def parse_when(text, now, tz=330):
    """'in 2h 30m', '7pm', 'tomorrow 9:30', 'fri 8pm', '25 nov 6:30pm', '2026-11-25 18:30', '25/11/2026 18:30' -> UTC timestamp."""
    s = " ".join(str(text or "").lower().replace(",", " ").replace(" at ", " ").split())
    if not s:
        return None
    m = re.fullmatch(r"(?:in|after|\+)\s*(.+?)", s) or re.fullmatch(r"(.+?)\s+(?:later|from now)", s)
    if m:
        d = parse_duration(m.group(1))
        return int(now) + d if d else None
    d = parse_duration(s)
    if d:
        return int(now) + d
    hh = mm = None
    tm = _TIME.search(s)
    if tm and (tm.group(2) or tm.group(3)):
        h, mi, ap = int(tm.group(1)), int(tm.group(2) or 0), (tm.group(3) or "").replace(".", "")
        if mi > 59 or h > 23 or (ap and not 1 <= h <= 12):
            return None
        if ap == "pm" and h != 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        hh, mm = h, mi
        s = s[:tm.start()].strip()
    nowl = local_dt(now, tz)
    today = nowl.replace(hour=0, minute=0, second=0, microsecond=0)
    day = None
    if s in ("", "today", "tonight"):
        day = today
        if s == "tonight" and hh is None:
            hh, mm = 20, 0
        if s == "" and hh is None:
            return None
    elif s in ("tomorrow", "tmr", "tom", "tmrw"):
        day = today + timedelta(days=1)
    elif s in ("day after tomorrow", "overmorrow"):
        day = today + timedelta(days=2)
    else:
        wd = _weekday(s)
        if wd is not None:
            delta = (wd - today.weekday()) % 7
            day = today + timedelta(days=delta)
        else:
            day = _parse_date(s, today)
            if day is None:
                return None
    if hh is None:
        hh, mm = 9, 0
    local = day.replace(hour=hh, minute=mm)
    ts = to_ts(local, tz)
    if ts <= now and (s in ("", "today") or _weekday(s) is not None):      # "7pm" said at 9pm means tomorrow; "friday" on a Friday night means next Friday
        ts = to_ts(local + timedelta(days=1 if s in ("", "today") else 7), tz)
    return ts


def _parse_date(s, today):
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return _mk(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if mo > 12 >= d:
            d, mo = mo, d
        return _mk(y + 2000 if y < 100 else y, mo, d)
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})", s)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        if mo > 12 >= d:
            d, mo = mo, d
        return _roll(today, mo, d)
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z.]+)(?:\s+(\d{4}))?", s)
    if m and _month(m.group(2)):
        return _mk(int(m.group(3)), _month(m.group(2)), int(m.group(1))) if m.group(3) else _roll(today, _month(m.group(2)), int(m.group(1)))
    m = re.fullmatch(r"([a-z.]+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s+(\d{4}))?", s)
    if m and _month(m.group(1)):
        return _mk(int(m.group(3)), _month(m.group(1)), int(m.group(2))) if m.group(3) else _roll(today, _month(m.group(1)), int(m.group(2)))
    return None


def _mk(y, mo, d):
    try:
        return datetime(y, mo, d)
    except ValueError:
        return None


def _roll(today, mo, d):
    dt = _mk(today.year, mo, d)
    if dt is None:
        return None
    return dt if dt >= today else _mk(today.year + 1, mo, d)


def add_months(local, n, day=None):
    y, m = divmod(local.month - 1 + n, 12)
    y, m = local.year + y, m + 1
    for d in (day or local.day, 30, 29, 28):
        try:
            return local.replace(year=y, month=m, day=d)
        except ValueError:
            continue
    return local


def next_repeat(ts, rule, tz, now):
    """Next run after `ts` for a repeat rule {'kind': interval|daily|weekly|monthly, 'secs': n}. Always in the future."""
    kind = rule.get("kind")
    if kind == "interval":
        step = max(60, int(rule.get("secs", 3600)))
        n = int((now - ts) // step) + 1 if now >= ts else 1
        return ts + step * max(1, n)
    local = local_dt(ts, tz)
    k = 1
    while True:
        if kind == "daily":
            nxt = local + timedelta(days=k)
        elif kind == "weekly":
            nxt = local + timedelta(days=7 * k)
        else:
            nxt = add_months(local, k, rule.get("day"))
        t = to_ts(nxt, tz)
        if t > now:
            return t
        k += 1
