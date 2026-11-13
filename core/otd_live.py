"""Live connection to the OpenTabletDriver daemon.

The OTD daemon (0.6.x) talks JSON-RPC (StreamJsonRpc, "Content-Length" framed) over a named pipe:
    Windows: \\\\.\\pipe\\OpenTabletDriver.Daemon
    Linux/macOS: unix socket $TMPDIR/CoreFxPipe_OpenTabletDriver.Daemon
The same channel the OTD GUI and its tablet debugger use. We use:
    GetApplicationInfo / GetTablets / GetSettings   -> version, tablet + digitizer size, live area
    SetTabletDebug(true) + "DeviceReport" events    -> every pen report (report rate in Hz, position, pressure)

Debug reports are only switched on while the live view is open (or if the user enables
"measure while playing"), and switched off again afterwards.
"""
import collections
import json
import os
import socket
import threading
import time

PIPE_WIN = r"\\.\pipe\OpenTabletDriver.Daemon"
PIPE_NAME = "CoreFxPipe_OpenTabletDriver.Daemon"


def _unix_pipe():
    """.NET puts named pipes into the temp folder on Linux/macOS ($TMPDIR, macOS: /var/folders/...)."""
    import tempfile
    for d in (os.environ.get("TMPDIR"), tempfile.gettempdir(), "/tmp", os.environ.get("XDG_RUNTIME_DIR")):
        if d and os.path.exists(os.path.join(d, PIPE_NAME)):
            return os.path.join(d, PIPE_NAME)
    return os.path.join(os.environ.get("TMPDIR") or "/tmp", PIPE_NAME)


class _Conn:
    def __init__(self):
        if os.name == "nt":
            self.f = open(PIPE_WIN, "r+b", buffering=0)
            self.sock = None
        else:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.connect(_unix_pipe())
            self.f = self.sock.makefile("rwb", buffering=0)
        self.next_id = 1
        self.buf = b""

    def close(self):
        for c in (self.f, self.sock):
            try:
                if c:
                    c.close()
            except Exception:
                pass

    def send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method, "params": params or []}
        mid = None
        if not notify:
            mid = self.next_id
            self.next_id += 1
            msg["id"] = mid
        body = json.dumps(msg).encode("utf-8")
        self.f.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        return mid

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.f.read(65536)
            if not chunk:
                raise ConnectionError("pipe closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def read_msg(self):
        while b"\r\n\r\n" not in self.buf:
            chunk = self.f.read(65536)
            if not chunk:
                raise ConnectionError("pipe closed")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        length = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1])
        return json.loads(self._read(length).decode("utf-8", "replace"))

    def call(self, method, params=None):
        mid = self.send(method, params)
        while True:
            m = self.read_msg()
            if m.get("id") == mid:
                if "error" in m:
                    raise RuntimeError(str(m["error"]))
                return m.get("result")


def _with_timeout(fn, timeout=3.0):
    box = {}

    def run():
        try:
            box["v"] = fn()
        except Exception as e:  # noqa
            box["e"] = e

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        raise TimeoutError("OTD is not responding")
    if "e" in box:
        raise box["e"]
    return box.get("v")


def _ci(d, *keys):
    """case-insensitive nested get"""
    for k in keys:
        if not isinstance(d, dict):
            return None
        hit = None
        for kk, v in d.items():
            if kk.lower() == k.lower():
                hit = v
                break
        d = hit
    return d


def parse_tablet(ref):
    props = _ci(ref, "Properties") or ref
    name = _ci(props, "Name")
    dig = _ci(props, "Specifications", "Digitizer") or {}
    out = {"name": name}
    for k in ("Width", "Height", "MaxX", "MaxY"):
        v = _ci(dig, k)
        if v is not None:
            out[k.lower()] = float(v)
    mp = _ci(props, "Specifications", "Pen", "MaxPressure")
    if mp:
        out["maxpressure"] = float(mp)
    return out


class OTDLive:
    def __init__(self, log=print):
        self.log = log
        self.lock = threading.Lock()
        self.status = {"connected": False, "error": None, "version": None, "tablets": [],
                       "settings": None, "ts": 0}
        self.raw = {}
        # live stream
        self.want_until = 0.0          # stream wanted until this time (UI heartbeat)
        self.always = False            # "measure while playing"
        self.streaming = False
        self.stream_conn = None
        self.times = collections.deque(maxlen=4000)
        self.last = None               # last tablet report
        self.hz_hist = collections.deque(maxlen=600)   # (time, hz) once per 0.5s
        self.reports = 0
        threading.Thread(target=self._poll_loop, daemon=True).start()
        threading.Thread(target=self._stream_manager, daemon=True).start()

    # ---------------------------------------------------------- settings / tablets
    def _poll_once(self):
        def job():
            c = _Conn()
            try:
                info = None
                try:
                    info = c.call("GetApplicationInfo")
                except Exception:
                    pass
                tablets = c.call("GetTablets") or []
                settings = c.call("GetSettings")
                return info, tablets, settings
            finally:
                c.close()
        info, tablets, settings = _with_timeout(job, 4.0)
        with self.lock:
            self.raw = {"info": info, "tablets": tablets, "settings": settings}
            ver = _ci(info, "Version") if isinstance(info, dict) else None
            self.status.update({"connected": True, "error": None, "version": ver,
                                "tablets": [parse_tablet(t) for t in tablets if isinstance(t, dict)],
                                "settings": settings, "ts": time.time()})

    def _poll_loop(self):
        last = None   # last logged state
        while True:
            try:
                self._poll_once()
                dp = getattr(self, "dump_path", None)
                if dp and not os.path.exists(dp):
                    try:
                        os.makedirs(os.path.dirname(dp), exist_ok=True)
                        json.dump(self.raw, open(dp, "w", encoding="utf-8"), indent=1, default=str)
                        self.log("OTD raw data saved")
                    except Exception as de:
                        self.log("Could not save OTD raw data:", repr(de))
                if last != "ok":
                    st = self.status
                    self.log("OTD connected:", st.get("version"), [t.get("name") for t in st.get("tablets", [])])
                    last = "ok"
            except BaseException as e:
                msg = f"{type(e).__name__}: {e}"
                with self.lock:
                    self.status.update({"connected": False, "error": msg})
                if last != msg:
                    self.log("OTD not reachable:", msg)
                    last = msg
            time.sleep(3.0)

    def settings(self):
        with self.lock:
            if self.status["connected"] and time.time() - self.status["ts"] < 15:
                return self.status["settings"]
        return None

    def connected_names(self):
        with self.lock:
            return [t.get("name") for t in self.status["tablets"] if t.get("name")]

    def digitizer(self, name=None):
        with self.lock:
            tabs = list(self.status["tablets"])
        for t in tabs:
            if t.get("width") and (not name or not t.get("name") or t["name"] == name):
                return t
        return None

    # ---------------------------------------------------------- live reports
    def heartbeat(self, seconds=6.0):
        self.want_until = max(self.want_until, time.time() + seconds)

    def _wanted(self):
        return self.always or time.time() < self.want_until

    def _set_debug(self, on):
        def job():
            c = _Conn()
            try:
                c.call("SetTabletDebug", [bool(on)])
            finally:
                c.close()
        _with_timeout(job, 3.0)

    def _reader(self, conn):
        try:
            conn.call("SetTabletDebug", [True])
            while self.streaming and self.stream_conn is conn:
                m = conn.read_msg()
                if m.get("method") != "DeviceReport":
                    continue
                params = m.get("params")
                arg = params[-1] if isinstance(params, list) and params else params
                if not isinstance(arg, dict):
                    continue
                path = str(_ci(arg, "Path") or "")
                data = _ci(arg, "Data") or {}
                pos = _ci(data, "Position")
                if not isinstance(pos, dict):
                    continue  # aux / out-of-range reports
                now = time.perf_counter()
                tab = parse_tablet(_ci(arg, "Tablet") or {})
                with self.lock:
                    self.times.append(now)
                    self.reports += 1
                    self.last = {"x": _ci(pos, "X"), "y": _ci(pos, "Y"), "pressure": _ci(data, "Pressure"),
                                 "buttons": _ci(data, "PenButtons"), "type": path.split(".")[-1],
                                 "tablet": tab, "t": time.time()}
        except Exception as e:
            if self.streaming:
                self.log("OTD live stream ended:", e)
        finally:
            if self.stream_conn is conn:
                self.streaming = False
                self.stream_conn = None

    def _stream_manager(self):
        last_hz = 0
        while True:
            try:
                want = self._wanted() and self.status["connected"]
                if want and not self.streaming:
                    conn = _with_timeout(_Conn, 3.0)
                    self.streaming = True
                    self.stream_conn = conn
                    threading.Thread(target=self._reader, args=(conn,), daemon=True).start()
                elif not want and self.streaming:
                    self.streaming = False
                    conn, self.stream_conn = self.stream_conn, None
                    try:
                        self._set_debug(False)
                    except Exception:
                        pass
                    if conn:
                        threading.Thread(target=conn.close, daemon=True).start()
                if time.time() - last_hz >= 0.5:
                    last_hz = time.time()
                    hz = self.hz()
                    if hz:
                        self.hz_hist.append((time.time(), hz))
            except Exception as e:
                self.log("OTD-Live:", e)
                time.sleep(2)
            time.sleep(0.2)

    def hz(self, window=1.0):
        """Report rate over the last `window` seconds while the pen is in range."""
        with self.lock:
            ts = list(self.times)
        if len(ts) < 3:
            return None
        now = time.perf_counter()
        if now - ts[-1] > 0.25:
            return None  # pen not in range / no reports right now
        recent = [t for t in ts if now - t <= window]
        if len(recent) < 3:
            return None
        span = recent[-1] - recent[0]
        return round((len(recent) - 1) / span, 1) if span > 0 else None

    def hz_stats(self, since):
        vals = [h for (t, h) in self.hz_hist if t >= since]
        if not vals:
            return None
        vals.sort()
        return {"avg": round(sum(vals) / len(vals), 1), "min": vals[0], "max": vals[-1],
                "median": vals[len(vals) // 2], "n": len(vals)}

    def snapshot(self):
        with self.lock:
            st = {k: v for k, v in self.status.items() if k != "settings"}
            last = dict(self.last) if self.last else None
        return {"status": st, "streaming": self.streaming, "always": self.always,
                "hz": self.hz(), "last": last, "reports": self.reports,
                "hz_session": self.hz_stats(time.time() - 600)}
