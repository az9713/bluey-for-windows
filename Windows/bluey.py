"""Bluey for Windows: the tray app. Ports main.swift, AppDelegate.swift, HotKeys.swift, PairingPanel.swift,
ReportPanel.swift, FocusDiagnostics.swift, FocusLinkProbe.swift and the countdown in FocusCompanion.swift.

Two threads: Qt owns windows on the main thread, asyncio owns all logic on the other (Swift's @MainActor role)."""
import asyncio
import ctypes
import io
import json
import os
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import link

_WDA_EXCLUDEFROMCAPTURE = 0x11
_WM_HOTKEY, _WM_POWERBROADCAST, _PBT_SUSPEND, _PBT_SETTING = 0x0312, 0x0218, 0x0004, 0x8013
_MOD_ALT, _MOD_CONTROL, _MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
_FOCUS_COPY = ("On your iPhone, open Bluey and tap Pair Mac to scan this code. Keep the code private: it pairs your phone "
               "to this PC.\n\nDuring focus sessions, your main display is shared with OpenAI every 30 seconds. No screenshots "
               "are saved by Bluey. Pause or end to stop sharing.")


def run_app():
    from PySide6.QtCore import QAbstractNativeEventFilter, QByteArray, QEvent, QObject, QRectF, Qt, Signal, Slot
    from PySide6.QtGui import (QAction, QColor, QCursor, QFont, QFontDatabase, QGuiApplication, QIcon, QImage,
                               QLinearGradient, QPainter, QPen, QPixmap)
    from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                                   QMenu, QPlainTextEdit, QPushButton, QScrollArea, QSystemTrayIcon, QVBoxLayout, QWidget)
    import segno

    import codex_jobs
    import control
    import focus as focus_module
    import notes
    import overlay as overlay_module
    import realtime
    from overlay import BERRY2, INK, color

    def pixel_font(family, size, weight=None):
        font = QFont(family)
        font.setPixelSize(size)
        if weight:
            font.setWeight(weight)
        return font

    class Dispatcher(QObject):
        """Hops from the asyncio thread to the Qt thread. A named slot on a QObject guarantees the queued hop."""
        call = Signal(object)

        def __init__(self):
            super().__init__()
            self.call.connect(self.run, Qt.ConnectionType.QueuedConnection)

        @Slot(object)
        def run(self, fn):
            fn()

    class HideFromCapture(QObject):
        """Keeps every Bluey window, including the pairing QR code, out of screenshots sent to OpenAI."""

        def eventFilter(self, watched, event):
            if event.type() == QEvent.Type.Show and isinstance(watched, QWidget) and watched.isWindow():
                if not control.user32.SetWindowDisplayAffinity(wintypes.HWND(int(watched.winId())), _WDA_EXCLUDEFROMCAPTURE):
                    print(f"Bluey: {type(watched).__name__} could not be hidden from screenshots")
            return False

    class Countdown(QWidget):
        """A large white countdown at the bottom right, on top of everything and click-through."""

        def __init__(self):
            super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
                             | Qt.WindowType.WindowTransparentForInput | Qt.WindowType.WindowDoesNotAcceptFocus)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            self.resize(420, 132)
            layout = QVBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)
            self.mode = QLabel(alignment=Qt.AlignmentFlag.AlignRight)
            self.mode.setFont(pixel_font("Segoe UI", 12, QFont.Weight.Medium))
            self.clock = QLabel(alignment=Qt.AlignmentFlag.AlignRight)
            self.clock.setFont(pixel_font("IBM Plex Mono", 76, QFont.Weight.DemiBold))
            for label in (self.mode, self.clock):
                label.setStyleSheet("color: white; background: transparent;")
                layout.addWidget(label)

        def show_session(self, status, remaining, developer):
            if status is None or status == "ended":
                self.hide()
                return
            area = QGuiApplication.primaryScreen().availableGeometry()
            self.move(area.right() - 452, area.bottom() - 24 - 132)
            self.clock.setText(("Ⅱ " if status == "paused" else "") + focus_module.FocusSession.clock(remaining))
            self.mode.setText("DEVELOPER MODE · 4×" if developer else "")
            self.show()

    class ReportPanel(QWidget):
        """The research card. It shows up while he researches, then previews the first few lines. Click it to
        expand. It stays on screen until you press the minus button."""

        def __init__(self):
            super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
                             | Qt.WindowType.WindowDoesNotAcceptFocus)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            self.question, self.report, self.error, self.expanded = "", None, None, False
            self._press = None
            self._moved = False
            outer = QVBoxLayout(self)
            outer.setContentsMargins(22, 22, 22, 22)
            header = QHBoxLayout()
            self.title = QLabel(wordWrap=True)
            self.title.setFont(pixel_font("Fredoka", 20, QFont.Weight.Bold))
            self.title.setStyleSheet("color: #17151F; background: transparent;")  # the card stays light in dark mode
            header.addWidget(self.title, 1, Qt.AlignmentFlag.AlignTop)
            self.toggle_button = self._circle("⌃", self._toggle)
            header.addWidget(self.toggle_button, 0, Qt.AlignmentFlag.AlignTop)
            header.addWidget(self._circle("−", self.hide), 0, Qt.AlignmentFlag.AlignTop)
            outer.addLayout(header)
            self.body = QLabel(wordWrap=True, openExternalLinks=True, textFormat=Qt.TextFormat.RichText)
            self.body.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
            self.body.setFont(pixel_font("IBM Plex Sans", 15))
            self.body.setStyleSheet("color: #17151F; background: transparent;")
            scroll = QScrollArea(widgetResizable=True, frameShape=QScrollArea.Shape.NoFrame)
            scroll.viewport().setStyleSheet("background: transparent;")
            scroll.setStyleSheet("QScrollArea { background: transparent; }")
            scroll.setWidget(self.body)
            outer.addWidget(scroll, 1)

        def _circle(self, text, action):
            button = QPushButton(text)
            button.setFixedSize(26, 26)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setStyleSheet("QPushButton { background: #6C86F5; color: white; border-radius: 13px; font-weight: 900; }")
            button.clicked.connect(action)
            return button

        def show_loading(self, question):
            self.question, self.report, self.error, self.expanded = question, None, None, False
            self._refresh()

        def show_report(self, report):
            self.report, self.error = report, None
            self._refresh()

        def show_error(self, message):
            self.error = message
            self._refresh()

        def _toggle(self):
            self.expanded = not self.expanded
            self._refresh()

        def _refresh(self):
            from html import escape
            report = self.report
            self.title.setText(report.title if report else "Researching" if self.error is None else "Research")
            self.toggle_button.setVisible(self.expanded)
            if report and self.expanded:
                html = "".join(f"<p>{escape(paragraph)}</p>" for paragraph in report.paragraphs)
                if report.sources:
                    html += "<p style='font-family:\"IBM Plex Mono\"; font-size:11px; color:#5D5873'>SOURCES</p>"
                    html += "".join(f"<div><a style='color:#6C86F5' href='{escape(url, quote=True)}'>{escape(title)}</a></div>" for title, url in report.sources)
            elif report:
                preview = " ".join(report.paragraphs)
                html = f"{escape(preview[:190] + ('…' if len(preview) > 190 else ''))}<p style='font-family:\"IBM Plex Mono\"; font-size:11px; color:#6C86F5'>Click to read more</p>"
            elif self.error:
                html = escape(self.error)
            else:
                html = f"<span style='color:#5D5873'>Doing some research on “{escape(self.question)}”</span>"
            self.body.setText(html)
            area = QGuiApplication.primaryScreen().availableGeometry()
            width = 520 if self.expanded else 400
            if self.expanded and report:
                # Roughly fit the text (about 58 characters a line), up to most of the screen. It scrolls past that.
                lines = sum(len(paragraph) // 58 + 1 for paragraph in report.paragraphs)
                content = lines * 21 + len(report.paragraphs) * 12 + (30 + len(report.sources) * 22 if report.sources else 0)
                height = min(int(area.height() * 0.75), 110 + content)
            else:
                height = 190 if report else 140
            self.setGeometry(area.right() - width - 24, area.top() + 24, width, height)
            self.show()

        def paintEvent(self, _):
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            box = QRectF(self.rect()).adjusted(5.25, 5.25, -5.25, -5.25)
            fill = QLinearGradient(0, 0, 0, self.height())
            fill.setColorAt(0, QColor("white"))
            fill.setColorAt(1, color(0xEEF0FF))
            p.setBrush(fill)
            p.setPen(QPen(color(BERRY2), 2.5))
            p.drawRoundedRect(box, 22, 22)

        # The card moves when dragged, and a plain click opens the preview.
        def mousePressEvent(self, event):
            self._press = event.globalPosition().toPoint() - self.pos()
            self._moved = False

        def mouseMoveEvent(self, event):
            if self._press is not None:
                target = event.globalPosition().toPoint() - self._press
                if (target - self.pos()).manhattanLength() > 3:
                    self._moved = True
                    self.move(target)

        def mouseReleaseEvent(self, _):
            if not self._moved and self.report and not self.expanded:
                self._toggle()
            self._press = None

    class PairingPanel(QWidget):
        def __init__(self):
            super().__init__(None, Qt.WindowType.Window)
            self.setWindowTitle("Pair Bluey")
            self.setFixedSize(440, 510)
            layout = QVBoxLayout(self)
            title = QLabel("Your little focus friend", alignment=Qt.AlignmentFlag.AlignCenter)
            title.setFont(pixel_font("Segoe UI", 23, QFont.Weight.DemiBold))
            layout.addWidget(title)
            buffer = io.BytesIO()
            segno.make(link.qr_text(link.device_name(), link.pairing_secret()), error="m").save(buffer, kind="png", scale=10, border=4)
            image = QImage.fromData(buffer.getvalue())
            code = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
            code.setPixmap(QPixmap.fromImage(image).scaled(300, 300, Qt.AspectRatioMode.KeepAspectRatio))
            layout.addWidget(code)
            copy = QLabel(_FOCUS_COPY, wordWrap=True)
            copy.setFont(pixel_font("Segoe UI", 13))
            layout.addWidget(copy)

    def menu_icon(dim):
        """The little blueberry blob with eyes for the tray."""
        size = 64
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        p = QPainter(pixmap)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(0.45 if dim else 1)
        gradient = QLinearGradient(0, 0, size, size)
        for stop, value in overlay_module.GRADIENT:
            gradient.setColorAt(stop, color(value))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(gradient)
        p.drawEllipse(3, 6, 58, 52)
        for x in (15, 34):
            p.setBrush(QColor("white"))
            p.drawEllipse(x, 19, 16, 16)
            p.setBrush(color(INK))
            p.drawEllipse(x + 4, 21, 9, 9)
        p.end()
        return QIcon(pixmap)

    class App(QAbstractNativeEventFilter):
        def __init__(self, qt):
            super().__init__()
            self.qt = qt
            self.settings = link.settings
            self.dispatcher = Dispatcher()
            self.ui = self.dispatcher.call.emit
            self.loop = asyncio.new_event_loop()
            threading.Thread(target=self.loop.run_forever, daemon=True).start()
            self.overlay = overlay_module.CursorOverlay()
            self.countdown = Countdown()
            self.reports = ReportPanel()
            self.pairing = None
            self.server = link.PhoneServer(link.pairing_secret())
            self.codex = codex_jobs.CodexController()

            def show_countdown(session):
                status, remaining, developer = (session.status, session.remaining, session.is_developer_mode) if session else (None, 0, False)
                self.ui(lambda: self.countdown.show_session(status, remaining, developer))
            self.focus = focus_module.FocusCompanion(on_countdown=show_countdown)
            self.host = realtime.RealtimeHost(self.overlay, self.codex, self.reports, self.focus, self.ui)
            self.tray = QSystemTrayIcon(menu_icon(True))
            self.tray.setToolTip("Bluey")
            self.menu = QMenu()
            self.menu.aboutToShow.connect(self.build_menu)
            self.tray.setContextMenu(self.menu)
            self.tray.activated.connect(self._tray_clicked)
            self.hotkeys = {}

        def logic(self, fn):
            """Runs fn on the asyncio thread."""
            self.loop.call_soon_threadsafe(fn)

        def start(self):
            self.tray.show()
            self.overlay.on_face = lambda face: self.loop.call_soon_threadsafe(self.server.send_face, face)
            self.overlay.start()

            def on_change(state):
                self.server.broadcast({"command": "focusState", "text": json.dumps(state)})

            def on_nudge(message, target):
                self.host.focus_nudge(message, target)
                self.server.broadcast({"command": "focusChirp", "text": message})

            def phones_changed(names):
                self.ui(lambda: self.tray.setIcon(menu_icon(not names)))
                if not names:
                    self.focus.disconnected()
                    if self.host.awake:
                        self.host.set_awake(False)

            self.focus.on_change, self.focus.on_nudge = on_change, on_nudge
            self.server.on_phones_changed = phones_changed
            self.server.on_request = self.on_request
            self.codex.on_event = self.on_codex_event

            async def begin():
                self.focus.start_service()
                await self.server.start()
                self.codex.warm_up()
            asyncio.run_coroutine_threadsafe(begin(), self.loop)
            self.register_hotkeys()
            self.watch_display()
            if "--pair" in sys.argv:
                self.show_pairing()
            if not link.get_key():
                self.edit_key()

        def on_request(self, packet, reply):
            command = packet.get("command") or ""
            if not command.startswith("focus"):
                self.host.handle(packet, reply)
                return
            focus = self.focus
            focus.heartbeat()
            try:
                args = json.loads(packet.get("text") or "{}")
                args = args if isinstance(args, dict) else {}
            except ValueError:
                args = {}
            result = ""
            if command == "focusStart":
                minutes = args.get("minutes", 30.0)
                result = focus.start(str(args.get("task") or ""), minutes if isinstance(minutes, (int, float)) else 30.0)
            elif command == "focusPause":
                result = focus.pause()
            elif command == "focusResume":
                result = focus.resume()
            elif command == "focusEnd":
                result = focus.end()
            elif command == "focusTestNudge":
                result = focus.test_nudge()
            elif command == "focusDeveloperMode":
                enabled = args.get("developerMode")
                result = focus.set_developer_mode(enabled) if isinstance(enabled, bool) else "Specify whether Developer Mode is on or off."
            elif command == "focusSync":
                focus.sync()
            reply({"command": "focusReply", "callID": packet.get("callID"), "text": result})

        def on_codex_event(self, note, line):
            """A Codex job finished. Awake, he hears about it and says so himself. Asleep, the bubble shows here
            and the phone chirps."""
            if self.host.awake and self.server.phone_names:
                self.server.broadcast({"command": "codexNote", "text": note})
                return

            async def tell():
                text = await codex_jobs.write_line(note, realtime.personality()) or line
                self.host.show_reply(text)
                self.server.broadcast({"command": "codexChirp", "text": text})
            self.loop.create_task(tell())

        # MARK: Windows events

        def register_hotkeys(self):
            # Ctrl+Alt+W replaces the Mac's Option+Space: Alt+Space opens the Windows system menu, and other apps often hold Ctrl+Alt+Space.
            bindings = {ord("W"): self.toggle_awake, ord("P"): self.point_here, ord("F"): self.toggle_follow,
                        ord("D"): self.overlay.go_home, ord("H"): self.toggle_show, ord("T"): self.overlay.talk_test,
                        ord("S"): self.stop_actions}
            for number, (vk, action) in enumerate(bindings.items(), 1):
                if control.user32.RegisterHotKey(None, number, _MOD_CONTROL | _MOD_ALT | _MOD_NOREPEAT, vk):
                    self.hotkeys[number] = action
                else:
                    print(f"Bluey: hotkey Ctrl+Alt+{chr(vk)} is taken by another app")
            self.qt.installNativeEventFilter(self)

        def watch_display(self):
            """The display turning off pauses focus, like NSWorkspace.screensDidSleepNotification on the Mac."""
            class GUID(ctypes.Structure):
                _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]
            console_display = GUID(0x6FE69556, 0x704A, 0x47A0, (ctypes.c_ubyte * 8)(0x8F, 0x24, 0xC2, 0x8D, 0x93, 0x6F, 0xDA, 0x47))
            control.user32.RegisterPowerSettingNotification.restype = wintypes.HANDLE
            control.user32.RegisterPowerSettingNotification(wintypes.HANDLE(int(self.overlay.view.winId())), ctypes.byref(console_display), 0)

        def nativeEventFilter(self, event_type, message):
            if event_type != QByteArray(b"windows_generic_MSG") and event_type != b"windows_generic_MSG":
                return False, 0
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == _WM_HOTKEY:
                action = self.hotkeys.get(msg.wParam)
                if action:
                    action()
            elif msg.message == _WM_POWERBROADCAST:
                sleeping = msg.wParam == _PBT_SUSPEND
                if msg.wParam == _PBT_SETTING:
                    # POWERBROADCAST_SETTING: a 16-byte GUID, a 4-byte length, then the data. 0 means display off.
                    sleeping = ctypes.c_ulong.from_address(msg.lParam + 20).value == 0
                if sleeping:
                    self.logic(lambda: self.focus.pause("PC sleeping or locked. Resume when you're ready."))
            return False, 0

        # MARK: Actions

        def toggle_awake(self):
            self.logic(lambda: self.server.broadcast({"command": "sleep" if self.host.awake else "wake"}))

        def restart_if_awake(self):
            """Wakes him again so a new personality or set of tools takes effect right away."""
            def go():
                if self.host.awake:
                    self.server.broadcast({"command": "sleep"})
                    self.loop.call_later(1, lambda: self.server.broadcast({"command": "wake"}))
            self.logic(go)

        def point_here(self):
            position = QCursor.pos() - self.overlay.view.geometry().topLeft()
            self.overlay.mode = ("pinned", (float(position.x()), float(position.y())))

        def toggle_follow(self):
            self.settings.followMouse = not self.settings.followMouse
            self.overlay.go_home()

        def toggle_show(self):
            self.settings.showCursor = not self.settings.showCursor

        def stop_actions(self):
            def go():
                self.focus.pause("Stopped with Ctrl+Alt+S")
                self.host.stop_actions()
            self.logic(go)

        def toggle_computer_control(self):
            self.settings.computerControl = not self.settings.computerControl
            self.restart_if_awake()  # his tools change with this switch

        def show_pairing(self):
            if not self.pairing:
                self.pairing = PairingPanel()
            self.pairing.show()
            self.pairing.activateWindow()

        def edit_personality(self):
            dialog = QDialog()
            dialog.setWindowTitle("His Personality")
            dialog.resize(520, 320)
            layout = QVBoxLayout(dialog)
            layout.addWidget(QLabel("Describe who he is and how he talks. How he looks at and points at your screen stays built in.", wordWrap=True))
            text = QPlainTextEdit(realtime.personality())
            layout.addWidget(text)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Reset)
            reset = {"pressed": False}

            def reset_clicked():
                reset["pressed"] = True
                dialog.accept()
            buttons.button(QDialogButtonBox.StandardButton.Reset).setText("Reset to Default")
            buttons.button(QDialogButtonBox.StandardButton.Reset).clicked.connect(reset_clicked)
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            if dialog.exec():
                self.settings.personality = "" if reset["pressed"] else text.toPlainText()
                self.restart_if_awake()

        def edit_key(self):
            dialog = QDialog()
            dialog.setWindowTitle("OpenAI API Key")
            dialog.resize(420, 130)
            layout = QVBoxLayout(dialog)
            layout.addWidget(QLabel("Saved privately on this PC (not in the project files). The phone only ever gets short-lived keys.", wordWrap=True))
            field = QLineEdit(echoMode=QLineEdit.EchoMode.Password)
            field.setPlaceholderText("Key saved. Paste a new one to replace it." if link.get_key() else "sk-…")
            layout.addWidget(field)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            if dialog.exec() and field.text().strip():
                link.set_key(field.text().strip())

        def add_codex_folder(self):
            path = QFileDialog.getExistingDirectory(None, "Folder Codex can work in when you ask Bluey")
            if path and path not in self.settings.codexFolders:
                self.settings.codexFolders = list(self.settings.codexFolders) + [str(Path(path))]
                codex_jobs.forget_projects()

        def open_notes(self):
            notes.root().mkdir(parents=True, exist_ok=True)
            os.startfile(notes.root())

        def quit(self):
            def done():
                self.focus.shutdown()
                self.codex.shutdown()
                self.ui(self.qt.quit)
            self.logic(done)

        def _tray_clicked(self, reason):
            if reason == QSystemTrayIcon.ActivationReason.Trigger:
                self.menu.popup(QCursor.pos())

        # MARK: Menu

        def build_menu(self):
            menu = self.menu
            menu.clear()
            settings = self.settings

            def item(title, action=None, shortcut="", checked=None, parent=None, enabled=True):
                entry = QAction(title + (f"\tCtrl+Alt+{shortcut}" if shortcut else ""), menu)
                if action:
                    entry.triggered.connect(lambda _=False: action())
                if checked is not None:
                    entry.setCheckable(True)
                    entry.setChecked(checked)
                entry.setEnabled(enabled)
                (parent or menu).addAction(entry)
                return entry

            phones = self.server.phone_names
            item(f"Phone connected: {', '.join(phones)}" if phones else "Waiting for the phone app…", enabled=False)
            item("Pair iPhone (QR)…", self.show_pairing)
            session = self.focus.current
            if session and session.status != "ended":
                item(f"Focus: {session.task} · {focus_module.FocusSession.clock(session.remaining)}", enabled=False)
                if session.status == "running":
                    item("Pause Focus", lambda: self.logic(self.focus.pause))
                else:
                    item("Resume Focus", lambda: self.logic(self.focus.resume))
                item("End Focus", lambda: self.logic(self.focus.end))
            item("Developer Mode (4× · checks every 10s)",
                 lambda: self.logic(lambda: self.focus.set_developer_mode(not self.focus.developer_mode)), checked=self.focus.developer_mode)
            item("Go to Sleep (Follow Mode)" if self.host.awake else "Wake Up and Talk", self.toggle_awake, "W", enabled=bool(phones))
            menu.addSeparator()
            item("Let Him Use the Computer", self.toggle_computer_control, checked=settings.computerControl)
            item("Stop Him", self.stop_actions, "S")
            menu.addSeparator()
            item("Point Here", self.point_here, "P")
            item("Eyes Follow My Mouse", self.toggle_follow, "F", checked=settings.followMouse)
            item("Stop Pointing", self.overlay.go_home, "D")
            item("Talk Test", self.overlay.talk_test, "T")
            menu.addSeparator()

            def choices(title, options, current, apply):
                sub = menu.addMenu(title)
                for label, value in options:
                    item(label, lambda v=value: apply(v), checked=current == value, parent=sub)

            choices("Mood", [(m.capitalize(), m) for m in ("listening", "resting", "thinking", "happy")], settings.mood,
                    lambda v: setattr(settings, "mood", v))
            choices("Cursor Size", [(f"{n} ({s} pt)", float(s)) for n, s in (("Small", 48), ("Medium", 72), ("Large", 96), ("Huge", 120))],
                    float(settings.cursorSize), lambda v: setattr(settings, "cursorSize", v))
            choices("Phone Sits Under", [("Left", 0.2), ("Center", 0.5), ("Right", 0.8)], settings.phonePosition,
                    lambda v: setattr(settings, "phonePosition", v))
            if self.codex.is_installed:
                sub = menu.addMenu("Codex")
                for job in reversed(self.codex.jobs[-8:]):
                    item(f"{job.project}: {codex_jobs.short(job.prompt, 40)} ({job.status})",
                         lambda t=job.thread_id: codex_jobs.CodexController.open_thread(t), parent=sub)
                if self.codex.jobs:
                    sub.addSeparator()
                item("Add Project Folder…", self.add_codex_folder, parent=sub)
                item("Open Codex", lambda: os.startfile("codex://"), parent=sub)
            item("Open Meeting Notes", self.open_notes)
            item("Personality…", self.edit_personality)
            item("OpenAI Key…", self.edit_key)
            menu.addSeparator()
            choices("Pointer Trail", [("Comet Trail", "comet"), ("String to the Phone", "string"), ("No Trail", "none")], settings.trail,
                    lambda v: setattr(settings, "trail", v))
            item("Show Cursor", self.toggle_show, "H", checked=settings.showCursor)
            menu.addSeparator()
            item("Quit Bluey", self.quit)

    qt = QApplication(sys.argv)
    qt.setQuitOnLastWindowClosed(False)
    if link.FONTS_DIR.is_dir():
        for font in link.FONTS_DIR.glob("*.ttf"):
            QFontDatabase.addApplicationFont(str(font))
    hide = HideFromCapture()
    qt.installEventFilter(hide)
    app = App(qt)
    app.start()
    return qt.exec()


# MARK: Developer probes. These use the running app's real transport, or synthetic images, never a phone.

def _synthetic_jpeg(text):
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (960, 600), (26, 26, 26))
    ImageDraw.Draw(image).multiline_text((50, 100), text, fill="white", font_size=36)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG")
    return buffer.getvalue()


def focus_diagnostics(real_screen):
    """--focus-api-test sends synthetic images to OpenAI. --focus-screen-test sends your real desktop once."""
    import focus as focus_module
    import screen

    async def run():
        if real_screen:
            shot = await asyncio.to_thread(screen.snapshot)
            answer = await focus_module.classify(shot, focus_module.FocusSession("Build the Bluey focus friend PC and iPhone apps"), 0)
            print(f"LIVE desktop capture + Decisions: {answer.activity}, P(off)={answer.probability}, confidence={answer.confidence}. No screenshot saved.")
            return 0
        for name, title, task in (
            ("supporting_tutorial", "CODEX TUTORIAL\nHow to build an app with Codex\nResearch for my next video", "Make a video teaching Codex"),
            ("unrelated_entertainment", "CAT COMPILATION\nFunny cats on trampolines\nUp next: more cat videos", "Prepare the monthly business expense report"),
            ("twitter_unrelated", "X / HOME / FOR YOU\nFunny football moments\nCelebrity drama and memes\nTrending: weekend plans\nScroll for more entertainment", "Work on the Bluey app inside the Codex app"),
            ("twitter_research", "X / SEARCH: SwiftUI Bonjour\nFix NWConnection pairing in SwiftUI\nTechnical guide to Bonjour discovery\nCheck service names and TXT records", "Fix Bonjour pairing in the Bluey SwiftUI app"),
        ):
            lines = [(screen.Target(f"L{i}", text, screen.Rect(50, 100 + i * 45, 860, 40)), []) for i, text in enumerate(title.split("\n"), 1)] if name.startswith("twitter") else []
            shot = screen.ScreenSnapshot(_synthetic_jpeg(title), lines, (960.0, 600.0), "Edge" if name.startswith("twitter") else None)
            result = await focus_module.classify(shot, focus_module.FocusSession(task), 120)
            print(f"LIVE synthetic fixture {name}: {result.activity}, P(off)={result.probability}, confidence={result.confidence}")
            if name == "twitter_unrelated" and (result.activity != "off_task" or result.probability < 0.5):
                return 1
            if name == "twitter_research" and result.activity != "on_task":
                return 1
            if name == "unrelated_entertainment":
                message, target = await focus_module.prepare_nudge(shot, task)
                print(f"LIVE Responses nudge: {message}; target={'validated' if target else 'none'}")
        return 0

    try:
        return asyncio.run(run())
    except Exception as error:
        print(f"LIVE API unavailable: {error}")
        return 1


async def _discover(secret, timeout=15):
    """Finds the running app through real mDNS, like the phone's NWBrowser. Returns (host, port)."""
    from zeroconf import ServiceBrowser, ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf
    zeroconf = AsyncZeroconf()
    found = asyncio.get_running_loop().create_future()

    async def resolve(name):
        info = AsyncServiceInfo(link.SERVICE_TYPE, name)
        await info.async_request(zeroconf.zeroconf, 3000)
        if (info.properties or {}).get(b"bluey-id") == link.identifier(secret).encode() and info.parsed_addresses() and not found.done():
            found.set_result((info.parsed_addresses()[0], info.port))

    def changed(zeroconf=None, service_type=None, name=None, state_change=None):
        if state_change is ServiceStateChange.Added:
            asyncio.ensure_future(resolve(name))

    browser = AsyncServiceBrowser(zeroconf.zeroconf, link.SERVICE_TYPE, handlers=[changed])
    try:
        return await asyncio.wait_for(found, timeout)
    finally:
        await browser.async_cancel()
        await zeroconf.async_close()


def focus_link_probe(developer_test, nudge_test):
    """Plays the phone against an already running Bluey: real discovery, encryption, clock, capture and API path."""
    import focus as focus_module
    task = "Bluey integration test: build the focus friend PC and iPhone apps"
    secret = link.pairing_secret()

    async def run():
        host, port = await _discover(secret)
        print(f"LIVE app discovery: found {host}:{port} through mDNS")
        reader, writer = await asyncio.open_connection(host, port)
        connection = link.LineConnection(reader, writer, secret, server=False)
        done = asyncio.get_running_loop().create_future()
        state = {"phase": "discover", "previous": False, "checked": False, "paused_elapsed": 0.0, "paused_at": None, "resumed": 0.0}

        def finish(code, note):
            if not done.done():
                print(f"LIVE app integration: {note}")
                done.set_result(code)

        def send(command):
            text = None
            if command == "focusStart":
                text = json.dumps({"task": task, "minutes": 2 if developer_test else 1})
            elif command == "focusDeveloperMode":
                text = json.dumps({"developerMode": state["previous"] if state["phase"] == "restoring" else True})
            connection.send({"command": command, **({"text": text} if text else {})})

        def authenticated():
            print("LIVE app transport: authenticated")
            state["phase"] = "initial"
            connection.send({"hello": "Bluey integration probe"})
            send("focusSync")

        def receive(packet):
            phase = state["phase"]
            if nudge_test and phase == "testNudge" and packet.get("command") == "focusChirp":
                finish(0, f"test nudge dispatched through the real bubble and phone chirp path: {packet.get('text')}")
                return
            if packet.get("command") != "focusState":
                return
            sync = json.loads(packet["text"])
            current = sync.get("current")
            if phase == "initial":
                if nudge_test:
                    if not sync.get("developerMode"):
                        finish(2, "enable Developer Mode first")
                    else:
                        state["phase"] = "testNudge"
                        send("focusTestNudge")
                    return
                if current and current["status"] != "ended":
                    finish(2, "active user session; skipped")
                    return
                state["previous"] = bool(sync.get("developerMode"))
                state["phase"] = "configuring" if developer_test else "starting"
                send("focusDeveloperMode" if developer_test else "focusStart")
                return
            if phase == "configuring" and sync.get("developerMode"):
                print("LIVE Developer Mode: enabled and synchronized")
                state["phase"] = "starting"
                send("focusStart")
                return
            if phase == "restoring" and bool(sync.get("developerMode")) == state["previous"]:
                finish(0, "4x clock and ten-second checks verified; original setting restored")
                return
            if not current or current["task"] != task:
                return
            if phase == "starting" and current["status"] == "running":
                state["phase"] = phase = "running"
                print("LIVE app focus: started")
            last = current["checks"][-1] if current["checks"] else None
            if phase == "running" and last and last["activity"] != "paused":
                state["checked"] = last["activity"] in ("on_task", "off_task", "uncertain")
                print(f"LIVE scheduled check at {int(last['elapsed'])}s: {last['activity']}")
                if developer_test:
                    if not (current.get("developerMode") and last.get("sampleDuration") == 40 and 39 <= last["elapsed"] < 60
                            and abs(current["elapsed"] - current["realElapsed"] * 4) < 0.1):
                        finish(1, "unexpected Developer Mode timing")
                        return
                    print(f"LIVE 4x clock: observation at {last['elapsed'] / 4:.1f} real seconds")
                state["phase"] = "pausing"
                send("focusPause")
            elif phase == "pausing" and current["status"] == "paused":
                state.update(paused_elapsed=current["elapsed"], paused_at=time.monotonic(), phase="paused")
            elif phase == "paused":
                if abs(current["elapsed"] - state["paused_elapsed"]) > 0.05:
                    finish(1, "pause advanced the clock")
            elif phase == "resuming" and current["status"] == "running":
                print("LIVE app focus: resumed")
                state.update(resumed=current["elapsed"], phase="resumed")
            elif phase == "resumed" and current["elapsed"] - state["resumed"] >= 2:
                state["phase"] = "ending"
                send("focusEnd")
            elif phase == "ending" and current["status"] == "ended":
                if not any(s["id"] == current["id"] for s in sync["history"]) or not state["checked"]:
                    finish(1, "ended, but no valid live check/history")
                elif developer_test:
                    state["phase"] = "restoring"
                    send("focusDeveloperMode")
                else:
                    finish(0, "pause/resume/end synchronized; history contains the live check")

        connection.on_authenticated, connection.on_packet = authenticated, receive
        connection.on_closed = lambda: finish(1, f"connection closed in {state['phase']}")
        connection.start()
        began = time.monotonic()
        while not done.done():
            await asyncio.sleep(1)
            send("focusPing")
            if state["phase"] == "paused" and time.monotonic() - state["paused_at"] >= 3:
                print("LIVE app clock: paused without advancing")
                state["phase"] = "resuming"
                send("focusResume")
            if time.monotonic() - began > 80:
                finish(1, f"timed out in {state['phase']}")
        connection.cancel()
        return done.result()

    return asyncio.run(run())


def main():
    args = sys.argv
    if "--focus-link-test" in args or "--focus-developer-test" in args or "--focus-nudge-test" in args:
        return focus_link_probe("--focus-developer-test" in args, "--focus-nudge-test" in args)
    if "--focus-api-test" in args or "--focus-screen-test" in args:
        return focus_diagnostics("--focus-screen-test" in args)
    return run_app()


if __name__ == "__main__":
    sys.exit(main())
