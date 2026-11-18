"""Bridge to tosu (https://tosu.app) - if it runs next to osu!, we take its pp.

tosu calculates pp with osu!lazer's own calculator (newer than rosu-pp, includes the
latest pp updates). While you play we read its /json/v2 a few times per second and keep:
    - the live pp course (map time -> pp), exactly what tosu's overlay showed
    - the pp from the results screen
and later match that to the replay (same map + same hit counts + same combo).
"""
import json
import os
import threading
import time
import urllib.request

PLAY, RESULTS = 2, 7


class TosuBridge:
    def __init__(self, data_dir, log, url="http://127.0.0.1:24050"):
        self.log = log
        self.url = url.rstrip("/")
        self.file = os.path.join(data_dir, "tosu_records.json")
        self.lock = threading.Lock()
        self.connected = False
        self.client = None
        self.session = None          # current play: {md5, curve: [[t, pp]], ...}
        self.records = []            # finished plays (results screen seen)
        self.on_fail = None          # callback(info) once per failed play (health ran out, osu!stable)
        try:
            self.records = json.load(open(self.file, encoding="utf-8"))[-40:]
        except Exception:
            self.records = []
        threading.Thread(target=self._loop, daemon=True).start()

    def set_url(self, url):
        self.url = (url or "http://127.0.0.1:24050").rstrip("/")

    def status(self):
        return {"connected": self.connected, "client": self.client, "url": self.url,
                "playing": bool(self.session and time.time() - self.session["seen"] < 2)}

    # ---------------------------------------------------------------- polling
    def _get(self):
        with urllib.request.urlopen(self.url + "/json/v2", timeout=1.5) as r:
            return json.loads(r.read())

    def _loop(self):
        was = None
        while True:
            try:
                d = self._get()
                if not self.connected:
                    self.log("tosu connected – pp now come from tosu (osu!lazer calculator)")
                self.connected = True
                was = "ok"
                st = int(((d.get("state") or {}).get("number")) or 0)
                self.client = d.get("client")
                self._handle(st, d)
                time.sleep(0.2 if st == PLAY else 0.6)
            except Exception as e:
                if was != "off" and self.connected:
                    self.log(f"tosu no longer reachable ({type(e).__name__})")
                self.connected = False
                was = "off"
                time.sleep(5)

    def _handle(self, st, d):
        bm = d.get("beatmap") or {}
        md5 = (bm.get("checksum") or "").lower()
        if st == PLAY:
            play = d.get("play") or {}
            t = float(((bm.get("time") or {}).get("live")) or 0)
            pp = float(((play.get("pp") or {}).get("current")) or 0)
            s = self.session
            if not s or s["md5"] != md5 or t < s["last_t"] - 1500:     # new play or retry
                s = self.session = {"md5": md5, "curve": [], "last_t": t, "seen": time.time(),
                                    "player": play.get("playerName"), "done": False}
            s["seen"] = time.time()
            self._watch_fail(s, play, t)
            if t != s["last_t"] or not s["curve"]:
                if not s["curve"] or abs(pp - s["curve"][-1][1]) > 1e-6 or t - s["curve"][-1][0] > 2000:
                    s["curve"].append([round(t), round(pp, 3)])
                s["last_t"] = t
        elif st == RESULTS:
            rs = d.get("resultsScreen") or {}
            h = rs.get("hits") or {}
            if not md5 or not rs.get("maxCombo") and not h.get("300"):
                return
            rec = {"md5": md5, "player": rs.get("playerName"), "score": rs.get("score"),
                   "n300": h.get("300"), "n100": h.get("100"), "n50": h.get("50"), "nmiss": h.get("0"),
                   "max_combo": rs.get("maxCombo"), "pp": (rs.get("pp") or {}).get("current"),
                   "fc": (rs.get("pp") or {}).get("fc"), "client": self.client, "at": time.time(),
                   "score_id": rs.get("scoreId")}
            key = (rec["md5"], rec["score"], rec["n300"], rec["n100"], rec["n50"], rec["nmiss"], rec["max_combo"])
            with self.lock:
                for r in self.records:
                    if (r["md5"], r["score"], r["n300"], r["n100"], r["n50"], r["nmiss"], r["max_combo"]) == key:
                        if rec["pp"] and not r.get("pp"):
                            r["pp"], r["fc"] = rec["pp"], rec["fc"]
                        return
                s = self.session
                if s and s["md5"] == md5 and not s["done"]:
                    rec["curve"] = s["curve"]
                    s["done"] = True
                self.records = (self.records + [rec])[-40:]
                try:
                    tmp = self.file + ".tmp"
                    json.dump(self.records, open(tmp, "w", encoding="utf-8"))
                    os.replace(tmp, self.file)
                except OSError:
                    pass

    def _watch_fail(self, s, play, t):
        """Health at 0 for a moment during a play (no NoFail) = the game over screen is up."""
        hb = play.get("healthBar") or {}
        hp = hb.get("normal")
        if hp is None:
            return
        try:
            hp = float(hp)
        except (TypeError, ValueError):
            return
        if hp > 0.5:
            s["alive"] = True
            s.pop("hp0", None)
            return
        mods = str(((play.get("mods") or {}).get("name")) or "")
        if not s.get("alive") or s.get("fail_sent") or "NF" in mods or "AT" in mods or "RX" in mods or "AP" in mods:
            return
        s.setdefault("hp0", time.time())
        if time.time() - s["hp0"] >= 1.8:          # fail animation has started, game over menu is coming up
            s["fail_sent"] = True
            info = {"md5": s["md5"], "time": t, "client": self.client, "mods": mods}
            if self.on_fail:
                threading.Thread(target=self.on_fail, args=(info,), daemon=True).start()

    # ---------------------------------------------------------------- lookup
    def find(self, rep):
        want = (str(rep.get("beatmap_md5") or "").lower(), rep.get("n300"), rep.get("n100"), rep.get("n50"),
                rep.get("nmiss"), rep.get("max_combo"))
        with self.lock:
            for r in reversed(self.records):
                if (r["md5"], r["n300"], r["n100"], r["n50"], r["nmiss"], r["max_combo"]) == want:
                    if r.get("score") and rep.get("score") and int(r["score"]) != int(rep["score"]):
                        continue
                    if r.get("pp") is None:
                        continue
                    return r
        return None
