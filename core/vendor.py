"""Downloads the browser (WebAssembly) build of rosu-pp-js - the pp calculator used by tosu & co.

Sources tried in order: GitHub release asset with "web" in its name, then the npm tarball.
The web build is the wasm-bindgen "--target web" output: an ES module with `export default init`
plus a *_bg.wasm file. It's stored as vendor/rosu/rosu.js + rosu_bg.wasm.
"""
import io
import json
import os
import tarfile
import threading
import urllib.request
import zipfile

GH = "https://api.github.com/repos/MaxOhn/rosu-pp-js/releases/latest"
NPM = "https://registry.npmjs.org/rosu-pp-js/latest"


def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "osu-review", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _members(blob, name):
    """yields (path, bytes) from a tgz or zip"""
    if name.endswith(".zip") or blob[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            for n in z.namelist():
                if not n.endswith("/"):
                    yield n, z.read(n)
    else:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:*") as t:
            for m in t.getmembers():
                if m.isfile():
                    f = t.extractfile(m)
                    if f:
                        yield m.name, f.read()


def _pick_web_build(files):
    """files: dict path->bytes. Returns (js_bytes, wasm_bytes) of a --target web build or None."""
    wasms = {p: b for p, b in files.items() if p.endswith("_bg.wasm")}
    for wp, wb in wasms.items():
        js_path = wp[: -len("_bg.wasm")] + ".js"
        js = files.get(js_path)
        if not js:
            continue
        txt = js.decode("utf-8", "replace")
        if "export default" in txt and "require(" not in txt[:4000] and "import.meta" in txt:
            return js, wb
    return None


class RosuVendor:
    def __init__(self, base_dir, log=print):
        self.dir = os.path.join(base_dir, "vendor", "rosu")
        self.log = log
        self.state = "missing" if not self.installed() else "ready"
        self.error = None
        self.lock = threading.Lock()

    def installed(self):
        return os.path.isfile(os.path.join(self.dir, "rosu.js")) and os.path.isfile(os.path.join(self.dir, "rosu_bg.wasm"))

    def path(self, name):
        return os.path.join(self.dir, {"rosu.js": "rosu.js", "rosu_bg.wasm": "rosu_bg.wasm"}[name])

    def ensure_async(self):
        if self.installed() or self.state == "downloading":
            return
        threading.Thread(target=self.install, daemon=True).start()

    def install(self):
        with self.lock:
            if self.installed():
                self.state = "ready"
                return
            self.state, self.error = "downloading", None
            tried = []
            try:
                sources = []
                try:
                    rel = json.loads(_get(GH))
                    for a in rel.get("assets", []):
                        n = a.get("name", "").lower()
                        if "web" in n and (n.endswith(".tgz") or n.endswith(".tar.gz") or n.endswith(".zip")):
                            sources.append((f'github {rel.get("tag_name")} {a["name"]}', a["browser_download_url"], n))
                except Exception as e:
                    tried.append(f"github: {e}")
                try:
                    meta = json.loads(_get(NPM))
                    sources.append((f'npm {meta.get("version")}', meta["dist"]["tarball"], "x.tgz"))
                except Exception as e:
                    tried.append(f"npm: {e}")
                for label, url, name in sources:
                    try:
                        blob = _get(url, timeout=120)
                        files = dict(_members(blob, name))
                        got = _pick_web_build(files)
                        if not got:
                            tried.append(f"{label}: no web build included")
                            continue
                        os.makedirs(self.dir, exist_ok=True)
                        open(os.path.join(self.dir, "rosu.js"), "wb").write(got[0])
                        open(os.path.join(self.dir, "rosu_bg.wasm"), "wb").write(got[1])
                        open(os.path.join(self.dir, "source.txt"), "w", encoding="utf-8").write(label)
                        self.state = "ready"
                        self.log("rosu-pp installed:", label)
                        return
                    except Exception as e:
                        tried.append(f"{label}: {e}")
                raise RuntimeError(" | ".join(tried) or "no source found")
            except Exception as e:
                self.state, self.error = "error", str(e)[:500]
                self.log("rosu-pp installation failed:", self.error)
