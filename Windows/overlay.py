"""The big character cursor. Ports CursorOverlay.swift: motion in CursorEngine, drawing in CursorView (QPainter
instead of Core Animation layers), and a click-through, transparent window over the whole primary screen."""
import math
import random
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (QColor, QCursor, QFont, QFontMetricsF, QGuiApplication, QLinearGradient, QPainter,
                           QPainterPath, QPen, QPixmap, QPolygonF, QRadialGradient, QTransform)
from PySide6.QtWidgets import QWidget

import link

BERRY1, BERRY2, BERRY3, BERRY4, INK = 0xA9BCFF, 0x6C86F5, 0x4254D6, 0x2B2F8F, 0x17151F
GRADIENT = [(0, BERRY1), (0.34, BERRY2), (0.68, BERRY3), (1, BERRY4)]


def color(hex_value, alpha=1.0):
    c = QColor(hex_value >> 16 & 255, hex_value >> 8 & 255, hex_value & 255)
    c.setAlphaF(alpha)
    return c


def display_font(size):
    font = QFont("Fredoka")
    font.setPixelSize(int(size))
    font.setWeight(QFont.Weight.Medium)
    return font


class TimingCurve:
    """A CSS-style cubic-bezier timing curve: maps time (0..1) to progress (0..1)."""

    def __init__(self, x1, y1, x2, y2):
        self.x1, self.y1, self.x2, self.y2 = x1, y1, x2, y2

    @staticmethod
    def _coordinate(t, a, b):
        u = 1 - t
        return 3 * u * u * t * a + 3 * u * t * t * b + t * t * t

    @staticmethod
    def _derivative(t, a, b):
        u = 1 - t
        return 3 * u * u * a + 6 * u * t * (b - a) + 3 * t * t * (1 - b)

    def _parameter(self, x):
        t = x
        for _ in range(8):
            error = self._coordinate(t, self.x1, self.x2) - x
            if abs(error) < 1e-7:
                return t
            d = self._derivative(t, self.x1, self.x2)
            if abs(d) < 1e-7:
                break
            t -= error / d
        low, high = 0.0, 1.0
        t = x
        for _ in range(40):
            value = self._coordinate(t, self.x1, self.x2)
            if abs(value - x) < 1e-7:
                break
            if value < x:
                low = t
            else:
                high = t
            t = (low + high) / 2
        return t

    def value(self, x):
        if x <= 0:
            return 0.0
        if x >= 1:
            return 1.0
        return self._coordinate(self._parameter(x), self.y1, self.y2)

    def rate(self, x):
        t = self._parameter(min(max(x, 0.0), 1.0))
        dx = self._derivative(t, self.x1, self.x2)
        return self._derivative(t, self.y1, self.y2) / dx if dx > 1e-7 else 0.0


LAUNCH = TimingCurve(0.42, 0, 0.22, 1)  # starts gently, arrives with a long, soft deceleration
REDIRECT = TimingCurve(0.25, 0.25, 0.22, 1)  # keeps the speed it already had, then settles
STEADY = TimingCurve(0.42, 0, 0.38, 1)  # even and deliberate, for drags
_RING_EASE = TimingCurve(0.2, 0.7, 0.3, 1)
_STAR_EASE = TimingCurve(0.1, 0.8, 0.3, 1)


class Flight:
    """One planned trip: a smooth cubic curve walked with an easing curve."""

    def __init__(self, start_at, c1, c2, to, start, duration, timing, pointing):
        self.origin, self.c1, self.c2, self.to = start_at, c1, c2, to
        self.start, self.duration, self.timing, self.pointing = start, duration, timing, pointing
        self.landed = False

    def progress(self, now):
        return 1.0 if self.duration <= 0 else min(1.0, max(0.0, (now - self.start) / self.duration))

    def position(self, e):
        u = 1 - e
        a, b, c, d = u * u * u, 3 * u * u * e, 3 * u * e * e, e * e * e
        return (a * self.origin[0] + b * self.c1[0] + c * self.c2[0] + d * self.to[0],
                a * self.origin[1] + b * self.c1[1] + c * self.c2[1] + d * self.to[1])

    def tangent(self, e):
        u = 1 - e
        a, b, c = 3 * u * u, 6 * u * e, 3 * e * e
        return (a * (self.c1[0] - self.origin[0]) + b * (self.c2[0] - self.c1[0]) + c * (self.to[0] - self.c2[0]),
                a * (self.c1[1] - self.origin[1]) + b * (self.c2[1] - self.c1[1]) + c * (self.to[1] - self.c2[1]))


class CursorEngine:
    """Where the cursor is and how it moves. Coordinates are top-left origin, y down, in logical points.
    Modes: "docked", "following" or ("pinned", (x, y))."""

    def __init__(self):
        self.settings = link.settings
        self.mode = "following"
        self.tip = (-1000.0, -1000.0)
        self.velocity = (0.0, 0.0)
        self.angle = math.pi / 4
        self.angle_velocity = 0.0
        self.opacity = 0.0
        self.flight = None
        self.placed = False
        self.bounds = (1440.0, 900.0)
        self.dragging = False
        self.on_land = None
        self.on_launch = None
        self.landed_at = -10.0
        self.press_start = -10.0
        self.press_depth = 0.0
        self.talk_until = 0.0
        self.brain_mood = None
        self.gaze_override = None
        self.awake = False
        self.mouse = (0.0, 0.0)
        self.last_mouse_move = time.perf_counter()
        self.blink_until = 0.0
        self.next_blink = time.perf_counter() + 3

    size = property(lambda self: float(self.settings.cursorSize))
    is_home = property(lambda self: not isinstance(self.mode, tuple))

    def phone_point(self, b):
        return b[0] * self.settings.phonePosition, b[1] + b[1] * 0.18

    def dock_point(self, b):
        return b[0] * self.settings.phonePosition, b[1] - self.size * 0.42

    def tucked_point(self, b):
        return b[0] * self.settings.phonePosition, b[1] + self.size * 0.35

    def _goal(self):
        if self.mode == "docked":
            return self.dock_point(self.bounds)
        if self.mode == "following":
            return self.tucked_point(self.bounds)
        return self.mode[1]

    def time_to_arrive(self, now):
        return max(0.0, self.flight.start + self.flight.duration - now) if self.flight else 0.0

    def press(self, depth, now=None):
        """A little squish toward the tip, like pressing a button."""
        self.press_start = time.perf_counter() if now is None else now
        self.press_depth = depth

    def step(self, dt, now, mouse, bounds):
        self.bounds = bounds
        if math.hypot(mouse[0] - self.mouse[0], mouse[1] - self.mouse[1]) > 1.5:
            self.last_mouse_move = now
        self.mouse = mouse
        if not self.placed:
            self.tip = self.tucked_point(bounds)
            self.placed = True
        target = self._goal()
        anchor = self.flight.to if self.flight else self.tip
        if math.hypot(anchor[0] - target[0], anchor[1] - target[1]) > 0.5:
            self._plan(target, now)
        self._advance(now)

        # Fades in the moment it leaves home. In follow mode it fades out once it has tucked itself away.
        settled = (self.flight.progress(now) if self.flight else 1.0) >= 1
        want = 0.0 if self.mode == "following" and settled else 1.0
        rate = 12.0 if want > self.opacity else 6.0
        self.opacity += (want - self.opacity) * min(1.0, dt * rate)

        # Upright while out and about (tipped up when parked), leaning a touch into the motion.
        lean = max(-0.2, min(0.2, self.velocity[0] / 2800))
        target_angle = (math.pi / 4 if self.is_home else 0.0) + lean
        k = 24.0
        self.angle_velocity += (k * (target_angle - self.angle) - 2 * math.sqrt(k) * self.angle_velocity) * dt
        self.angle += self.angle_velocity * dt

        if now > self.next_blink:
            self.blink_until = now + 0.13
            self.next_blink = now + random.uniform(2.5, 6)

    def _plan(self, goal, now):
        """A natural-looking trip: a gentle curve, timed like a real hand movement, with a long, soft landing."""
        start = self.tip
        dx, dy = goal[0] - start[0], goal[1] - start[1]
        distance = math.hypot(dx, dy)
        pointing = not self.is_home
        dock = self.dock_point(self.bounds)
        from_home = math.hypot(start[0] - dock[0], start[1] - dock[1]) < self.size * 1.2 or start[1] > self.bounds[1] - 2
        if distance <= 0.5:
            self.flight = Flight(start, start, goal, goal, now, 0.0, LAUNCH, pointing)
            return
        duration = min(1.2, max(0.45, 0.4 + 0.12 * math.log2(1 + distance / 24)))
        if self.dragging:
            duration = min(1.6, max(0.6, duration * 1.45))
        direction = (dx / distance, dy / distance)
        normal = (-direction[1], direction[0])
        if abs(direction[0]) > 0.4:
            if normal[1] > 0:
                normal = (-normal[0], -normal[1])  # sideways trips bow gently upward
        else:
            toward_middle = 1 if self.bounds[0] / 2 - (start[0] + goal[0]) / 2 >= 0 else -1
            if normal[0] * toward_middle < 0:
                normal = (-normal[0], -normal[1])  # vertical trips bow toward the middle
        bow = 0.0 if self.dragging else min(distance * 0.1, 70) * 0.75
        reach = distance * 0.3
        c2 = (goal[0] - direction[0] * reach + normal[0] * bow, goal[1] - direction[1] * reach + normal[1] * bow)
        speed = math.hypot(*self.velocity)
        if speed > 60:
            # A change of plans mid-flight: carry on from the current speed and heading, no kink.
            carry = (self.velocity[0] * duration / 3, self.velocity[1] * duration / 3)
            length = math.hypot(*carry)
            if length > distance * 0.6:
                carry = (carry[0] / length * distance * 0.6, carry[1] / length * distance * 0.6)
            c1 = (start[0] + carry[0], start[1] + carry[1])
            timing = REDIRECT
        else:
            c1 = (start[0] + direction[0] * reach + normal[0] * bow, start[1] + direction[1] * reach + normal[1] * bow)
            timing = STEADY if self.dragging else LAUNCH
        self.flight = Flight(start, c1, c2, goal, now, duration, timing, pointing)
        if pointing and from_home and self.on_launch:
            self.on_launch((dock[0], self.bounds[1]))

    def _advance(self, now):
        flight = self.flight
        if not flight:
            self.velocity = (0.0, 0.0)
            return
        p = flight.progress(now)
        e = flight.timing.value(p)
        self.tip = flight.position(e)
        if p >= 1:
            self.velocity = (0.0, 0.0)
            if not flight.landed:
                flight.landed = True
                if flight.pointing:
                    self.landed_at = now
                    if self.on_land:
                        self.on_land(self.tip)
                elif self.mode == "following" and self.on_launch:
                    self.on_launch((self.tip[0], self.bounds[1]))
        else:
            rate = flight.timing.rate(p) / flight.duration
            d = flight.tangent(e)
            self.velocity = (d[0] * rate, d[1] * rate)

    def press_scale(self, now):
        t = now - self.press_start
        if t < 0 or t >= 0.42:
            return 1.0
        if t < 0.12:
            return 1 - self.press_depth * math.sin(t / 0.12 * math.pi / 2)
        r = (t - 0.12) / 0.3
        return 1 - self.press_depth * math.cos(r * math.pi * 1.5) * math.exp(-r * 3.2)

    def hover(self, now):
        """A slow, gentle hover while it holds a point, eased in so it never jumps."""
        if self.is_home or (self.flight.progress(now) if self.flight else 1.0) < 1:
            return 0.0
        since = now - self.landed_at
        return math.sin(since * 2.4) * 1.6 * min(1.0, max(0.0, (since - 0.35) / 0.6))

    @property
    def body_center(self):
        r = self.size * 0.5 * math.sqrt(2)
        return self.tip[0] + math.cos(self.angle + math.pi / 4) * r, self.tip[1] + math.sin(self.angle + math.pi / 4) * r

    def face(self, bounds, now):
        """What the phone's face should do: look at the cursor (or at your mouse in follow mode)."""
        phone = self.phone_point(bounds)
        c = self.mouse if self.mode == "following" else self.body_center
        gx = max(-1.0, min(1.0, (c[0] - phone[0]) / (bounds[0] * 0.5)))
        gy = -max(0.15, min(1.0, (phone[1] - c[1]) / (bounds[1] * 1.1)))
        if self.gaze_override:
            gx, gy = self.gaze_override
        testing = now < self.talk_until
        talk = 0.5 + 0.5 * math.sin(now * 19) * math.sin(now * 7.3) if testing else 0.0
        mood = self.settings.mood
        if self.brain_mood:
            mood = self.brain_mood
        elif testing:
            mood = "talking"
        elif not self.is_home and mood == "listening":
            mood = "pointing"
        elif self.mode == "following" and not self.awake and mood == "listening":
            # Your mouse sat still: he gets drowsy after 5 seconds and dozes off after 15.
            idle = now - self.last_mouse_move
            if idle > 15:
                mood = "resting"
            elif idle > 5:
                mood = "sleepy"
        return {"gazeX": gx, "gazeY": gy, "mood": mood, "talk": talk}


def teardrop_path(s, r):
    """A square with three fully rounded corners and one sharp one (the tip) at the origin. y down."""
    path = QPainterPath(QPointF(r, 0))
    path.lineTo(s / 2, 0)
    box = QRectF(0, 0, s, s)
    path.arcTo(box, 90, -90)
    path.arcTo(box, 0, -90)
    path.arcTo(box, 270, -90)
    path.lineTo(0, r)
    path.arcTo(QRectF(0, 0, 2 * r, 2 * r), 180, -90)
    path.closeSubpath()
    return path


def star_path(radius):
    points = []
    for i in range(8):
        r = radius if i % 2 == 0 else radius * 0.4
        a = i * math.pi / 4 - math.pi / 2
        points.append(QPointF(math.cos(a) * r, math.sin(a) * r))
    path = QPainterPath()
    path.addPolygon(QPolygonF(points))
    path.closeSubpath()
    return path


def body_pixmap(side, dpr):
    """The cursor's body, drawn once (gradient, shine, white rim) and then just moved around."""
    pad = side * 0.45
    total = side + pad * 2
    pixmap = QPixmap(math.ceil(total * dpr), math.ceil(total * dpr))
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.translate(pad, pad)
    k = side / 96
    path = teardrop_path(side, 6 * k)
    p.fillPath(path, QColor("white"))
    p.setClipPath(path)
    gradient = QLinearGradient(0.25 * side, 0.067 * side, 0.75 * side, 0.933 * side)
    for stop, value in GRADIENT:
        gradient.setColorAt(stop, color(value))
    p.fillRect(QRectF(0, 0, side, side), gradient)
    shine = QRadialGradient(QPointF(0.3 * side, 0.24 * side), 0.38 * side)
    shine.setColorAt(0, QColor(255, 255, 255, 140))
    shine.setColorAt(1, QColor(255, 255, 255, 0))
    p.fillRect(QRectF(0, 0, side, side), shine)
    p.setPen(QPen(QColor("white"), 8 * k))  # half is clipped away, leaving a 4 pt rim
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(path)
    p.end()
    return pixmap, pad


class CursorView(QWidget):
    """Everything on the overlay, painted each frame while anything is visible."""
    _SPEECH_PAD = (24, 15)
    _TAIL = 18

    def __init__(self):
        super().__init__()
        self.engine = CursorEngine()
        self.settings = link.settings
        self.speech_target = None  # the thing he's pointing at, so his bubble sits above it
        self._art = None
        self._samples = []
        self._last_twinkle = 0.0
        self._look = (-0.7, -0.7)
        self._string_mid = None
        self._string_velocity = [0.0, 0.0]
        self._effects = []  # (born, life, painter function of (p, progress))
        self._label = None  # (text, born, life)
        self._caption = None
        self._speech = None  # {text, size, font, born, fading_since}
        self._was_active = False
        self.active = False
        self.engine.on_land = self._soft_land
        self.engine.on_launch = self._puff

    def size_tuple(self):
        return float(self.width()), float(self.height())

    # MARK: Frame

    def step(self, dt, now, mouse):
        self.engine.step(dt, now, mouse, self.size_tuple())
        speed = math.hypot(*self.engine.velocity)
        self._update_look(dt, speed)
        self._update_samples(now, speed)
        self._effects = [e for e in self._effects if now - e[0] <= e[1]]
        if self._label and now - self._label[1] > self._label[2]:
            self._label = None
        if self._speech and self._speech["fading_since"] and now - self._speech["fading_since"] > 0.22:
            self._speech = None
        self._now, self._dt, self._speed = now, dt, speed
        # ponytail: repaints the whole screen while active, nothing while idle. Use dirty rects if 4K stutters.
        visible = self.settings.showCursor and self.engine.opacity > 0.005
        self.active = visible or bool(self._effects or self._label or self._speech)
        if self.active or self._was_active:
            self.update()
        self._was_active = self.active

    def _update_look(self, dt, speed):
        # Its eyes look where it's heading, and back at the tip when it stops.
        want = (-0.7, -0.7)
        if speed > 90:
            a = -self.engine.angle
            vx, vy = self.engine.velocity
            local = (vx * math.cos(a) - vy * math.sin(a), vx * math.sin(a) + vy * math.cos(a))
            n = max(math.hypot(*local), 1.0)
            want = (local[0] / n, local[1] / n)
        follow = min(1.0, dt * 12)
        self._look = (self._look[0] + (want[0] - self._look[0]) * follow, self._look[1] + (want[1] - self._look[1]) * follow)

    def _update_samples(self, now, speed):
        center = self.engine.body_center
        self._samples.append((center, now))
        self._samples = [s for s in self._samples if now - s[1] <= 0.2]
        if self.settings.showCursor and self.settings.trail == "comet" and speed > 900 and now - self._last_twinkle > 0.05:
            self._last_twinkle = now
            self._twinkle((center[0] + random.uniform(-10, 10), center[1] + random.uniform(-10, 10)))

    # MARK: Paint

    def paintEvent(self, _):
        if not hasattr(self, "_now"):
            return
        now, dt = self._now, self._dt
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        visible = self.settings.showCursor
        self._paint_string(p, now, dt, visible)
        self._paint_trail(p, visible)
        for born, life, paint in self._effects:
            if now >= born:
                p.save()
                paint(p, min(1.0, (now - born) / life))
                p.restore()
        if visible and self.engine.opacity > 0.005:
            self._paint_cursor(p, now)
        self._paint_label(p, now)
        self._paint_speech(p, now)
        p.end()

    def _paint_cursor(self, p, now):
        engine = self.engine
        side = engine.size
        dpr = self.devicePixelRatioF()
        if not self._art or self._art[0] != (side, dpr):
            self._art = ((side, dpr), *body_pixmap(side, dpr))
        pixmap, pad = self._art[1], self._art[2]
        k = side / 96
        p.save()
        p.setOpacity(engine.opacity)
        p.translate(engine.tip[0], engine.tip[1] + engine.hover(now))
        p.rotate(math.degrees(engine.angle))
        squish = engine.press_scale(now)
        p.scale(squish, squish)
        p.drawPixmap(QPointF(-pad, -pad), pixmap)
        blinking = now < engine.blink_until
        d = 18 * k
        for i in range(2):
            cx, cy = (34 + i * 24) * k + d / 2, 40 * k + d / 2
            p.save()
            p.translate(cx, cy)
            p.scale(1, 0.12 if blinking else 1)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("white"))
            p.drawEllipse(QPointF(0, 0), d / 2, d / 2)
            p.setBrush(color(INK))
            p.drawEllipse(QPointF(self._look[0] * 3.4 * k, self._look[1] * 3.4 * k), 4.5 * k, 4.5 * k)
            p.restore()
        p.restore()

    def _paint_trail(self, p, visible):
        speed = self._speed
        samples = self._samples
        if not (visible and self.settings.trail == "comet" and len(samples) > 2 and speed > 260):
            return
        # A soft tapered comet tail along the path it just flew.
        head = self.engine.size * 0.42
        left, right = [], []
        for i, (point, _) in enumerate(samples):
            before, after = samples[max(0, i - 1)][0], samples[min(len(samples) - 1, i + 1)][0]
            nx, ny = -(after[1] - before[1]), after[0] - before[0]
            length = max(math.hypot(nx, ny), 0.001)
            nx, ny = nx / length, ny / length
            w = head * (i / (len(samples) - 1)) ** 1.3 / 2
            left.append(QPointF(point[0] + nx * w, point[1] + ny * w))
            right.append(QPointF(point[0] - nx * w, point[1] - ny * w))
        p.save()
        p.setOpacity(min(1.0, (speed - 260) / 900) * 0.26 * self.engine.opacity)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color(BERRY2))
        p.drawPolygon(QPolygonF(left + right[::-1]))
        p.restore()

    def _paint_string(self, p, now, dt, visible):
        if not (visible and self.settings.trail == "string" and self.engine.opacity > 0.02):
            self._string_mid = None
            return
        # Like a balloon string held by the phone: it trails behind with a little slack and sway.
        w, h = self.size_tuple()
        start = (w * self.settings.phonePosition, h + 4)
        to = self.engine.body_center
        distance = math.hypot(to[0] - start[0], to[1] - start[1])
        slack = max(0.0, 160 - distance * 0.12)
        goal = ((start[0] + to[0]) / 2 + math.sin(now * 1.3) * 6, (start[1] + to[1]) / 2 + slack)
        mid = list(self._string_mid or goal)
        k = 22.0
        for i in range(2):
            self._string_velocity[i] += (k * (goal[i] - mid[i]) - 2 * math.sqrt(k) * 0.55 * self._string_velocity[i]) * dt
            mid[i] += self._string_velocity[i] * dt
        self._string_mid = tuple(mid)
        path = QPainterPath(QPointF(*start))
        path.quadTo(QPointF(2 * mid[0] - (start[0] + to[0]) / 2, 2 * mid[1] - (start[1] + to[1]) / 2), QPointF(*to))
        p.save()
        p.setOpacity(self.engine.opacity)
        pen = QPen(color(BERRY1, 0.7), 2.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.restore()

    # MARK: Effects (fire and forget)

    def _add_effect(self, life, paint):
        self._effects.append((time.perf_counter(), life, paint))

    def _ring(self, point, radius, value, alpha, duration):
        def paint(p, t):
            e = _RING_EASE.value(t)
            p.setOpacity(1 - e)
            p.setPen(QPen(color(value, alpha), 3.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            r = radius * (0.3 + 0.7 * e)
            p.drawEllipse(QPointF(*point), r, r)
        self._add_effect(duration, paint)

    def _star(self, point, radius, travel, value, duration):
        path = star_path(radius)
        spin = random.uniform(-2.5, 2.5)

        def paint(p, t):
            e = _STAR_EASE.value(t)
            p.setOpacity(1.0 if t < 0.45 else 1 - (t - 0.45) / 0.55)
            p.translate(point[0] + travel[0] * e, point[1] + travel[1] * e)
            p.rotate(math.degrees(spin * t))
            s = 1 - 0.6 * t
            p.scale(s, s)
            p.fillPath(path, color(value))
        self._add_effect(duration, paint)

    def _twinkle(self, point):
        self._star(point, random.uniform(3, 5.5), (random.uniform(-8, 8), random.uniform(6, 18)),
                   random.choice([BERRY1, BERRY2]), 0.5)

    def _soft_land(self, point):
        """Landing on something it's pointing at: a soft ring and a tiny nod."""
        self.engine.press(0.05)
        self._ring(point, 30, BERRY2, 0.55, 0.7)

    def _puff(self, point):
        """Popping out of (or diving back into) the phone at the bottom edge."""
        if not self.settings.showCursor:
            return
        self._ring(point, 26, BERRY1, 0.6, 0.55)
        for i in range(3):
            dx = (i - 1) * 14
            self._star((point[0] + dx, point[1] - 4), 4, (dx * 0.6, -28), BERRY1, 0.6)

    def click_effect(self, point, right=False):
        """A real click: a firm squish, a ring and a little burst of stars."""
        self.engine.press(0.14)
        self._ring(point, 38, BERRY3 if right else BERRY2, 0.85, 0.65)
        for i in range(6):
            a = i / 6 * 2 * math.pi + random.uniform(-0.25, 0.25)
            distance = random.uniform(34, 58)
            self._star(point, random.uniform(4, 7), (math.cos(a) * distance, math.sin(a) * distance),
                       random.choice([BERRY1, BERRY2, BERRY3]), 0.55)

    def typed_effect(self, text):
        """A typed character floating up from the cursor, so you can see him typing."""
        engine = self.engine
        if not self.settings.showCursor or engine.opacity <= 0.1:
            return
        engine.press(0.035)
        font = display_font(22)
        for i, ch in enumerate(text):
            if ch.isspace():
                continue
            start = (engine.tip[0] + engine.size * 0.55 + i * 6, engine.tip[1] - 6)
            drift = random.uniform(-12, 16)
            path = QPainterPath()
            path.addText(QPointF(-7, 8), font, ch)

            def paint(p, t, path=path, start=start, drift=drift):
                e = 1 - (1 - t) ** 2
                p.setOpacity(t / 0.12 if t < 0.12 else 1.0 if t < 0.55 else 1 - (t - 0.55) / 0.45)
                p.translate(start[0] + drift * e, start[1] - 46 * e)
                s = 1.1 - 0.35 * t
                p.scale(s, s)
                p.strokePath(path, QPen(QColor("white"), 3.5))
                p.fillPath(path, color(BERRY3))
            self._add_effect(0.9, paint)

    def bubble(self, text, life=1.4):
        """A little label next to the cursor, like "Ctrl+T" or "Opening Edge". The newest replaces any older one."""
        if self.settings.showCursor:
            self._label = (text, time.perf_counter(), life)

    def _paint_label(self, p, now):
        if not self._label:
            return
        text, born, life = self._label
        age = now - born
        if age < 0:
            return
        engine = self.engine
        font = display_font(19)
        metrics = QFontMetricsF(font)
        box_w, box_h = metrics.horizontalAdvance(text) + 28, metrics.height() + 14
        w, h = self.size_tuple()
        if engine.opacity > 0.1:
            x, y = engine.tip[0] + engine.size * 1.05, engine.tip[1] - engine.size * 0.1
        else:
            x, y = w * self.settings.phonePosition, h - engine.size * 1.3
        if x + box_w > w - 12:
            x = engine.tip[0] - box_w - engine.size * 0.3
        y -= min(age, 0.3) * 20
        pop = 1 - 0.6 * math.exp(-5.5 * age) * math.cos(8.3 * age)
        p.save()
        p.setOpacity(min(1.0, age / 0.12, (life - age) / 0.3))
        p.translate(x + box_w / 2, y)
        p.scale(pop, pop)
        box = QRectF(-box_w / 2, -box_h / 2, box_w, box_h)
        p.setPen(QPen(color(BERRY2, 0.9), 2))
        p.setBrush(color(INK, 0.92))
        p.drawRoundedRect(box, 14, 14)
        p.setFont(font)
        p.setPen(QColor("white"))
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
        p.restore()

    # MARK: Speech bubble

    def set_caption(self, caption):
        if caption == self._caption:
            return
        self._caption = caption
        now = time.perf_counter()
        if not caption:
            if self._speech and not self._speech["fading_since"]:
                self._speech["fading_since"] = now  # shrink away with a little pop
            return
        # Longer replies get a slightly smaller font and a wider bubble, so it never turns into a tower.
        length = len(caption)
        font = display_font(21 if length <= 50 else 19 if length <= 110 else 17)
        max_width = min(self.width() * 0.5, 580, max(240.0, math.sqrt(length) * 50))
        rect = QFontMetricsF(font).boundingRect(QRectF(0, 0, max_width, 800),
                                                Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignCenter, caption)
        pad = self._SPEECH_PAD
        size = (max(math.ceil(rect.width()) + pad[0] * 2, 70), math.ceil(rect.height()) + pad[1] * 2)
        appearing = not self._speech or self._speech["fading_since"]
        self._speech = {"text": caption, "size": size, "font": font, "fading_since": None,
                        "born": now if appearing else self._speech["born"]}

    def _paint_speech(self, p, now):
        """Follows his cursor every frame: above the thing he's pointing at (or below his cursor when there's
        no room up there), otherwise just above the phone."""
        speech = self._speech
        if not speech:
            return
        engine = self.engine
        w_screen, h_screen = self.size_tuple()
        w, h = speech["size"]
        tail, margin = self._TAIL, 14
        tail_up = False
        if engine.opacity > 0.1 and not engine.is_home:
            thing = self.speech_target or (engine.tip[0] - 12, engine.tip[1] - 30, 24, 24)
            target = [thing[0] + thing[2] / 2, thing[1] - 10]
            origin = [target[0] - w / 2, target[1] - tail - h]
            if origin[1] < margin:
                target = [engine.tip[0] + engine.size * 0.5, engine.tip[1] + engine.size * 1.35]
                origin = [target[0] - w / 2, target[1] + tail]
                tail_up = True
        else:
            target = [w_screen * self.settings.phonePosition, h_screen - engine.size * 0.9]
            origin = [target[0] - w / 2, target[1] - tail - h]
        origin[0] = min(max(origin[0], margin), w_screen - w - margin)
        origin[1] = min(max(origin[1], margin), h_screen - h - margin)
        age = now - speech["born"]
        bob = math.sin(age * 2.2) * 2
        origin[1] += bob
        target[1] += bob * 0.5
        path = self._speech_path(w, h, target[0] - origin[0], tail, tail_up)

        if speech["fading_since"]:
            t = min(1.0, (now - speech["fading_since"]) / 0.22)
            opacity, scale = 1 - t, 1 - 0.15 * t
        else:
            opacity, scale = min(1.0, max(age, 0.0) / 0.12), 1 - 0.45 * math.exp(-6 * max(age, 0.0)) * math.cos(15 * max(age, 0.0))
        p.save()
        p.setOpacity(opacity)
        p.translate(origin[0] + w / 2, origin[1] + h / 2)
        p.scale(scale, scale)
        p.translate(-w / 2, -h / 2)
        fill = QLinearGradient(0, 0, 0, h)
        fill.setColorAt(0, QColor("white"))
        fill.setColorAt(1, color(0xEEF0FF))
        pen = QPen(color(BERRY2), 2.5)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(fill)
        p.drawPath(path)
        p.setFont(speech["font"])
        p.setPen(color(INK))
        pad = self._SPEECH_PAD
        p.drawText(QRectF(pad[0], pad[1] - 1, w - pad[0] * 2, h - (pad[1] - 1) * 2),
                   Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignCenter, speech["text"])
        p.restore()

    @staticmethod
    def _speech_path(w, h, local_x, tail, tail_up):
        """The tail starts on the bottom (or top) edge, as close under the target as the corners allow, and
        leans toward it. Built y-up like the Swift path, then flipped."""
        r = min(24.0, h / 2)
        half = 13.0
        base = min(max(local_x, r + half + 2), w - r - half - 2)
        lean = min(max(local_x - base, -tail * 1.4), tail * 1.4)
        tip_y = h + tail if tail_up else -tail
        tip = QPointF(base + lean, tip_y)
        kappa = r * 0.4477
        path = QPainterPath(QPointF(r, 0))
        if not tail_up:
            path.lineTo(base - half, 0)
            path.cubicTo(QPointF(base - half * 0.3, 0), QPointF(tip.x() - 1, tip_y * 0.5), tip)
            path.cubicTo(QPointF(tip.x() + 2, tip_y * 0.45), QPointF(base + half * 0.4, 0), QPointF(base + half, 0))
        path.lineTo(w - r, 0)
        path.cubicTo(QPointF(w - kappa, 0), QPointF(w, kappa), QPointF(w, r))
        path.lineTo(w, h - r)
        path.cubicTo(QPointF(w, h - kappa), QPointF(w - kappa, h), QPointF(w - r, h))
        if tail_up:
            path.lineTo(base + half, h)
            path.cubicTo(QPointF(base + half * 0.3, h), QPointF(tip.x() + 1, h + tail * 0.5), tip)
            path.cubicTo(QPointF(tip.x() - 2, h + tail * 0.45), QPointF(base - half * 0.4, h), QPointF(base - half, h))
        path.lineTo(r, h)
        path.cubicTo(QPointF(kappa, h), QPointF(0, h - kappa), QPointF(0, h - r))
        path.lineTo(0, r)
        path.cubicTo(QPointF(0, kappa), QPointF(kappa, 0), QPointF(r, 0))
        path.closeSubpath()
        return QTransform(1, 0, 0, -1, 0, h).map(path)


class CursorOverlay:
    """A click-through, transparent window over the whole primary screen that shows the cursor."""

    def __init__(self):
        self.view = CursorView()
        self.on_face = None
        self._last = None
        self._timer = QTimer()
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)

    mode = property(lambda self: self.view.engine.mode, lambda self, value: setattr(self.view.engine, "mode", value))
    # Where the cursor rests when nobody is pointing: tucked away with eyes on your mouse, or parked above the phone.
    idle_mode = property(lambda self: "following" if link.settings.followMouse else "docked")

    def go_home(self):
        self.mode = self.idle_mode

    def talk_test(self, seconds=3.0):
        self.view.engine.talk_until = time.perf_counter() + seconds

    def start(self):
        self.go_home()
        view = self.view
        view.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
                            | Qt.WindowType.WindowTransparentForInput | Qt.WindowType.WindowDoesNotAcceptFocus)
        view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        view.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        screen = QGuiApplication.primaryScreen()
        screen.geometryChanged.connect(view.setGeometry)
        view.setGeometry(screen.geometry())
        view.show()
        self._timer.start(16)

    def _tick(self):
        # ponytail: 60 Hz only while something is drawn. Tucked away, 30 Hz is plenty for the phone's eyes.
        self._timer.setInterval(16 if self.view.active else 33)
        now = time.perf_counter()
        dt = 1 / 60 if self._last is None else min(max(now - self._last, 1 / 480), 1 / 20)
        self._last = now
        position = QCursor.pos() - self.view.geometry().topLeft()
        self.view.step(dt, now, (float(position.x()), float(position.y())))
        if self.on_face:
            self.on_face(self.view.engine.face(self.view.size_tuple(), now))
