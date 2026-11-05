"""Hands on the keyboard and mouse, plus the Win32 facts about the session. Ports ComputerControl.swift and
the idle, lock and front-app calls in FocusCompanion.swift. Positions are logical points, top-left origin of
the primary display (the overlay's space). Win32 works in physical pixels, so calls go through scale()."""
import asyncio
import ctypes
import json
import math
import os
import subprocess
import time
import urllib.parse
from ctypes import wintypes

import psutil

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shcore = ctypes.WinDLL("shcore", use_last_error=True)
user32.OpenInputDesktop.restype = wintypes.HANDLE
user32.GetForegroundWindow.restype = wintypes.HWND
user32.MonitorFromPoint.restype = wintypes.HMONITOR
user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
kernel32.GetTickCount64.restype = ctypes.c_ulonglong
# Before anything else reads a coordinate: without this, Windows reports a scaled 1536x864 for a 1920x1080 panel,
# and importing uiautomation or mss later changes the answer mid-run. -4 is PER_MONITOR_AWARE_V2, Qt's own default.
user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))

_ULONG_PTR = ctypes.c_size_t


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", _ULONG_PTR)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", _ULONG_PTR)]


class _UNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _UNION)]


_MOVE_ABSOLUTE = 0x0001 | 0x8000
_LEFT_DOWN, _LEFT_UP, _RIGHT_DOWN, _RIGHT_UP = 0x0002, 0x0004, 0x0008, 0x0010
_WHEEL, _HWHEEL = 0x0800, 0x1000
_KEY_UP, _KEY_UNICODE, _KEY_EXTENDED = 0x0002, 0x0004, 0x0001


def _send(*inputs):
    array = (_INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), array, ctypes.sizeof(_INPUT))


def _mouse(flags, dx=0, dy=0, data=0):
    event = _INPUT(type=0)
    event.u.mi = _MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
    return event


def _key(vk=0, scan=0, flags=0):
    event = _INPUT(type=1)
    event.u.ki = _KEYBDINPUT(vk, scan, flags, 0, 0)
    return event


# MARK: Session facts

def scale():
    """Physical pixels per logical point on the primary display."""
    dpi_x, dpi_y = wintypes.UINT(96), wintypes.UINT(96)
    shcore.GetDpiForMonitor(user32.MonitorFromPoint(wintypes.POINT(0, 0), 1), 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
    return dpi_x.value / 96


def physical_size():
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def screen_unlocked():
    """The input desktop is unreadable or not "Default" while the PC is locked or on a secure prompt."""
    desktop = user32.OpenInputDesktop(0, False, 0x0001)
    if not desktop:
        return False
    try:
        name = ctypes.create_unicode_buffer(64)
        user32.GetUserObjectInformationW(desktop, 2, name, ctypes.sizeof(name), None)
        return name.value == "Default"
    finally:
        user32.CloseDesktop(desktop)


def idle_seconds():
    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
    info = _LASTINPUTINFO(ctypes.sizeof(_LASTINPUTINFO))
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    # dwTime is a 32-bit tick count, so compare in the same width.
    return ((kernel32.GetTickCount64() & 0xFFFFFFFF) - info.dwTime & 0xFFFFFFFF) / 1000


def front_process():
    """(pid, app name) of the foreground window, or (None, None)."""
    window = user32.GetForegroundWindow()
    if not window:
        return None, None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
    try:
        return pid.value, psutil.Process(pid.value).name().removesuffix(".exe")
    except psutil.Error:
        return pid.value, None


# MARK: Mouse

def mouse_location():
    point = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    s = scale()
    return point.x / s, point.y / s


def warp(point):
    """Puts the real pointer back where the user left it."""
    s = scale()
    user32.SetCursorPos(round(point[0] * s), round(point[1] * s))


def _move(point):
    s = scale()
    width, height = physical_size()
    return _mouse(_MOVE_ABSOLUTE, math.ceil(point[0] * s * 65535 / max(width - 1, 1)),
                  math.ceil(point[1] * s * 65535 / max(height - 1, 1)))


async def click(point, right=False, count=1):
    down, up = (_RIGHT_DOWN, _RIGHT_UP) if right else (_LEFT_DOWN, _LEFT_UP)
    _send(_move(point))
    await asyncio.sleep(0.03)
    for i in range(1, max(1, count) + 1):
        _send(_mouse(down))
        await asyncio.sleep(0.035)
        _send(_mouse(up))
        if i < count:
            await asyncio.sleep(0.08)


def drag_begin(point):
    _send(_move(point), _mouse(_LEFT_DOWN))


def drag_move(point):
    _send(_move(point))


def drag_end(point):
    _send(_move(point), _mouse(_LEFT_UP))


async def scroll(dx, dy, point):
    """dx and dy are notches (120 wheel units). Positive dy scrolls down the page, positive dx scrolls right."""
    _send(_move(point))
    for _ in range(abs(dy)):
        _send(_mouse(_WHEEL, data=-120 if dy > 0 else 120))
        await asyncio.sleep(0.016)
    for _ in range(abs(dx)):
        _send(_mouse(_HWHEEL, data=120 if dx > 0 else -120))
        await asyncio.sleep(0.016)


# MARK: Keyboard

class UnknownKey(Exception):
    def __init__(self, key):
        super().__init__(f'I don\'t know the key "{key}".')


_KEYS = {
    **{chr(c): c - 32 for c in range(ord("a"), ord("z") + 1)}, **{str(d): 0x30 + d for d in range(10)},
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
    "return": 0x0D, "enter": 0x0D, "tab": 0x09, "space": 0x20, "spacebar": 0x20, "delete": 0x2E, "del": 0x2E,
    "forwarddelete": 0x2E, "backspace": 0x08, "escape": 0x1B, "esc": 0x1B, "left": 0x25, "up": 0x26, "right": 0x27,
    "down": 0x28, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "-": 0xBD, "minus": 0xBD, "=": 0xBB,
    "equal": 0xBB, "plus": 0xBB, "[": 0xDB, "]": 0xDD, ";": 0xBA, "'": 0xDE, ",": 0xBC, "comma": 0xBC, ".": 0xBE,
    "period": 0xBE, "/": 0xBF, "slash": 0xBF, "\\": 0xDC, "`": 0xC0,
}
_EXTENDED = {0x25, 0x26, 0x27, 0x28, 0x24, 0x23, 0x21, 0x22, 0x2E}
_MODIFIERS = {"ctrl": (0x11, "Ctrl"), "control": (0x11, "Ctrl"), "cmd": (0x11, "Ctrl"), "command": (0x11, "Ctrl"),
              "shift": (0x10, "Shift"), "alt": (0x12, "Alt"), "option": (0x12, "Alt"), "opt": (0x12, "Alt"),
              "win": (0x5B, "Win"), "windows": (0x5B, "Win"), "meta": (0x5B, "Win"), "super": (0x5B, "Win")}
_LABELS = {"return": "↩", "enter": "↩", "tab": "⇥", "space": "Space", "spacebar": "Space", "backspace": "⌫",
           "delete": "Del", "del": "Del", "forwarddelete": "Del", "escape": "Esc", "esc": "Esc", "left": "←",
           "right": "→", "up": "↑", "down": "↓"}


def parse_combo(combo):
    """Returns (modifier VKs, key VK, label) for a shortcut like "ctrl+t", "return" or "ctrl+shift+n"."""
    modifiers, key, labels, key_label = [], None, [], ""
    for part in filter(None, combo.lower().replace(" ", "").split("+")):
        if part in _MODIFIERS:
            vk, name = _MODIFIERS[part]
            if vk not in modifiers:
                modifiers.append(vk)
                labels.append(name)
        elif part in _KEYS:
            key, key_label = _KEYS[part], _LABELS.get(part, part.upper())
        else:
            raise UnknownKey(part)
    if key is None:
        raise UnknownKey(combo)
    return modifiers, key, "+".join(labels + [key_label])


def _press_vk(key, modifiers=()):
    extended = _KEY_EXTENDED if key in _EXTENDED else 0
    _send(*[_key(m) for m in modifiers], _key(key, flags=extended), _key(key, flags=extended | _KEY_UP),
          *[_key(m, flags=_KEY_UP) for m in reversed(modifiers)])


def press(combo):
    modifiers, key, label = parse_combo(combo)
    _press_vk(key, modifiers)
    return label


def _type_unicode(text):
    units = text.encode("utf-16-le")
    events = []
    for i in range(0, len(units), 2):
        unit = int.from_bytes(units[i:i + 2], "little")
        events += [_key(scan=unit, flags=_KEY_UNICODE), _key(scan=unit, flags=_KEY_UNICODE | _KEY_UP)]
    _send(*events)


async def type_text(text, on_chunk):
    # Short text types at a natural pace. Long text goes in faster, a few characters at a time.
    size, pause = (6, 0.012) if len(text) > 160 else (1, 0.022)
    chunk = ""

    async def flush():
        nonlocal chunk
        if chunk:
            _type_unicode(chunk)
            on_chunk(chunk)
            chunk = ""
            await asyncio.sleep(pause)

    for character in text:
        if character in "\n\r\t":
            await flush()
            _press_vk(0x09 if character == "\t" else 0x0D)
            await asyncio.sleep(pause)
            continue
        chunk += character
        if len(chunk) >= size:
            await flush()
    await flush()


# MARK: Apps and links

def _window_of(wanted):
    """The first visible titled top-level window whose process name is `wanted`."""
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(window, _):
        if user32.IsWindowVisible(window) and user32.GetWindowTextLengthW(window):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
            try:
                if psutil.Process(pid.value).name().removesuffix(".exe").lower() == wanted:
                    found.append(window)
                    return False
            except psutil.Error:
                pass
        return True

    user32.EnumWindows(each, 0)
    return found[0] if found else None


def start_apps():
    """Every Start menu app as (name, AppID). This covers desktop and Store apps with one call."""
    output = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                             "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-StartApps | ConvertTo-Json -Compress"],
                            capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW).stdout
    apps = json.loads(output)
    return [(a["Name"], a["AppID"]) for a in ([apps] if isinstance(apps, dict) else apps)]


def find_app(wanted, apps=None):
    apps = start_apps() if apps is None else apps
    wanted = wanted.lower()
    for test in (lambda n: n == wanted, lambda n: n.startswith(wanted), lambda n: wanted in n):
        for name, app_id in apps:
            if test(name.lower()):
                return name, app_id
    return None


async def open_app(name):
    """Opens or brings forward an app by name, like "Edge" or "notepad"."""
    wanted = name.strip().lower().removesuffix(".exe")
    window = _window_of(wanted)
    if window:
        user32.ShowWindow(window, 9)  # SW_RESTORE
        # Windows only lets a process take the foreground just after input, and a tapped Alt counts as input.
        _press_vk(0x12)
        user32.SetForegroundWindow(window)
        return f"Switched to {name}."
    try:
        found = await asyncio.to_thread(find_app, wanted)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return f"I couldn't look for {name}: {error}"
    if not found:
        return f"I couldn't find an app called {name}."
    subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{found[1]}"])
    return f"Opened {found[0]}."


def open_url(text):
    """Opens a website or link in the default browser."""
    address = text.strip()
    if "://" not in address:
        address = "https://" + address
    url = urllib.parse.urlparse(address)
    if url.scheme.lower() not in ("http", "https") or not url.hostname:
        return "That doesn't look like a web address."
    os.startfile(address)
    return f"Opened {url.hostname}."
