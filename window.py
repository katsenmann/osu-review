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


def on_ui(win, fn):
    """Runs fn on the window's UI thread without waiting for it (Windows); elsewhere directly."""
    if os.name != "nt":
        return fn()
    try:
        from System import Action  # pythonnet

        def run():
            try:
                fn()
            except Exception as e:
                log(f"ui task: {e}")
        win.native.BeginInvoke(Action(run))
    except Exception as e:
        log(f"ui task not queued: {e}")


def raise_native(win, js=None):
    """Runs on the UI thread (Windows): jump to a score and bring the window to the front."""
    from System.Windows.Forms import FormWindowState
    f = win.native
    if js:
        try:
            f.browser.webview.CoreWebView2.ExecuteScriptAsync(js)
        except Exception as e:
            log(f"raise js: {e}")
    if f.WindowState == FormWindowState.Minimized:
        f.WindowState = FormWindowState.Normal
    f.Show()
    f.TopMost = True
    f.Activate()
    f.TopMost = False


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


def watch_webview(win):
    """Logs WebView2 engine crashes and reloads the page when only the page process died."""
    if os.name != "nt":
        return
    try:
        core = win.native.browser.webview.CoreWebView2
    except Exception as e:
        log(f"crash watch unavailable: {e}")
        return

    def failed(sender, args):
        try:
            kind = str(args.ProcessFailedKind)
        except Exception:
            kind = "?"
        extra = ""
        for attr in ("Reason", "ExitCode", "ProcessDescription"):
            try:
                extra += f" {attr}={getattr(args, attr)}"
            except Exception:
                pass
        log(f"WebView2 process failed: {kind}{extra}")
        try:
            api("/api/clientlog/line", {"msg": f"window engine: {kind}{extra}"}, timeout=1)
        except Exception:
            pass
        if "Render" in kind or "Frame" in kind or "Gpu" in kind or "Utility" in kind:
            try:
                sender.Reload()
                log("page reloaded after engine failure")
            except Exception as e:
                log(f"reload failed: {e}")

    try:
        core.ProcessFailed += failed
        log("crash watch on")
    except Exception as e:
        log(f"crash watch failed: {e}")


def main():
    try:
        import faulthandler
        faulthandler.enable(open(os.path.join(data_dir(), "window.log"), "a", encoding="utf-8"))
    except Exception:
        pass
    log(f"window start (pid {os.getpid()}, {sys.executable})")
    # if the window hangs before the page has loaded, write where it is stuck into window.log
    try:
        import faulthandler
        faulthandler.dump_traceback_later(20, repeat=True, file=open(os.path.join(data_dir(), "window.log"), "a", encoding="utf-8"))
    except Exception:
        pass
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
                # like the icon: on Windows only queue the work on the UI thread, never wait for it
                # (waiting from this thread could freeze the window when a new score came in)
                js = f"location.hash={json.dumps(r['hash'].lstrip('#'))}" if r.get("hash") else None
                if os.name == "nt":
                    on_ui(win, lambda js=js: raise_native(win, js))
                else:
                    if js:
                        try:
                            win.evaluate_js(js)
                        except Exception:
                            pass
                    raise_window(win)
            if r.get("quit"):
                try:
                    win.destroy()
                except Exception:
                    os._exit(0)

    def on_shown():
        # Never touch win.native from this thread: pywebview waits on the UI thread while this
        # handler runs, and a direct WinForms call from here waits for the UI thread too ->
        # the window froze grey ("not responding"). BeginInvoke queues the work and returns.
        log("window shown")
        threading.Timer(1.5, lambda: on_ui(win, lambda: (set_native_icon(win), watch_webview(win)))).start()
        threading.Thread(target=poll, daemon=True).start()

    def on_loaded():
        try:
            import faulthandler
            faulthandler.cancel_dump_traceback_later()
        except Exception:
            pass
        if not getattr(on_loaded, "done", False):
            on_loaded.done = True
            log("page loaded")

    win.events.shown += on_shown
    win.events.loaded += on_loaded
    store = os.path.join(data_dir(), "window-data")
    os.makedirs(store, exist_ok=True)
    try:
        webview.start(private_mode=False, storage_path=store, icon=ICON if os.name != "nt" else None)
    except TypeError:
        webview.start(private_mode=False, storage_path=store)
    log("window closed by the user")
    try:
        api("/api/shell/closed", {"pid": os.getpid()}, timeout=3)
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
