"""Captures the primary display and reads its text and controls. Ports ScreenReader.swift and ControlsReader.swift.

Bluey's own windows stay out of the capture because bluey.py gives each one WDA_EXCLUDEFROMCAPTURE."""
import asyncio
import io
import math
import threading
import time
from dataclasses import dataclass, field
from typing import NamedTuple

import mss
from PIL import Image

import control


class Rect(NamedTuple):
    x: float
    y: float
    w: float
    h: float
    midx = property(lambda r: r.x + r.w / 2)
    midy = property(lambda r: r.y + r.h / 2)
    maxy = property(lambda r: r.y + r.h)

    def inside(self, size):
        return self.x >= 0 and self.y >= 0 and self.x + self.w <= size[0] and self.y + self.h <= size[1]

    def contains(self, point, margin=0):
        return self.x - margin <= point[0] <= self.x + self.w + margin and self.y - margin <= point[1] <= self.y + self.h + margin

    def distance(self, point):
        return math.hypot(max(self.x - point[0], 0, point[0] - self.x - self.w),
                          max(self.y - point[1], 0, point[1] - self.y - self.h))


class Target(NamedTuple):
    """One thing on screen the character can point at or click: a control, a line of text or a single word."""
    id: str
    text: str
    rect: Rect  # logical points, top-left origin


class Control(NamedTuple):
    id: str
    kind: str
    label: str
    rect: Rect


@dataclass
class ScreenSnapshot:
    jpeg: bytes = b""
    lines: list = field(default_factory=list)  # [(line Target, [word Target])]
    size: tuple = (0.0, 0.0)
    app: str = None
    controls: list = field(default_factory=list)

    def _control_target(self, c):
        return Target(c.id, c.label or c.kind, c.rect)

    def target(self, id):
        id = id.strip().upper()
        for c in self.controls:
            if c.id == id:
                return self._control_target(c)
        for line, words in self.lines:
            if line.id == id:
                return line
            for word in words:
                if word.id == id:
                    return word
        return None

    def target_near(self, point):
        """What's under or right next to a point: the smallest word, line or control there."""
        candidates = [self._control_target(c) for c in self.controls]
        for line, words in self.lines:
            candidates += [line, *words]
        hits = [t for t in candidates if t.rect.contains(point, 6)]
        if hits:
            return min(hits, key=lambda t: t.rect.w * t.rect.h)
        near = [t for t in candidates if t.rect.distance(point) < 60]
        return min(near, key=lambda t: t.rect.distance(point)) if near else None

    @property
    def target_list(self):
        """The list the model picks from. Coordinates are on a 0-1000 grid so it can relate them to the image."""
        def grid(r):
            return f"@{int(r.midx / self.size[0] * 1000)},{int(r.midy / self.size[1] * 1000)}"
        out = []
        if self.app:
            out.append(f"Frontmost app: {self.app}")
        if self.controls:
            out.append("Controls (click these by id):")
            out += [f"{c.id} {c.kind} {grid(c.rect)}" + (f' "{c.label}"' if c.label else "") for c in self.controls]
        out.append("Text on screen (L = line, W = word, @x,y on a 0-1000 grid):")
        if not self.lines:
            out.append("(no text found)")
        for line, words in self.lines:
            detail = " | " + " ".join(f"{w.id}={w.text}" for w in words) if len(words) > 1 else ""
            out.append(f'{line.id} {grid(line.rect)} "{line.text}"{detail}')
        return "\n".join(out)


def snapshot() -> ScreenSnapshot:
    """Blocking. Call it off the event loop."""
    width, height = control.physical_size()
    with mss.mss() as grabber:
        shot = grabber.grab({"left": 0, "top": 0, "width": width, "height": height})
    scale = control.scale()
    size = (width / scale, height / scale)
    # Read the front app's controls at the same time as the text. A hung app must not hold up the check.
    controls = []
    reader = threading.Thread(target=lambda: controls.extend(read_controls(size)), daemon=True)
    reader.start()
    lines = recognize(shot.bgra, width, height, scale)
    reader.join(1.0)
    image = Image.frombytes("RGB", (width, height), shot.rgb)
    image.thumbnail((1100, 1100))
    jpeg = io.BytesIO()
    image.save(jpeg, "JPEG", quality=72)
    return ScreenSnapshot(jpeg.getvalue(), lines, size, control.front_process()[1], list(controls))


# MARK: OCR

_ocr_warned = False


def recognize(bgra: bytes, width, height, scale):
    """Text with exact boxes, through the OCR engine built into Windows. Empty if no OCR language is installed."""
    global _ocr_warned
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter
    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        if not _ocr_warned:
            print("Bluey: no Windows OCR language is installed, so checks use the screenshot alone.")
            _ocr_warned = True
        return []
    writer = DataWriter()
    writer.write_bytes(bgra)
    bitmap = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8, width, height)

    async def read():
        return await engine.recognize_async(bitmap)
    observed = []
    for line in asyncio.run(read()).lines:
        boxes = [w.bounding_rect for w in line.words]
        if not boxes:
            continue
        left, top = min(b.x for b in boxes), min(b.y for b in boxes)
        right, bottom = max(b.x + b.width for b in boxes), max(b.y + b.height for b in boxes)
        observed.append((line.text, Rect(left / scale, top / scale, (right - left) / scale, (bottom - top) / scale),
                         [(w.text, w.bounding_rect) for w in line.words]))
    # Reading order: top to bottom, then left to right, treating rows within 1% of the height as level.
    row = max(height / scale * 0.01, 1)
    observed.sort(key=lambda o: (round(o[1].y / row), o[1].x))
    result, word_count = [], 0
    for i, (text, rect, words) in enumerate(observed[:400], 1):
        targets = []
        if word_count < 600:
            for word, box in words:
                word_count += 1
                targets.append(Target(f"W{word_count}", word, Rect(box.x / scale, box.y / scale, box.width / scale, box.height / scale)))
        result.append((Target(f"L{i}", text, rect), targets))
    return result


# MARK: Controls

_KINDS = {
    "ButtonControl": "button", "SplitButtonControl": "menu button", "CheckBoxControl": "checkbox",
    "RadioButtonControl": "option", "EditControl": "text field", "ComboBoxControl": "combo box",
    "HyperlinkControl": "link", "SliderControl": "slider", "SpinnerControl": "stepper",
    "TabItemControl": "tab", "MenuItemControl": "menu", "TreeItemControl": "disclosure",
}
_FIELDS = {"text field", "combo box"}
_IS_PASSWORD = 30019  # UIA_IsPasswordPropertyId


def _rect(c):
    b = c.BoundingRectangle
    s = control.scale()
    return Rect(b.left / s, b.top / s, (b.right - b.left) / s, (b.bottom - b.top) / s)


def _describe(c, kind):
    parts = []
    for text in (c.Name, c.HelpText):
        if text and text.strip():
            parts.append(text.strip())
            break
    value = ""
    if not c.GetPropertyValue(_IS_PASSWORD):
        try:
            pattern = c.GetValuePattern() if kind in _FIELDS or not parts else None
            value = (pattern.Value or "").strip() if pattern else ""
        except Exception:
            value = ""
    if value:
        parts.append(f"contains: {value[:40]}" if kind in _FIELDS else value[:40])
    return ", ".join(parts).replace("\n", " ")


def read_controls(size, limit=140, budget=0.45):
    """The foreground window's visible controls, in reading order, within a small time budget."""
    import uiautomation as auto
    window = control.user32.GetForegroundWindow()
    if not window:
        return []
    found, seen = [], set()
    with auto.UIAutomationInitializerInThread():
        try:
            queue = [auto.ControlFromHandle(window)]
        except Exception:
            return []
        deadline, visited = time.monotonic() + budget, 0
        while queue and visited < 5000 and time.monotonic() < deadline:
            element = queue.pop(0)
            visited += 1
            try:
                if element is None:
                    continue
                kind = _KINDS.get(element.ControlTypeName)
                if kind:
                    rect = _rect(element)
                    if rect.w > 3 and rect.h > 3 and rect.x < size[0] and rect.y < size[1] and rect.x + rect.w > 0 and rect.y + rect.h > 0:
                        label = _describe(element, kind)
                        key = (int(rect.x), int(rect.y), int(rect.w), int(rect.h))
                        if (label or kind in _FIELDS) and key not in seen:
                            seen.add(key)
                            found.append((kind, label, rect))
                # Menu items hold whole menus, so don't walk into closed menus.
                if element.ControlTypeName != "MenuItemControl":
                    queue += element.GetChildren()[:400]
            except Exception:
                continue  # elements vanish while we walk; skip those
    found.sort(key=lambda f: (round(f[2].y / 6), f[2].x))
    return [Control(f"C{i}", kind, label, rect) for i, (kind, label, rect) in enumerate(found[:limit], 1)]


def focused_field():
    """Where typing will go and whether it's a password field: (rect or None, secure). Blocking."""
    import uiautomation as auto
    with auto.UIAutomationInitializerInThread():
        try:
            focused = auto.GetFocusedControl()
            if focused is None:
                return None, False
            return _rect(focused), bool(focused.GetPropertyValue(_IS_PASSWORD))
        except Exception:
            return None, False
