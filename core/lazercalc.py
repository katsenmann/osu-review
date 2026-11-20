"""osu!lazer's own pp calculator - the same one tosu uses (tosuapp/lazer-calculator).

rosu-pp (our in-browser fallback) still has the pp formulas from October 2025; osu! changed them
on 2026-07-03 (new reading skill etc.). tosu switched to a prebuilt build of the official lazer
code, so to get *exactly* tosu's / osu!'s numbers we run that same build:

    vendor/lazercalc/node(.exe)          a portable Node.js (only if none is installed)
    vendor/lazercalc/pkg/binding.node    @tosuapp/lazer-calculator-<platform> from npm
    vendor/lazercalc/calc.js             small helper (written by us), one process per job

The binding keeps memory it never frees (tosuapp/lazer-calculator#25), so every calculation runs
in its own short-lived process. Supported: Windows x64 and Linux x64 (what the package ships).
"""
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import zipfile

from core.integrity import check_sha256, check_sri, from_shasums

NODE_INDEX = "https://nodejs.org/dist/index.json"
NPM_PKG = "https://registry.npmjs.org/@tosuapp%2Flazer-calculator-{plat}"
CHECK_EVERY = 3 * 24 * 3600          # look for a newer calculator every 3 days


def _get(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": "osu-review"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _platform():
    if sys.platform.startswith("win"):
        plat = "win32"
    elif sys.platform.startswith("linux"):
        plat = "linux"
    else:
        return None
    m = platform.machine().lower()
    if m not in ("amd64", "x86_64", "x64"):
        return None
    return plat + "-x64"


HELPER = r"""// osu!review - runs tosu's lazer pp calculator for one replay (input: JSON on stdin)
const path = require('path');
const L = require(path.join(__dirname, 'pkg', 'binding.node'));
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', d => input += d);
process.stdin.on('end', () => {
  let out;
  try { out = run(JSON.parse(input)); } catch (e) { out = { ok: false, error: String(e && e.stack || e) }; }
  process.stdout.write(JSON.stringify(out));
});

const FIELDS = ['totalScore','accuracy','maxCombo','sliderEndHits','comboBreaks','ignoreHits','ignoreMisses','largeBonuses',
  'smallBonuses','largeTickHits','largeTickMisses','smallTickHits','smallTickMisses','perfects','greats','goods','oks','mehs','misses'];

function toMods(list) {
  return (list || []).map(m => {
    const settings = new Map(Object.entries(m.settings || {}));
    let mod;
    try { mod = new L.LazerMod(); mod.acronym = m.acronym; mod.settings = settings; } catch (_) { mod = { acronym: m.acronym, settings }; }
    return mod;
  });
}

function makeScore(template, legacy, v) {
  const s = Object.assign({}, template);
  for (const f of FIELDS) s[f] = 0;
  s.isLegacyScore = !!legacy;
  for (const k in v) s[k] = v[k];
  return s;
}

function perf(bm, attrs, sc) {
  const r = bm.calculatePerformance(attrs, sc);
  return r && typeof r.pp === 'number' ? r.pp : NaN;
}

function run(job) {
  const t0 = Date.now();
  const bm = L.PlayBeatmap.parse(job.osu);
  bm.applyMods(toMods(job.mods));
  const tmpl = bm.createScore(1);
  const legacy = !!job.legacy;

  // whole map
  const full = bm.createGradualDifficulty();
  full.skipToEnd();
  const attrs = full.createDifficultyAttrs();
  const d = attrs.getData();

  const fin = makeScore(tmpl, legacy, job.final);
  fin.accuracy = bm.calculateAccuracy(fin);
  const pp = perf(bm, attrs, fin);

  // full combo: misses become 300s, max combo
  const fcv = Object.assign({}, job.final, { greats: (job.final.greats || 0) + (job.final.misses || 0), misses: 0, maxCombo: d.maxCombo, totalScore: 0 });
  if (job.final.largeTickMisses) { fcv.largeTickHits = (job.final.largeTickHits || 0) + job.final.largeTickMisses; fcv.largeTickMisses = 0; }
  const fc = makeScore(tmpl, false, fcv); fc.accuracy = bm.calculateAccuracy(fc);
  const ppFc = perf(bm, attrs, fc);

  // SS
  let ss = bm.createScore(1);
  if (!(ss.greats > 0) || (ss.accuracy > 0 && ss.accuracy < 0.99)) ss = bm.createScore(100);
  const ppSs = perf(bm, attrs, ss);

  // live course: difficulty of the first n objects + hit counts after the n-th judgement
  const grad = [];
  if (job.steps && job.steps.length) {
    const g = bm.createGradualDifficulty();
    const every = Math.max(1, Math.ceil(job.steps.length / 2500));
    let last = 0;
    for (let i = 0; i < job.steps.length; i++) {
      if (!g.advance()) { grad.push(last); continue; }
      if (i % every && i !== job.steps.length - 1) { grad.push(null); continue; }
      const st = job.steps[i];
      const sc = makeScore(tmpl, false, st);
      try { sc.accuracy = g.calculateProgressiveAccuracy(sc); } catch (_) { sc.accuracy = 1; }
      const at = g.createDifficultyAttrs();
      const v = perf(bm, at, sc);
      last = isFinite(v) ? v : last;
      grad.push(last);
    }
    // fill skipped points
    for (let i = 0; i < grad.length; i++) if (grad[i] === null) {
      let j = i; while (j < grad.length && grad[j] === null) j++;
      const a = i ? grad[i - 1] : 0, b = j < grad.length ? grad[j] : a;
      for (let k = i; k < j; k++) grad[k] = a + (b - a) * (k - i + 1) / (j - i + 1);
      i = j;
    }
  }
  return { ok: true, pp, fc: ppFc, ss: ppSs, stars: d.stars, maxCombo: d.maxCombo, aim: d.aim, speed: d.speed,
           reading: d.reading, grad, ms: Date.now() - t0 };
}
"""


class LazerCalc:
    def __init__(self, data_dir, log=print):
        self.dir = os.path.join(data_dir, "vendor", "lazercalc")
        self.meta_file = os.path.join(self.dir, "meta.json")
        self.log = log
        self.plat = _platform()
        self.lock = threading.Lock()
        self.run_lock = threading.Semaphore(2)
        self.error = None
        self.last_try = 0
        try:
            self.meta = json.load(open(self.meta_file, encoding="utf-8"))
        except Exception:
            self.meta = {}
        if not self.plat:
            self.state = "unsupported"
        else:
            self.state = "ready" if self.installed() else "missing"

    # ---------------------------------------------------------------- install
    def node_path(self):
        own = os.path.join(self.dir, "node.exe" if os.name == "nt" else "node")
        if os.path.isfile(own):
            return own
        sysnode = shutil.which("node")
        if not sysnode:
            return None
        # ask the system node for its version only once (each check would flash a console window)
        cache = getattr(self, "_sysnode", None)
        if cache and cache[0] == sysnode:
            return cache[1]
        ok = None
        try:
            v = subprocess.run([sysnode, "-v"], capture_output=True, text=True, timeout=10,
                               creationflags=0x08000000 if os.name == "nt" else 0).stdout.strip()
            if int(v.lstrip("v").split(".")[0]) >= 20:
                ok = sysnode
        except Exception:
            pass
        self._sysnode = (sysnode, ok)
        return ok

    def installed(self):
        return bool(self.node_path()) and os.path.isfile(os.path.join(self.dir, "pkg", "binding.node"))

    def version(self):
        return self.meta.get("version")

    def ensure_async(self, force_check=False):
        if not self.plat or self.state == "downloading":
            return
        due = time.time() - self.meta.get("checked", 0) > CHECK_EVERY
        if self.installed() and not (due or force_check):
            return
        if not force_check and time.time() - self.last_try < 600:
            return                      # failed a moment ago (offline?) - try again later
        self.last_try = time.time()
        threading.Thread(target=self.install, daemon=True).start()

    def _install_node(self):
        if self.node_path():
            return
        idx = json.loads(_get(NODE_INDEX))
        want = "win-x64-zip" if self.plat.startswith("win") else "linux-x64"
        rel = next((r for r in idx if r.get("lts") and want in r.get("files", []) and int(r["version"].lstrip("v").split(".")[0]) >= 22), None)
        if not rel:
            raise RuntimeError("no suitable Node.js release found")
        v = rel["version"]
        if self.plat.startswith("win"):
            url = f"https://nodejs.org/dist/{v}/node-{v}-win-x64.zip"
            blob = _get(url, timeout=600)
            check_sha256(blob, from_shasums(_get(f"https://nodejs.org/dist/{v}/SHASUMS256.txt").decode(), f"node-{v}-win-x64.zip"))
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                name = next(n for n in z.namelist() if n.endswith("/node.exe"))
                data = z.read(name)
            dst = os.path.join(self.dir, "node.exe")
        else:
            url = f"https://nodejs.org/dist/{v}/node-{v}-linux-x64.tar.xz"
            blob = _get(url, timeout=600)
            check_sha256(blob, from_shasums(_get(f"https://nodejs.org/dist/{v}/SHASUMS256.txt").decode(), f"node-{v}-linux-x64.tar.xz"))
            with tarfile.open(fileobj=io.BytesIO(blob), mode="r:xz") as t:
                m = next(m for m in t.getmembers() if m.name.endswith("/bin/node"))
                data = t.extractfile(m).read()
            dst = os.path.join(self.dir, "node")
        with open(dst + ".part", "wb") as f:
            f.write(data)
        os.replace(dst + ".part", dst)
        if os.name != "nt":
            os.chmod(dst, 0o755)
        self.log("lazer pp: Node.js", v, "ready")

    def install(self):
        with self.lock:
            self.state, self.error = ("updating" if self.installed() else "downloading"), None
            try:
                os.makedirs(self.dir, exist_ok=True)
                self._install_node()
                meta = json.loads(_get(NPM_PKG.format(plat=self.plat)))
                latest = (meta.get("dist-tags") or {}).get("latest")
                info = (meta.get("versions") or {}).get(latest) or {}
                tarball = (info.get("dist") or {}).get("tarball")
                if not tarball:
                    raise RuntimeError("calculator package not found on npm")
                pkg = os.path.join(self.dir, "pkg")
                if latest != self.meta.get("version") or not os.path.isfile(os.path.join(pkg, "binding.node")):
                    blob = _get(tarball, timeout=900)
                    check_sri(blob, (info.get("dist") or {}).get("integrity"))
                    tmp = pkg + ".new"
                    shutil.rmtree(tmp, ignore_errors=True)
                    os.makedirs(tmp)
                    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as t:
                        for m in t.getmembers():
                            if not m.isfile():
                                continue
                            rel = m.name.split("/", 1)[1] if "/" in m.name else m.name
                            if ".." in rel.split("/"):
                                continue
                            out = os.path.join(tmp, *rel.split("/"))
                            os.makedirs(os.path.dirname(out), exist_ok=True)
                            with open(out, "wb") as f:
                                f.write(t.extractfile(m).read())
                    if not os.path.isfile(os.path.join(tmp, "binding.node")):
                        raise RuntimeError("binding.node missing in the package")
                    old = pkg + ".old"
                    shutil.rmtree(old, ignore_errors=True)
                    if os.path.isdir(pkg):
                        os.replace(pkg, old)
                    os.replace(tmp, pkg)
                    shutil.rmtree(old, ignore_errors=True)
                    self.log("lazer pp calculator installed:", latest)
                with open(os.path.join(self.dir, "calc.js"), "w", encoding="utf-8") as f:
                    f.write(HELPER)
                self.meta = {"version": latest, "checked": time.time()}
                json.dump(self.meta, open(self.meta_file, "w", encoding="utf-8"))
                self.state = "ready"
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
                self.state = "ready" if self.installed() else "error"
                self.log("lazer pp calculator:", self.error)

    # ---------------------------------------------------------------- calculate
    def calculate(self, job, timeout=180):
        """job: {osu: text, mods: [{acronym, settings}], legacy: bool, final: {...}, steps: [{...}]}"""
        if not self.installed():
            raise RuntimeError("lazer calculator not installed")
        helper = os.path.join(self.dir, "calc.js")
        if not os.path.isfile(helper) or open(helper, encoding="utf-8").read() != HELPER:
            with open(helper, "w", encoding="utf-8") as f:
                f.write(HELPER)
        flags = 0x08000000 if os.name == "nt" else 0       # CREATE_NO_WINDOW
        with self.run_lock:
            pr = subprocess.run([self.node_path(), helper], input=json.dumps(job), capture_output=True, text=True,
                                timeout=timeout, cwd=self.dir, creationflags=flags, encoding="utf-8")
        out = (pr.stdout or "").strip()
        try:
            res = json.loads(out)
        except Exception:
            raise RuntimeError(f"calculator crashed ({pr.returncode}): {(pr.stderr or out)[-400:]}")
        if not res.get("ok"):
            raise RuntimeError(res.get("error", "unknown error")[-600:])
        res["version"] = self.version()
        return res


# ---------------------------------------------------------------- building a job from a play
LEGACY_MODS = [(1, "NF"), (2, "EZ"), (4, "TD"), (8, "HD"), (16, "HR"), (32, "SD"), (64, "DT"), (128, "RX"), (256, "HT"),
               (512, "NC"), (1024, "FL"), (2048, "AT"), (4096, "SO"), (8192, "AP"), (16384, "PF")]


def lazer_mods(rep):
    info = rep.get("lazer_info") or {}
    if rep.get("is_lazer") and info.get("mods") is not None:
        return [{"acronym": m.get("acronym"), "settings": m.get("settings") or {}} for m in info["mods"] if m.get("acronym")]
    bits = rep.get("mods") or 0
    out = []
    for b, ac in LEGACY_MODS:
        if bits & b:
            if ac == "DT" and bits & 512:
                continue        # NC includes DT
            if ac == "SD" and bits & 16384:
                continue        # PF includes SD
            out.append({"acronym": ac, "settings": {}})
    if not rep.get("is_lazer"):
        out.append({"acronym": "CL", "settings": {}})   # stable scores are "classic" scores in lazer
    return out


def build_job(play, osu_text):
    """Hit counts after every judged object (same order tosu's live counter sees them)."""
    rep, a = play["replay"], play.get("analysis") or {}
    legacy = not rep.get("is_lazer")
    stats = ((rep.get("lazer_info") or {}).get("statistics")) or {}
    final = {"greats": rep["n300"], "oks": rep["n100"], "mehs": rep["n50"], "misses": rep["nmiss"], "maxCombo": rep["max_combo"]}
    if legacy:
        final["totalScore"] = rep.get("score") or 0
    else:
        objs = a.get("objects") or []
        sl = [o for o in objs if o.get("kind") == "slider"]
        if stats:
            final["sliderEndHits"] = stats.get("slider_tail_hit", 0)
            final["largeTickHits"] = stats.get("large_tick_hit", 0)
            final["largeTickMisses"] = stats.get("large_tick_miss", 0)
            final["smallTickHits"] = stats.get("small_tick_hit", 0)
            final["smallTickMisses"] = stats.get("small_tick_miss", 0)
        elif sl:   # estimate from the simulation: whole slider caught -> end + ticks hit
            final["sliderEndHits"] = sum(1 for o in sl if o.get("got", 0) >= o.get("parts", 0))
            final["largeTickHits"] = max(0, sum(o.get("got", 0) for o in sl) - final["sliderEndHits"])
            final["largeTickMisses"] = sum(max(0, o.get("parts", 0) - o.get("got", 0)) for o in sl)
    steps = []
    objs = a.get("objects") or []
    order = a.get("order") or [i for i, o in enumerate(objs) if o.get("res") and o.get("res") != "unplayed"]
    n300 = n100 = n50 = miss = combo = maxc = 0
    for i in order:
        o = objs[i]
        r = o.get("res")
        if r == "miss":
            miss += 1
            combo = 0
        elif r == "100":
            n100 += 1
            combo += 1
        elif r == "50":
            n50 += 1
            combo += 1
        else:
            n300 += 1
            combo += 1
        maxc = o["mc"] if o.get("mc") is not None else max(maxc, combo)
        steps.append({"greats": n300, "oks": n100, "mehs": n50, "misses": miss, "maxCombo": maxc})
    return {"osu": osu_text, "mods": lazer_mods(rep), "legacy": legacy, "final": final, "steps": steps}
