"""Keeps the osu! API client secret out of plain text on disk.

On Windows the secret is encrypted with DPAPI (CryptProtectData): only your Windows account on this
PC can decrypt it, so copying config.json somewhere else doesn't reveal it. Elsewhere it stays as is.
"""
import base64
import ctypes
import os

PREFIX = "dpapi:"
MASK = "********"          # what the UI gets instead of the real secret


class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _call(fn, data):
    buf = ctypes.create_string_buffer(data, len(data))
    src = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _BLOB()
    ok = fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out))  # CRYPTPROTECT_UI_FORBIDDEN
    if not ok:
        raise OSError("DPAPI failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def protect(text):
    if not text or os.name != "nt" or str(text).startswith(PREFIX):
        return text
    try:
        c = ctypes.windll.crypt32
        return PREFIX + base64.b64encode(_call(c.CryptProtectData, str(text).encode("utf-8"))).decode()
    except Exception:
        return text


def unprotect(text):
    if not text or not str(text).startswith(PREFIX):
        return text
    try:
        raw = base64.b64decode(str(text)[len(PREFIX):])
        c = ctypes.windll.crypt32
        fn = c.CryptUnprotectData
        buf = ctypes.create_string_buffer(raw, len(raw))
        src = _BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        out = _BLOB()
        if not fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out)):
            return ""
        try:
            return ctypes.string_at(out.pbData, out.cbData).decode("utf-8")
        finally:
            ctypes.windll.kernel32.LocalFree(out.pbData)
    except Exception:
        return ""
