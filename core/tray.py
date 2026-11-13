"""Tray icon (Windows notification area) with plain ctypes - no extra packages.

Left click / double click: open osu!review. Right click: menu (Open, Check for updates, Quit).
notify(): a Windows toast-style balloon, e.g. "Update available".
"""
import ctypes
import os
import threading
from ctypes import wintypes

WM_DESTROY, WM_COMMAND, WM_USER, WM_NULL = 0x0002, 0x0111, 0x0400, 0x0000
WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONUP, WM_CONTEXTMENU = 0x0202, 0x0203, 0x0205, 0x007B
WM_TRAY = WM_USER + 20
NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO, NIF_SHOWTIP = 0x1, 0x2, 0x4, 0x10, 0x80
NIIF_INFO, NIIF_USER, NIIF_LARGE_ICON = 0x1, 0x4, 0x20
MF_STRING, MF_SEPARATOR, MF_GRAYED, MF_DEFAULT = 0x0, 0x800, 0x1, 0x1000
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x2, 0x100, 0x80
IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40
IDI_APPLICATION = 32512
ID_OPEN, ID_UPDATE, ID_QUIT = 1, 2, 3

LRESULT = ctypes.c_ssize_t
_FN = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)      # Windows only; CFUNCTYPE keeps the import working elsewhere
WNDPROC = _FN(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT), ("wParam", wintypes.WPARAM),
                ("lParam", wintypes.LPARAM), ("time", wintypes.DWORD), ("pt", wintypes.POINT)]


class Tray:
    def __init__(self, icon_path, log, on_open, on_update, on_quit, tip="osu!review"):
        self.icon_path, self.log, self.tip = icon_path, log, tip
        self.on_open, self.on_update, self.on_quit = on_open, on_update, on_quit
        self.hwnd = None
        self.hicon = None
        self.ready = threading.Event()
        self._added = False
        self._wndproc = WNDPROC(self._proc)        # keep a reference, or Windows calls freed memory

    # ------------------------------------------------------------ public
    def start(self):
        if os.name != "nt":
            return False
        threading.Thread(target=self._run, name="tray", daemon=True).start()
        self.ready.wait(5)
        return self._added

    def notify(self, title, text):
        if not self._added:
            return
        try:
            nid = self._nid(NIF_INFO)
            nid.szInfoTitle = title[:63]
            nid.szInfo = text[:255]
            nid.dwInfoFlags = NIIF_USER | NIIF_LARGE_ICON if self.hicon else NIIF_INFO
            nid.hBalloonIcon = self.hicon
            self.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception as e:
            self.log("tray notify:", e)

    def remove(self):
        if self._added:
            try:
                self.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid(0)))
            except Exception:
                pass
            self._added = False

    # ------------------------------------------------------------ internals
    def _nid(self, flags):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = flags
        return nid

    def _add(self):
        nid = self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP)
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self.hicon
        nid.szTip = self.tip[:127]
        ok = self.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        nid.uVersion = 4
        self.shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid))
        self._added = bool(ok)
        if not ok:
            self.log("tray: could not add the icon")

    def _run(self):
        try:
            u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
            self.u32, self.shell32 = u32, ctypes.windll.shell32
            u32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            u32.DefWindowProcW.restype = LRESULT
            u32.CreateWindowExW.restype = wintypes.HWND
            u32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                            wintypes.HINSTANCE, wintypes.LPVOID]
            u32.LoadImageW.restype = wintypes.HANDLE
            u32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
            u32.LoadIconW.restype = wintypes.HICON
            u32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPVOID]
            u32.CreatePopupMenu.restype = wintypes.HMENU
            u32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
            u32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                           wintypes.HWND, wintypes.LPVOID]
            u32.SetMenuDefaultItem.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.UINT]
            u32.DestroyMenu.argtypes = [wintypes.HMENU]
            u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            u32.SetForegroundWindow.argtypes = [wintypes.HWND]
            u32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
            u32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
            u32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
            u32.RegisterWindowMessageW.restype = wintypes.UINT
            self.shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
            k32.GetModuleHandleW.restype = wintypes.HMODULE
            hinst = k32.GetModuleHandleW(None)

            wc = WNDCLASSW()
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = hinst
            wc.lpszClassName = "osureview_tray"
            u32.RegisterClassW(ctypes.byref(wc))
            self.hwnd = u32.CreateWindowExW(0, "osureview_tray", "osu!review tray", 0, 0, 0, 0, 0, None, None, hinst, None)
            self.msg_taskbar = u32.RegisterWindowMessageW("TaskbarCreated")
            if os.path.isfile(self.icon_path):
                self.hicon = u32.LoadImageW(None, self.icon_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if not self.hicon:
                self.hicon = u32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
            self._add()
        except Exception as e:
            self.log("tray failed:", e)
            self.ready.set()
            return
        self.ready.set()
        msg = MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))

    def _fire(self, fn):
        def go():
            try:
                fn()
            except Exception as e:
                self.log("tray action:", e)
        threading.Thread(target=go, daemon=True).start()

    def _menu(self):
        u32 = self.u32
        m = u32.CreatePopupMenu()
        u32.AppendMenuW(m, MF_STRING, ID_OPEN, "Open osu!review")
        u32.AppendMenuW(m, MF_STRING, ID_UPDATE, "Check for updates")
        u32.AppendMenuW(m, MF_SEPARATOR, 0, None)
        u32.AppendMenuW(m, MF_STRING, ID_QUIT, "Quit osu!review")
        u32.SetMenuDefaultItem(m, ID_OPEN, 0)
        pt = wintypes.POINT()
        u32.GetCursorPos(ctypes.byref(pt))
        u32.SetForegroundWindow(self.hwnd)                 # otherwise the menu does not close on outside clicks
        cmd = u32.TrackPopupMenu(m, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY, pt.x, pt.y, 0, self.hwnd, None)
        u32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        u32.DestroyMenu(m)
        if cmd == ID_OPEN:
            self._fire(self.on_open)
        elif cmd == ID_UPDATE:
            self._fire(self.on_update)
        elif cmd == ID_QUIT:
            self.remove()
            self._fire(self.on_quit)

    def _proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                ev = lparam & 0xFFFF                        # NOTIFYICON_VERSION_4: event in the low word
                if ev in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self._fire(self.on_open)
                elif ev in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._menu()
                return 0
            if msg == getattr(self, "msg_taskbar", -1):     # Explorer restarted -> icon is gone, add it again
                self._add()
                return 0
            if msg == WM_DESTROY:
                self.remove()
                self.u32.PostQuitMessage(0)
                return 0
        except Exception as e:
            self.log("tray:", e)
        return self.u32.DefWindowProcW(hwnd, msg, wparam, lparam)
