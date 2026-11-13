"""osu! API v2 (client credentials) - only used to read the official pp of a submitted score."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://osu.ppy.sh/api/v2"
UA = "osu!review (desktop replay analyzer)"


class OsuApi:
    def __init__(self):
        self.cid = self.secret = ""
        self.token, self.exp = None, 0
        self.users = {}

    def configure(self, cid, secret):
        cid, secret = str(cid or "").strip(), str(secret or "").strip()
        if (cid, secret) != (self.cid, self.secret):
            self.cid, self.secret, self.token, self.exp = cid, secret, None, 0

    @property
    def ready(self):
        return bool(self.cid and self.secret)

    def _token(self):
        if self.token and time.time() < self.exp - 60:
            return self.token
        body = urllib.parse.urlencode({"client_id": self.cid, "client_secret": self.secret,
                                       "grant_type": "client_credentials", "scope": "public"}).encode()
        req = urllib.request.Request("https://osu.ppy.sh/oauth/token", data=body, method="POST",
                                     headers={"Accept": "application/json", "User-Agent": UA,
                                              "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise PermissionError("wrong client ID or secret") if e.code in (400, 401) else e
        self.token, self.exp = d["access_token"], time.time() + int(d.get("expires_in", 3600))
        return self.token

    def get(self, path, **params):
        url = API + path + (("?" + urllib.parse.urlencode(params)) if params else "")
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + self._token(), "Accept": "application/json",
                                                   "User-Agent": UA, "x-api-version": "20240529"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def test(self):
        self._token()
        return True

    def user_id(self, name):
        k = name.lower()
        if k not in self.users:
            u = self.get(f"/users/{urllib.parse.quote('@' + name)}/osu", key="username")
            self.users[k] = u["id"] if u else None
        return self.users[k]

    def score_pp(self, rep, beatmap_id=None):
        """Official pp of the score in this replay. -> {"status": found|unranked|not_found, "pp", "rank", ...}"""
        sid = int(rep.get("online_id") or 0)
        if sid > 0:
            # the replay knows its score id on the osu! servers -> exact
            s = self.get(f"/scores/{sid}") if rep.get("is_lazer") else self.get(f"/scores/osu/{sid}")
            if s:
                if s.get("pp") is None:
                    return {"status": "unranked", "score_id": sid, "map_status": (s.get("beatmap") or {}).get("status")}
                return {"status": "found", "pp": float(s["pp"]), "score_id": s.get("id", sid),
                        "rank_global": s.get("rank_global"), "map_status": (s.get("beatmap") or {}).get("status")}
        bm = None
        if not beatmap_id:
            bm = self.get("/beatmaps/lookup", checksum=rep["beatmap_md5"])
            if not bm:
                return {"status": "not_found", "why": "map"}
            beatmap_id = bm["id"]
        uid = self.user_id(rep.get("player") or "")
        if not uid:
            return {"status": "not_found", "why": "user"}
        d = self.get(f"/beatmaps/{beatmap_id}/scores/users/{uid}/all", ruleset="osu") or {}
        scores = d.get("scores") or []
        want_score = int(rep.get("score") or 0)
        want = (rep.get("n300"), rep.get("n100"), rep.get("n50"), rep.get("nmiss"))
        best = None
        for s in scores:
            st = s.get("statistics") or {}
            counts = (st.get("great", 0), st.get("ok", 0), st.get("meh", 0), st.get("miss", 0))
            same_score = want_score and want_score in (s.get("legacy_total_score"), s.get("total_score"), s.get("score"))
            if counts == want and s.get("max_combo") == rep.get("max_combo") and (same_score or not want_score):
                best = s
                break
            if counts == want and s.get("max_combo") == rep.get("max_combo") and best is None:
                best = s
        status = (bm or {}).get("status")
        if not best:
            return {"status": "not_found", "why": "score", "on_server": len(scores), "map_status": status}
        if best.get("pp") is None:
            return {"status": "unranked", "map_status": status or (best.get("beatmap") or {}).get("status")}
        return {"status": "found", "pp": float(best["pp"]), "score_id": best.get("id"),
                "rank_global": best.get("rank_global"), "map_status": status}
