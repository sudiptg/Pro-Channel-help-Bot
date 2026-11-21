"""Post Studio - configuration and the list of every setting.

Set these in Render -> Environment:
    BOT_TOKEN   token from @BotFather                                   (required)
    OWNER_ID    your numeric Telegram ID (several IDs: 111,222)         (required)
    MONGO_URI   mongodb+srv://...   (recommended: without it all data is lost when Render restarts)
    DB_NAME     database name, default: post_studio
    APP_NAME    name shown in the menus, default: Post Studio
    DEFAULT_TZ  default time zone as a UTC offset, default: +05:30
Optional: WEBHOOK_BASE (Render sets RENDER_EXTERNAL_URL itself), USE_POLLING=1, DENY_TEXT, TICK_SECONDS.
"""
import hashlib
import os


def _env(name, default=""):
    v = os.environ.get(name)
    return default if v is None else str(v).strip()


def _ids(raw):
    out = []
    for p in str(raw or "").replace(";", ",").replace(" ", ",").split(","):
        p = p.strip()
        if p.lstrip("-").isdigit():
            out.append(int(p))
    return out


def _flag(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


class Config:
    pass


def load():
    c = Config
    c.APP_NAME = _env("APP_NAME", "Post Studio")
    c.BOT_TOKEN = _env("BOT_TOKEN")
    c.OWNER_IDS = _ids(_env("OWNER_ID") or _env("OWNER_IDS"))
    c.MONGO_URI = _env("MONGO_URI")
    c.DB_NAME = _env("DB_NAME", "post_studio")
    c.DATA_FILE = _env("DATA_FILE", "post_studio_data.json")
    c.API_BASE = _env("TG_API_BASE", "https://api.telegram.org").rstrip("/")
    c.PUBLIC_URL = (_env("WEBHOOK_BASE") or _env("PUBLIC_URL") or _env("RENDER_EXTERNAL_URL")).rstrip("/")
    seed = c.BOT_TOKEN or "no-token"
    c.WEBHOOK_PATH = _env("WEBHOOK_PATH") or hashlib.sha256(("path:" + seed).encode()).hexdigest()[:24]
    c.WEBHOOK_HEADER_TOKEN = hashlib.sha256(("hdr:" + seed).encode()).hexdigest()[:32]
    c.USE_POLLING = _flag(_env("USE_POLLING"))
    c.PORT = int(_env("PORT", "10000") or 10000)
    c.DEFAULT_TZ = _env("DEFAULT_TZ", "+05:30")
    c.TICK = float(_env("TICK_SECONDS", "10") or 10)
    c.WORKERS = int(_env("WORKERS", "8") or 8)
    c.DENY_TEXT = _env("DENY_TEXT", "This is a private bot.")


load()

# ----------------------------------------------------------------------------------------------------------------------
# Telegram limits
# ----------------------------------------------------------------------------------------------------------------------
TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
ALBUM_MAX = 10
ROW_MAX = 8            # buttons in one row
BUTTON_MAX = 60        # buttons in one post (Telegram allows 100)
LABEL_MAX = 64
DIVIDER = "━━━━━━━━━━━━━━"
STYLES = (("", "Default"), ("primary", "Blue"), ("success", "Green"), ("danger", "Red"))
STYLE_DOT = {"": "", "primary": "🔵", "success": "🟢", "danger": "🔴"}
STYLE_SQ = {"": "⬜", "primary": "🟦", "success": "🟩", "danger": "🟥"}


# ----------------------------------------------------------------------------------------------------------------------
# Settings registry. One line = one real setting (the menus are built from this list).
#   scope "all"  = user default  ->  can be overridden per channel  ->  can be overridden per post (opt=True)
#   scope "user" = one value per user
# ----------------------------------------------------------------------------------------------------------------------
class S:
    __slots__ = ("key", "group", "label", "kind", "default", "choices", "scope", "hint", "opt")

    def __init__(self, key, group, label, kind, default, choices=None, scope="all", hint="", opt=False):
        self.key, self.group, self.label, self.kind = key, group, label, kind
        self.default, self.choices, self.scope, self.hint, self.opt = default, choices or [], scope, hint, opt

    def choice_label(self, value):
        for v, lab in self.choices:
            if v == value:
                return lab
        return str(value)

    def show(self, value):
        if self.kind == "bool":
            return "ON" if value else "OFF"
        if self.kind == "choice":
            return self.choice_label(value)
        s = str(value or "")
        return "-" if not s else (s[:18] + "…" if len(s) > 19 else s).replace("\n", " ")


def _tz_choices():
    mins = set(range(-12 * 60, 14 * 60 + 1, 30)) | {-9 * 60 - 30, 5 * 60 + 45, 8 * 60 + 45, 12 * 60 + 45, 13 * 60 + 45}
    out = []
    for m in sorted(mins):
        sign = "+" if m >= 0 else "-"
        a = abs(m)
        s = "%s%02d:%02d" % (sign, a // 60, a % 60)
        out.append((s, "UTC" + s + (" (India)" if s == "+05:30" else "")))
    return out


def tz_minutes(s, default=330):
    try:
        s = str(s).strip().replace("UTC", "")
        sign = -1 if s.startswith("-") else 1
        h, _, m = s.lstrip("+-").partition(":")
        return sign * (int(h) * 60 + int(m or 0))
    except Exception:
        return default


DELETE_CHOICES = [(0, "Never"), (900, "15 minutes"), (1800, "30 minutes"), (3600, "1 hour"), (10800, "3 hours"),
                  (21600, "6 hours"), (43200, "12 hours"), (86400, "24 hours"), (172800, "2 days"), (259200, "3 days"),
                  (604800, "7 days"), (1209600, "14 days"), (2592000, "30 days")]
STYLE_CHOICES = [(v, n) for v, n in STYLES]

GROUPS = [
    ("post", "📝", "Post defaults"),
    ("buttons", "🔘", "Buttons & votes"),
    ("behavior", "🧭", "Behavior"),
    ("time", "🕒", "Time & dates"),
    ("alerts", "🔔", "Alerts"),
    ("ui", "🖥", "Interface"),
]

SETTINGS = [
    # ---- post defaults ------------------------------------------------------------------------------------------
    S("preview", "post", "Link preview", "choice", "off",
      [("off", "Off"), ("on", "On"), ("large", "Large picture"), ("small", "Small picture"), ("above", "Above the text")],
      hint="How a link inside the text is shown.", opt=True),
    S("silent", "post", "Silent (no sound)", "bool", False, hint="Subscribers get the post without a notification sound.", opt=True),
    S("protect", "post", "Protect content", "bool", False, hint="Nobody can forward or save the post.", opt=True),
    S("spoiler", "post", "Hide media (spoiler)", "bool", False, hint="Photos and videos are blurred until tapped.", opt=True),
    S("caption_above", "post", "Caption above media", "bool", False, hint="Puts the text above the picture/video.", opt=True),
    S("pin", "post", "Pin after publishing", "choice", "off", [("off", "No"), ("on", "Yes"), ("silent", "Yes, silently")], opt=True),
    S("delete_after", "post", "Auto-delete after", "choice", 0, DELETE_CHOICES,
      hint="The post is removed from the channel automatically.", opt=True),
    S("header", "post", "Header text", "text", "", hint="Added above every post. Supports HTML tags and {variables}.", opt=False),
    S("footer", "post", "Footer text", "text", "", hint="Added below every post. Supports HTML tags and {variables}.", opt=False),
    S("hf_use", "post", "Add header and footer", "bool", True, hint="Turn off to skip header and footer for one post.", opt=True),
    S("hf_gap", "post", "Header/footer spacing", "choice", "blank",
      [("nl", "Next line"), ("blank", "Blank line"), ("line", "Divider line")]),
    S("def_buttons", "post", "Default buttons", "text", "",
      hint="Buttons added to every post. Use the quick format: Text - https://link", opt=False),
    S("def_use", "post", "Add default buttons", "bool", True, hint="Turn off to skip default buttons for one post.", opt=True),
    S("def_pos", "post", "Default buttons position", "choice", "bottom", [("bottom", "Below mine"), ("top", "Above mine")]),
    S("variables", "post", "Smart variables", "bool", True,
      hint="{date} {time} {day} {channel} {channel_username} {n} are replaced when publishing.", opt=True),
    S("emoji_mode", "post", "Premium emoji", "choice", "keep", [("keep", "Keep (plain if refused)"), ("plain", "Always plain")],
      hint="Telegram lets channel posts use Premium emoji only if the bot owns a Fragment username.", opt=True),
    S("album_text", "post", "Album button message", "text", "👇", hint="Albums cannot carry buttons, so they follow in a small message."),
    # ---- buttons -------------------------------------------------------------------------------------------------
    S("btn_style", "buttons", "New button color", "choice", "", STYLE_CHOICES, hint="Color given to every new button."),
    S("btn_dot", "buttons", "Color dot in label", "bool", False, hint="Adds 🔵🟢🔴 before the label (for old Telegram apps)."),
    S("btn_icons", "buttons", "Premium emoji icons", "bool", True, hint="Use the emoji icon of a button (needs a bot with Fragment username).", opt=True),
    S("btn_place", "buttons", "New button goes", "choice", "row", [("row", "In a new row"), ("same", "Next to the last one")],
      scope="user"),
    S("vote_counts", "buttons", "Show vote counts", "bool", True, opt=True),
    S("vote_toggle", "buttons", "Allow changing a vote", "bool", True, hint="Tapping again removes the vote.", opt=True),
    S("vote_multi", "buttons", "Allow several votes", "bool", False, hint="Off = one vote per person per post.", opt=True),
    S("vote_alert", "buttons", "Thank-you popup after voting", "bool", False, opt=True),
    # ---- behavior ------------------------------------------------------------------------------------------------
    S("confirm_publish", "behavior", "Confirm before publishing", "bool", True, scope="user"),
    S("keep_draft", "behavior", "Keep draft after publishing", "bool", False, scope="user"),
    S("media_autostart", "behavior", "Start a post when I send media", "bool", True, scope="user",
      hint="Send a photo/video/text to the bot and it becomes a new draft."),
    S("fwd_to_draft", "behavior", "Forwarded messages become drafts", "bool", True, scope="user"),
    S("ask_channels", "behavior", "Always ask for channels", "bool", False, scope="user"),
    S("clean_chat", "behavior", "Delete my messages after reading", "bool", False, scope="user"),
    # ---- time ----------------------------------------------------------------------------------------------------
    S("tz", "time", "Time zone", "choice", "+05:30", _tz_choices(), scope="user"),
    S("time12", "time", "12-hour clock", "bool", True, scope="user"),
    S("date_fmt", "time", "Date format", "choice", "dmy", [("dmy", "21 Nov 2026"), ("iso", "2026-11-21"), ("dmy2", "21/11/2026"), ("mdy", "11/21/2026")], scope="user"),
    # ---- alerts --------------------------------------------------------------------------------------------------
    S("notify_sched", "alerts", "Tell me when a scheduled post goes out", "bool", True, scope="user"),
    S("notify_errors", "alerts", "Tell me about errors", "bool", True, scope="user"),
    S("notify_delete", "alerts", "Tell me about auto-deleted posts", "bool", False, scope="user"),
    S("notify_added", "alerts", "Tell me when a channel is detected", "bool", True, scope="user"),
    # ---- interface -----------------------------------------------------------------------------------------------
    S("ui_colors", "ui", "Colored menu buttons", "bool", True, scope="user", hint="Needs an up-to-date Telegram app."),
    S("page_size", "ui", "Items per page", "choice", 6, [(4, "4"), (6, "6"), (8, "8"), (10, "10")], scope="user"),
    S("ui_hints", "ui", "Show tips in menus", "bool", True, scope="user"),
]

BY_KEY = {s.key: s for s in SETTINGS}
OPTION_KEYS = [s.key for s in SETTINGS if s.opt]


def effective(key, *layers):
    """First layer (post opts -> channel cfg -> user cfg) that has the key wins, else the default."""
    for layer in layers:
        if isinstance(layer, dict) and key in layer and layer[key] is not None:
            return layer[key]
    return BY_KEY[key].default


# ----------------------------------------------------------------------------------------------------------------------
# Help texts (HTML)
# ----------------------------------------------------------------------------------------------------------------------
VARIABLES = {
    "date": "today's date", "time": "current time", "datetime": "date and time", "day": "weekday name",
    "year": "year", "channel": "channel title", "channel_username": "@username of the channel", "n": "post number",
}
