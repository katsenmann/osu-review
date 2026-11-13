"""osu!review - native program window.

Shows the app in its own desktop window (Windows: WebView2 / Edge engine, macOS: WebKit,
Linux: GTK/Qt) - no browser, no tabs, no address bar. The analysis part (app.py) keeps
running in the background; closing this window only closes the window.

Started by app.py with the app's own small Python (see core/shell.py), never by hand.
"""
import json
import os
import socket
import sys
import threading
import time
import urllib.request

PORT = 47290
LOCK_PORT = 47291           # second instance check: whoever can bind this port is the window
HERE = os.path.dirname(os.path.abspath(__file__))
TITLE = "osu!review"
ICON = os.path.join(HERE, "web", "icon.ico")
BASE = f"http://127.0.0.1:{PORT}"


def data_dir():
    if os.name == "nt":
        return os.path.join(os.environ.get("APPDATA", HERE), "osu-review")
    return os.path.join(HERE, "data")


def api(path, body=None, timeout=2.0):
    req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def log(msg):
    try:
        with open(os.path.join(data_dir(), "window.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def set_windows_identity():
    """Own taskbar entry + icon instead of grouping under Python."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("osureview.app")
    except Exception:
        pass


def set_native_icon(win):
    if os.name != "nt" or not os.path.isfile(ICON):
        return
    try:
        from System.Drawing import Icon  # pythonnet, comes with pywebview on Windows
        win.native.Icon = Icon(ICON)
    except Exception as e:
        log(f"icon: {e}")


def raise_window(win):
    try:
        win.restore()
        win.show()
        win.on_top = True
        time.sleep(0.15)
        win.on_top = False
    except Exception as e:
        log(f"raise: {e}")


def has_webview2():
    if os.name != "nt":
        return True
    try:
        import winreg
        key = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
        for root, base in ((winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\"), (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\"),
                           (winreg.HKEY_CURRENT_USER, "Software\\")):
            try:
                with winreg.OpenKey(root, base + key) as k:
                    v = winreg.QueryValueEx(k, "pv")[0]
                    if v and v != "0.0.0.0":
                        return True
            except OSError:
                pass
        return False
    except Exception:
        return True


def main():
    set_windows_identity()
    if not has_webview2():
        log("WebView2 runtime not installed")
        return 4
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            lock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", 0), 1)
        lock.bind(("127.0.0.1", LOCK_PORT))
        lock.listen(1)
    except OSError:
        # window already open -> ask it to come to the front
        try:
            api("/api/shell/show", {"hash": sys.argv[1] if len(sys.argv) > 1 else ""})
        except Exception:
            pass
        return 0

    import webview  # pywebview

    start_hash = sys.argv[1] if len(sys.argv) > 1 else ""
    win = webview.create_window(TITLE, BASE + "/?shell=1" + (start_hash or ""), width=1440, height=920,
                                min_size=(1000, 640), background_color="#0e0e14", text_select=True)

    def poll():
        # app.py tells the window when it should come to the front (new score, second start)
        while True:
            time.sleep(0.7)
            try:
                r = api("/api/shell/poll", {"pid": os.getpid()})
            except Exception:
                continue
            if r.get("show"):
                if r.get("hash"):
                    try:
                        win.evaluate_js(f"location.hash={json.dumps(r['hash'].lstrip('#'))}")
                    except Exception:
                        pass
                raise_window(win)
            if r.get("quit"):
                try:
                    win.destroy()
                except Exception:
                    os._exit(0)

    def on_shown():
        set_native_icon(win)
        threading.Thread(target=poll, daemon=True).start()

    win.events.shown += on_shown
    store = os.path.join(data_dir(), "window-data")
    os.makedirs(store, exist_ok=True)
    try:
        webview.start(private_mode=False, storage_path=store, icon=ICON if os.name != "nt" else None)
    except TypeError:
        webview.start(private_mode=False, storage_path=store)
    try:
        api("/api/shell/closed", {"pid": os.getpid()}, timeout=1)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        log(traceback.format_exc())
        sys.exit(3)
