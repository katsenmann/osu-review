"""Press a key in osu! - used to save a failed play (F2 on the game over screen of osu!stable).

Only presses the key when osu! itself is the window in front, so it can never end up in
another program. Windows only; elsewhere it does nothing.
"""
import ctypes
import os
import time

VK_F2, SC_F2 = 0x71, 0x3C
KEYEVENTF_SCANCODE, KEYEVENTF_KEYUP = 0x0008, 0x0002
INPUT_KEYBOARD = 1


def _foreground_exe():
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    hwnd = u.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = ctypes.c_ulong()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = k.OpenProcess(0x1000, False, pid.value)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(520)
        n = ctypes.c_ulong(520)
        if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        k.CloseHandle(h)


def press_f2_in_osu():
    """-> (ok, message)"""
    if os.name != "nt":
        return False, "only on Windows"
    try:
        exe = _foreground_exe()
        if exe != "osu!.exe":
            return False, f"osu! is not the window in front ({exe or 'nothing'})"

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                        ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t)]

        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", ctypes.c_ulong), ("u", _U)]

        def send(flags):
            i = INPUT(type=INPUT_KEYBOARD)
            i.u.ki = KEYBDINPUT(VK_F2, SC_F2, flags, 0, 0)
            return ctypes.windll.user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))

        a = send(KEYEVENTF_SCANCODE)
        time.sleep(0.06)
        b = send(KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP)
        if a != 1 or b != 1:
            return False, "Windows refused the key press"
        return True, "F2 pressed"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
