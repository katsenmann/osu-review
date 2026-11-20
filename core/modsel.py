"""Knows whether a menu is open over osu!'s song select (mod select F1, beatmap options F3 and the
dialogs behind it), so the song-select overlay can get out of the way.

tosu doesn't report these menus, so this watches what you do while osu! is the window in front:
F1 / "mods" button / a mod change -> mod select; F3 / "options" button / right-click on a map ->
beatmap options; Esc, the number keys and the buttons close them again. Only reads the key/mouse state (GetAsyncKeyState) - it never sends anything to osu!.
Windows only; elsewhere it stays "closed".
"""
import ctypes
import os
import threading
import time

from core.keys import _foreground_exe

VK_LBUTTON, VK_RBUTTON, VK_RETURN, VK_ESCAPE, VK_F1, VK_F3 = 0x01, 0x02, 0x0D, 0x1B, 0x70, 0x72
DIGITS = {0x30 + i: i for i in range(10)}
# beatmap options rows (osu! layout units, 768 high): y of row 1..6, each ~62 high, centred, ~735 wide
OPT_ROWS = [164, 244, 327, 406, 486, 566]


class ModSelect:
    def __init__(self, get_live, log=None):
        self.get_live = get_live
        self.log = log or (lambda *_: None)
        self.kind = None          # None | "mods" | "opts" | "sub" (a dialog opened from the options)
        self._md5 = self._mods = None
        self._down = {}
        self._fg_t = 0
        self._fg = False

    def start(self):
        if os.name == "nt":
            threading.Thread(target=self._run, daemon=True, name="modsel").start()
        return self

    # ---------------------------------------------------------------- helpers
    def _pressed(self, vk):
        """True once when the key goes down."""
        st = ctypes.windll.user32.GetAsyncKeyState(vk)
        now = bool(st & 0x8000)
        was = self._down.get(vk, False)
        self._down[vk] = now
        return (now and not was) or (bool(st & 1) and not now)   # bit 0 also catches very short taps

    def _osu_front(self):
        t = time.time()
        if t - self._fg_t > 0.5:          # the process lookup is the slow part
            self._fg_t = t
            try:
                self._fg = _foreground_exe() == "osu!.exe"
            except Exception:
                self._fg = False
        return self._fg

    @staticmethod
    def _cursor_units():
        """Cursor in osu!'s layout units (768 high, x from the left edge) -> (x, y, width) or None."""
        u = ctypes.windll.user32

        class RECT(ctypes.Structure):
            _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        hwnd = u.GetForegroundWindow()
        rc, p, o = RECT(), POINT(), POINT()
        if not hwnd or not u.GetClientRect(hwnd, ctypes.byref(rc)) or not u.GetCursorPos(ctypes.byref(p)):
            return None
        u.ClientToScreen(hwnd, ctypes.byref(o))
        h = rc.b - rc.t
        if h <= 0:
            return None
        s = h / 768.0
        return (p.x - o.x) / s, (p.y - o.y) / s, (rc.r - rc.l) / s

    @property
    def open(self):
        return self.kind is not None

    def _set(self, v, why=""):
        """v: True/False (mod select) or a kind string / None."""
        self.kind = ("mods" if v is True else None if v is False else v)

    # ---------------------------------------------------------------- loop
    def _tick(self):
        live = self.get_live() or {}
        if live.get("state") != 5:
            self._set(None)
            self._md5 = self._mods = None
            return
        md5, mods = live.get("md5"), live.get("mods") or ""
        if md5 != self._md5:
            if self._md5 is not None and self.kind == "mods":
                self._set(None)
            self._md5, self._mods = md5, mods
        elif mods != self._mods:
            self._mods = mods
            ctrl = os.name == "nt" and ctypes.windll.user32.GetAsyncKeyState(0x11) & 0x8000
            if not ctrl:                                   # Ctrl+A etc. toggle mods without the mod select
                self._set("mods")
        if not self._osu_front():
            self._down.clear()
            return
        k = self.kind
        digit = next((d for vk, d in DIGITS.items() if self._pressed(vk)), None)
        if self._pressed(VK_F1) and k in (None, "mods"):
            self._set(None if k == "mods" else "mods")
        if self._pressed(VK_F3) and k is None:
            self._set("opts")
        if self._pressed(VK_ESCAPE) or self._pressed(VK_RETURN):
            self._set(None)
        if digit is not None:
            if k == "mods" and digit in (1, 2):
                self._set(None if digit == 2 else "mods")
            elif k == "opts":
                self._set("sub" if digit in (1, 2, 4) else None)
            elif k == "sub":
                self._set(None)
        left, right = self._pressed(VK_LBUTTON), self._pressed(VK_RBUTTON)
        if not (left or right):
            return
        c = self._cursor_units()
        if not c:
            return
        x, y, w = c
        k = self.kind
        centre = abs(x - w / 2) <= 380
        on_mods_btn = 300 <= x <= 395 and 660 <= y <= 750       # bottom bar: "mods"
        on_opts_btn = 470 <= x <= 560 and 660 <= y <= 750       # bottom bar: "options"
        if right:
            if k is None and x >= w - 640 and 80 <= y <= 690:   # right-click on a map in the list
                self._set("opts")
            return
        if k is None:
            if on_mods_btn:
                self._set("mods")
            elif on_opts_btn:
                self._set("opts")
        elif k == "mods":
            if (centre and 582 <= y <= 660) or on_mods_btn:     # "2. Close"
                self._set(None)
        elif k == "opts":
            row = next((i + 1 for i, ry in enumerate(OPT_ROWS) if ry - 4 <= y <= ry + 66), None)
            if centre and row:
                self._set("sub" if row in (1, 2, 4) else None)
        elif k == "sub":
            if centre and 500 <= y <= 665:                       # "Close" / "Yes" / "No" buttons at the bottom
                                                                 # (clicks in the collection list keep it hidden)
                self._set(None)

    def _run(self):
        while True:
            try:
                self._tick()
            except Exception as e:
                self.log(f"modsel: {e}")
                time.sleep(5)
            time.sleep(1 / 40)
