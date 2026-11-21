"""Storage with one small API for two back ends:
   MongoDB     when MONGO_URI is set (data survives restarts)
   local file  otherwise (on Render's free plan the file disappears at every restart)
Collections: users, channels, drafts, posts, live (vote counters), sched, templates, meta, counters.
"""
import copy
import json
import logging
import os
import threading
import time

import config

log = logging.getLogger("store")
COLLECTIONS = ["users", "channels", "drafts", "posts", "live", "sched", "templates", "meta", "counters"]
_OPS = {"$lte": lambda a, b: a <= b, "$lt": lambda a, b: a < b, "$gte": lambda a, b: a >= b, "$gt": lambda a, b: a > b}


def _match(doc, flt):
    for k, want in (flt or {}).items():
        have = doc.get(k)
        if isinstance(want, dict) and any(str(o).startswith("$") for o in want):
            for op, val in want.items():
                if op == "$in":
                    ok = (any(h in val for h in have) if isinstance(have, list) else have in val)
                elif op == "$ne":
                    ok = have != val
                else:
                    try:
                        ok = have is not None and _OPS[op](have, val)
                    except TypeError:
                        ok = False
                if not ok:
                    return False
        elif isinstance(have, list) and not isinstance(want, list):
            if want not in have:
                return False
        elif have != want:
            return False
    return True


class LocalStore:
    kind = "file"

    def __init__(self, path=None):
        self.path = path or config.Config.DATA_FILE
        self.lock = threading.RLock()
        self.d = {}
        self.error = ""
        self.dirty = False
        self._load()
        threading.Thread(target=self._saver, daemon=True, name="store-saver").start()

    def _load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, encoding="utf-8") as f:
                    self.d = json.load(f)
        except Exception as e:
            self.error = "Could not read %s: %s" % (self.path, e)
            log.error(self.error)
            self.d = {}

    def _saver(self):
        while True:
            time.sleep(2.0)
            try:
                self.flush()
            except Exception as e:
                log.error("save failed: %s", e)

    def flush(self):
        with self.lock:
            if not self.dirty:
                return
            data = json.dumps(self.d, ensure_ascii=False)
            self.dirty = False
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, self.path)

    def _c(self, coll):
        return self.d.setdefault(coll, {})

    def get(self, coll, _id):
        with self.lock:
            doc = self._c(coll).get(str(_id))
            return copy.deepcopy(doc) if doc is not None else None

    def put(self, coll, doc):
        with self.lock:
            self._c(coll)[str(doc["_id"])] = copy.deepcopy(doc)
            self.dirty = True

    def set_fields(self, coll, _id, fields):
        with self.lock:
            cur = self._c(coll).setdefault(str(_id), {"_id": _id})
            cur.update(copy.deepcopy(fields))
            self.dirty = True

    def delete(self, coll, _id):
        with self.lock:
            if self._c(coll).pop(str(_id), None) is not None:
                self.dirty = True

    def find(self, coll, flt=None, sort=None, limit=0):
        with self.lock:
            rows = [copy.deepcopy(x) for x in self._c(coll).values() if _match(x, flt)]
        for field, direction in reversed(sort or []):
            rows.sort(key=lambda x: (x.get(field) is None, x.get(field) if x.get(field) is not None else 0), reverse=direction < 0)
        return rows[:limit] if limit else rows

    def count(self, coll, flt=None):
        with self.lock:
            return sum(1 for x in self._c(coll).values() if _match(x, flt))

    def incr(self, coll, _id, field, by=1):
        with self.lock:
            cur = self._c(coll).setdefault(str(_id), {"_id": _id})
            cur[field] = int(cur.get(field, 0)) + by
            self.dirty = True
            return cur[field]

    def index(self, coll, *fields):
        pass

    def dump(self):
        with self.lock:
            return {c: [copy.deepcopy(x) for x in self.d.get(c, {}).values()] for c in COLLECTIONS}

    def restore(self, data):
        with self.lock:
            for c in COLLECTIONS:
                for doc in data.get(c, []):
                    if isinstance(doc, dict) and "_id" in doc:
                        self._c(c)[str(doc["_id"])] = copy.deepcopy(doc)
            self.dirty = True


class MongoStore:
    kind = "mongo"
    error = ""

    def __init__(self, uri, dbname):
        import pymongo
        from pymongo import ReturnDocument
        self._after = ReturnDocument.AFTER
        kw = dict(serverSelectionTimeoutMS=8000, connectTimeoutMS=8000, socketTimeoutMS=25000, retryWrites=True)
        try:
            import certifi
            kw["tlsCAFile"] = certifi.where()
        except Exception:
            pass
        self.cli = pymongo.MongoClient(uri, **kw)
        self.cli.admin.command("ping")
        self.db = self.cli[dbname]

    def get(self, coll, _id):
        return self.db[coll].find_one({"_id": _id})

    def put(self, coll, doc):
        self.db[coll].replace_one({"_id": doc["_id"]}, doc, upsert=True)

    def set_fields(self, coll, _id, fields):
        self.db[coll].update_one({"_id": _id}, {"$set": fields}, upsert=True)

    def delete(self, coll, _id):
        self.db[coll].delete_one({"_id": _id})

    def find(self, coll, flt=None, sort=None, limit=0):
        cur = self.db[coll].find(flt or {})
        if sort:
            cur = cur.sort([(f, d) for f, d in sort])
        if limit:
            cur = cur.limit(limit)
        return list(cur)

    def count(self, coll, flt=None):
        return self.db[coll].count_documents(flt or {})

    def incr(self, coll, _id, field, by=1):
        doc = self.db[coll].find_one_and_update({"_id": _id}, {"$inc": {field: by}}, upsert=True, return_document=self._after)
        return doc[field]

    def index(self, coll, *fields):
        try:
            self.db[coll].create_index([(f, 1) for f in fields])
        except Exception as e:
            log.warning("index %s failed: %s", coll, e)

    def flush(self):
        pass

    def dump(self):
        return {c: list(self.db[c].find({})) for c in COLLECTIONS}

    def restore(self, data):
        for c in COLLECTIONS:
            for doc in data.get(c, []):
                if isinstance(doc, dict) and "_id" in doc:
                    self.put(c, doc)


def open_store():
    st = None
    if config.Config.MONGO_URI:
        try:
            st = MongoStore(config.Config.MONGO_URI, config.Config.DB_NAME)
            log.info("MongoDB connected (%s)", config.Config.DB_NAME)
        except Exception as e:
            log.error("MongoDB failed, using a local file instead: %s", e)
            st = LocalStore()
            st.error = "MongoDB is not reachable (%s). Data is kept in a local file and will be lost on restart." % str(e)[:120]
    else:
        st = LocalStore()
        st.error = "MONGO_URI is not set. Data is kept in a local file and will be lost when Render restarts."
    st.index("posts", "uid", "at")
    st.index("posts", "del")
    st.index("sched", "when")
    st.index("live", "dirty")
    st.index("drafts", "uid")
    st.index("channels", "owners")
    return st
