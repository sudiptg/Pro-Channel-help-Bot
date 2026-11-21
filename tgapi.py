"""Thin Telegram Bot API client: JSON calls, flood-wait handling, readable errors."""
import logging
import re
import time

import requests

import config

log = logging.getLogger("tg")

_SEND = ("send", "copy", "forward")


def classify(code, desc):
    d = (desc or "").lower()
    if code == 429 or "too many requests" in d:
        return "flood"
    if "message is not modified" in d:
        return "same"
    if ("message to edit not found" in d or "message to delete not found" in d or "message can't be edited" in d or "message_id_invalid" in d
            or "message can't be deleted" in d or "message identifier is not specified" in d):
        return "gone"
    if ("bot was kicked" in d or "bot is not a member" in d or "not enough rights" in d or "have no rights" in d or "administrator rights" in d
            or "chat_write_forbidden" in d or "chat_admin_required" in d or "forbidden" in d or code == 403):
        return "rights"
    if "chat not found" in d or "peer_id_invalid" in d or "channel_private" in d or "user not found" in d:
        return "nochat"
    if "can't parse entities" in d or "can't find end" in d or "unsupported start tag" in d:
        return "parse"
    if "caption is too long" in d or "message is too long" in d or "message_too_long" in d:
        return "toolong"
    if "emoji" in d or "document_invalid" in d or "icon" in d:
        return "emoji"
    if "button" in d or "wrong http url" in d or "url" in d and "invalid" in d:
        return "button"
    return "other"


FRIENDLY = {
    "rights": "The bot is not an admin of this channel, or it is missing a right (post, edit or delete messages).",
    "nochat": "Telegram cannot find this chat. Is the bot still a member?",
    "parse": "Telegram could not read the text formatting.",
    "button": "Telegram rejected one of the buttons. Check the links.",
    "toolong": "The text is too long for Telegram (4096 characters, or 1024 with a picture/video).",
    "gone": "The message no longer exists.",
    "same": "Nothing changed.",
    "flood": "Telegram asked the bot to slow down. Try again in a moment.",
    "network": "Could not reach Telegram. Try again.",
    "emoji": "Telegram refused a Premium emoji.",
    "other": "Telegram refused the request.",
}


class TgError(Exception):
    def __init__(self, method, code, desc, retry_after=0, network=False):
        super().__init__("%s failed: %s %s" % (method, code, desc))
        self.method, self.code, self.desc, self.retry_after = method, code, desc or "", retry_after
        self.kind = "network" if network else classify(code, desc)

    def friendly(self):
        base = FRIENDLY.get(self.kind, FRIENDLY["other"])
        if self.kind in ("other", "button", "parse", "emoji") and self.desc:
            base += " (%s)" % re.sub(r"^Bad Request:\s*", "", self.desc)[:120]
        return base


class Tg:
    def __init__(self, token=None, base=None):
        self.token = token if token is not None else config.Config.BOT_TOKEN
        self.base = (base or config.Config.API_BASE).rstrip("/")
        self.s = requests.Session()
        ad = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=32)
        self.s.mount("https://", ad)
        self.s.mount("http://", ad)
        self.me = {}

    def url(self, method):
        return "%s/bot%s/%s" % (self.base, self.token, method)

    def __call__(self, method, files=None, _timeout=35, _tries=3, **params):
        params = {k: v for k, v in params.items() if v is not None}
        is_send = method.lower().startswith(_SEND)
        last = None
        for attempt in range(_tries):
            try:
                if files:
                    import json as _json
                    data = {k: (_json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v) for k, v in params.items()}
                    r = self.s.post(self.url(method), data=data, files=files, timeout=_timeout + 30)
                else:
                    r = self.s.post(self.url(method), json=params, timeout=_timeout)
            except (requests.ConnectTimeout, requests.ConnectionError) as e:
                last = TgError(method, 0, str(e)[:100], network=True)
                time.sleep(0.6 * (attempt + 1))
                continue
            except requests.RequestException as e:
                last = TgError(method, 0, str(e)[:100], network=True)
                if is_send:
                    raise last
                time.sleep(0.6 * (attempt + 1))
                continue
            try:
                data = r.json()
            except ValueError:
                data = {"ok": False, "error_code": r.status_code, "description": r.text[:120]}
            if data.get("ok"):
                return data.get("result")
            code, desc = data.get("error_code", r.status_code), data.get("description", "")
            ra = int((data.get("parameters") or {}).get("retry_after", 0) or 0)
            err = TgError(method, code, desc, retry_after=ra)
            if err.kind == "flood" and attempt < _tries - 1 and ra <= 25:
                time.sleep(ra + 0.5)
                last = err
                continue
            if code >= 500 and attempt < _tries - 1 and not is_send:
                time.sleep(1.0 * (attempt + 1))
                last = err
                continue
            raise err
        raise last or TgError(method, 0, "unknown", network=True)

    def download(self, file_path):
        r = self.s.get("%s/file/bot%s/%s" % (self.base, self.token, file_path), timeout=60)
        r.raise_for_status()
        return r.content

    def ok(self, method, **params):
        """Like a call, but returns None instead of raising."""
        try:
            return self(method, **params)
        except TgError as e:
            log.info("%s ignored: %s", method, e)
            return None
