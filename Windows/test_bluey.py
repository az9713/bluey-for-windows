"""Deterministic checks, no real API calls. Ports Tests/ and pins the JSON the Swift phone decodes.
Run: .venv\\Scripts\\python test_bluey.py"""
import asyncio
import base64
import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

_home = tempfile.TemporaryDirectory()
os.environ["BLUEY_HOME"] = _home.name
sys.path.insert(0, str(Path(__file__).resolve().parent))

import codex_jobs
import control
import focus as f
import link
import notes
import realtime
import screen
from focus import FocusCheck, FocusCompanion, FocusDecision, FocusNudgePolicy, FocusSession

ANSWER = '{"answers":[{"name":"activity","type":"choice","choice":"%s","confidence":%s,"probabilities":[{"value":"on_task","probability":%s},{"value":"off_task","probability":%s},{"value":"uncertain","probability":%s}]}]}'
OFF = FocusDecision.decode((ANSWER % ("off_task", 0.2, 0.005, 0.99, 0.005)).encode())
ON = FocusDecision.decode((ANSWER % ("on_task", 0.95, 0.95, 0.02, 0.03)).encode())


def off(elapsed, strength=0.99):
    return FocusCheck(elapsed, "off_task", strength, strength)


class FocusTests(unittest.TestCase):
    def test_developer_clock_runs_four_times_faster_and_pause_still_freezes_it(self):
        s = FocusSession("Test Bluey", 60, True)
        s.advance(1)
        self.assertEqual((s.elapsed, s.real_active_elapsed, s.check_interval, s.observation_duration), (4, 1, 10, 40))
        s.pause("Testing")
        s.advance(100)
        self.assertEqual((s.elapsed, s.real_active_elapsed), (4, 1))
        s.resume()
        s.advance(20)
        self.assertEqual((s.status, s.elapsed, s.real_active_elapsed), ("ended", 60, 15))  # expiry clamps both clocks

    def test_changing_speed_preserves_both_clocks_and_marks_test_history(self):
        s = FocusSession("Test speed changes")
        s.advance(10)
        s.set_developer_mode(True)
        s.advance(10)
        self.assertEqual((s.elapsed, s.real_active_elapsed), (50, 20))
        s.set_developer_mode(False)
        s.advance(10)
        self.assertEqual((s.elapsed, s.real_active_elapsed, s.used_developer_mode, s.clock_rate), (60, 30, True, 1))
        old = s.to_json()
        for key in ("developerMode", "usedDeveloperMode", "realElapsed"):
            del old[key]
        legacy = FocusSession.from_json(old)
        self.assertFalse(legacy.is_developer_mode)
        self.assertEqual((legacy.clock_rate, legacy.real_active_elapsed), (1, legacy.elapsed))

    def test_developer_observations_use_accelerated_nudge_time_and_still_reset(self):
        p = FocusNudgePolicy(60, True)
        self.assertFalse(p.observe(off(40), 1))
        self.assertTrue(p.observe(off(80), 1))  # second check at 20 real seconds
        self.assertFalse(p.observe(off(120), 1))
        self.assertFalse(p.observe(off(160), 1))  # cooldown still active
        self.assertTrue(p.observe(off(200), 1))
        self.assertFalse(p.observe(off(240), 21))  # stale uses real seconds
        gap = FocusNudgePolicy(60, True)
        self.assertFalse(gap.observe(off(40), 1))
        self.assertFalse(gap.observe(off(120), 1))  # missed check resets the run

    def test_overall_confidence_is_not_a_second_off_task_probability_veto(self):
        p = FocusNudgePolicy(60, True)
        for t in (40.0, 80):
            self.assertFalse(p.observe(FocusCheck(t, "off_task", 0.56, 0.34), 1))
        self.assertTrue(p.observe(FocusCheck(120, "off_task", 0.56, 0.34), 1))
        normal = FocusNudgePolicy()
        for t in range(0, 181, 30):
            self.assertFalse(normal.observe(FocusCheck(t, "off_task", 0.7, 0.1), 1))
        self.assertTrue(normal.observe(FocusCheck(210, "off_task", 0.7, 0.1), 1))

    def test_two_minute_minimum_and_cooldown(self):
        p = FocusNudgePolicy()
        for t in range(30, 121, 30):
            self.assertFalse(p.observe(off(t), 1))
        self.assertTrue(p.observe(off(150), 1))
        for t in range(180, 271, 30):
            self.assertFalse(p.observe(off(t), 1))
        self.assertTrue(p.observe(off(300), 1))

    def test_moderate_evidence_waits_three_to_four_minutes(self):
        p = FocusNudgePolicy()
        for t in range(0, 181, 30):
            self.assertFalse(p.observe(off(t, 0.65), 2))
        self.assertTrue(p.observe(off(210, 0.65), 2))
        weak = FocusNudgePolicy()
        for t in range(0, 211, 30):
            self.assertFalse(weak.observe(off(t, 0.5), 1))
        self.assertTrue(weak.observe(off(240, 0.5), 1))

    def test_on_task_uncertain_unavailable_and_pause_reset_evidence(self):
        for activity in ("on_task", "uncertain", "unavailable", "paused"):
            p = FocusNudgePolicy()
            for t in (0, 30, 60, 90):
                self.assertFalse(p.observe(off(t), 1))
            self.assertFalse(p.observe(FocusCheck(120, activity), 1))
            for t in (150, 180, 210, 240):
                self.assertFalse(p.observe(off(t), 1))
            self.assertTrue(p.observe(off(270), 1))

    def test_stale_gap_and_non_majority_probability_do_not_accumulate(self):
        p = FocusNudgePolicy()
        for t in (0, 30, 60, 90):
            p.observe(off(t), 1)
        self.assertFalse(p.observe(off(120), 21))
        self.assertFalse(p.observe(off(150), 1))
        self.assertFalse(p.observe(off(240), 1))  # missing checks reset the span
        low = off(270)
        low.probability = 0.49
        self.assertFalse(p.observe(low, 1))
        self.assertFalse(p.observe(off(300), 1))
        self.assertFalse(p.observe(off(330), -1))
        self.assertFalse(p.observe(off(math.nan), 1))

    def test_pause_resume_expiry_and_round_trip(self):
        s = FocusSession("Make a Codex video", 60)
        s.advance(30)
        s.pause("Locked")
        s.advance(900)
        self.assertEqual(s.elapsed, 30)
        s.resume()
        s.advance(31)
        self.assertEqual((s.elapsed, s.status), (60, "ended"))
        s.advance(500)
        self.assertEqual(s.elapsed, 60)
        self.assertEqual(FocusSession.from_json(json.loads(json.dumps(s.to_json()))).to_json(), s.to_json())
        self.assertEqual(FocusSession("x").duration, 1800)

    def test_choice_contract_and_malformed_refusal(self):
        good = ANSWER % ("off_task", 0.7, 0.1, 0.8, 0.1)
        result = FocusDecision.decode(good.encode())
        self.assertEqual((result.probability, result.confidence), (0.8, 0.7))
        self.assertEqual(result.probabilities, {"on_task": 0.1, "off_task": 0.8, "uncertain": 0.1})
        for bad in ('{"answers":[{"type":"refusal","name":"activity"}]}', "{}", good.replace("0.8", "1.8"), "not json"):
            with self.assertRaises(f.DecodeError):
                FocusDecision.decode(bad.encode())

    def test_choice_estimates_survive_and_older_history_does_not_invent_values(self):
        check = FocusCheck(30, "on_task", 0.15, 0.73, {"on_task": 0.65, "off_task": 0.15, "uncertain": 0.2})
        self.assertEqual((check.estimate("on_task"), check.estimate("uncertain")), (0.65, 0.2))
        old = check.to_json()
        del old["probabilities"]
        older = FocusCheck.from_json(old)
        self.assertIsNone(older.probabilities)
        self.assertIsNone(older.estimate("on_task"))
        self.assertIsNone(older.estimate("uncertain"))
        self.assertEqual(older.estimate("off_task"), 0.15)
        self.assertIsNone(FocusCheck(60, "unavailable", 0.99, 0.99).estimate("off_task"))

    def test_sync_json_has_every_field_swift_requires(self):
        """Swift's synthesized Codable throws on a missing non-optional key, and the phone then drops the update."""
        s = FocusSession("Task", 60)
        s.checks = [FocusCheck(30, "on_task")]
        sync = json.loads(json.dumps(f.sync_json(s, [s], 7, True)))
        self.assertLessEqual({"history", "revision", "sentAt", "current", "developerMode"}, set(sync))
        self.assertLessEqual({"id", "task", "duration", "startedAt", "elapsed", "status", "detail", "checks"}, set(sync["current"]))
        self.assertLessEqual({"id", "elapsed", "activity", "probability", "confidence", "nudged"}, set(sync["current"]["checks"][0]))
        self.assertEqual(sync["current"]["id"], sync["current"]["id"].upper())
        # Dates are seconds since 2001, the JSONEncoder default: about 25 years before "now" in Unix time.
        self.assertLess(sync["sentAt"], 1_500_000_000)
        self.assertGreater(sync["sentAt"], 800_000_000)
        self.assertNotIn("current", f.sync_json(None, [], 1))


class PairingTests(unittest.TestCase):
    KEY, CHALLENGE = bytes([7]) * 32, bytes([4]) * 32

    def test_encrypted_messages_reject_replay_wrong_key_and_wrong_direction(self):
        phone = link.SecureChannel(self.KEY, self.CHALLENGE, server=False)
        mac = link.SecureChannel(self.KEY, self.CHALLENGE, server=True)
        line = phone.seal({"command": "focusStart", "text": "private task"})
        self.assertNotIn(b"private task", line)
        self.assertEqual(mac.open(line)["command"], "focusStart")
        with self.assertRaises(Exception):
            mac.open(line)
        with self.assertRaises(Exception):
            link.SecureChannel(bytes([8]) * 32, self.CHALLENGE, server=True).open(line)
        with self.assertRaises(Exception):
            phone.open(line)

    def test_known_answer_vectors_match_the_swift_derivation(self):
        """Independent derivations with stdlib hmac, so a mistake in the HKDF call shows up here."""
        import hashlib
        import hmac as h
        self.assertEqual(link.proof(self.KEY, self.CHALLENGE), h.new(self.KEY, b"bluey-pair-v1" + self.CHALLENGE, hashlib.sha256).digest())
        prk = h.new(self.CHALLENGE, self.KEY, hashlib.sha256).digest()  # RFC 5869 extract: salt keys the HMAC
        okm = h.new(prk, b"bluey-v1-mac-to-phone\x01", hashlib.sha256).digest()  # expand, one block
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        mac = link.SecureChannel(self.KEY, self.CHALLENGE, server=True)
        box = base64.b64decode(mac.seal({"hello": "PC"}))
        record = json.loads(AESGCM(okm).decrypt(box[:12], box[12:], None))
        self.assertEqual(record, {"sequence": 0, "packet": {"hello": "PC"}})

    def test_pairing_code_round_trip_and_invalid_input(self):
        secret = bytes([1]) * 32
        self.assertEqual(link.parse_pairing(link.qr_text("Test PC", secret)), ("Test PC", secret))
        self.assertIsNone(link.parse_pairing("https://example.com"))
        self.assertIsNone(link.parse_pairing(link.qr_text("PC", bytes([1]))))
        self.assertEqual(len(link.identifier(secret)), 24)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def connect(self, server_secret, client_secret):
        server = link.PhoneServer(server_secret, name="Test PC", host="127.0.0.1", advertise=False)
        await server.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        client = link.LineConnection(reader, writer, client_secret, server=False)
        return server, client

    async def test_authenticated_loopback_delivers_encrypted_packet_and_replies(self):
        secret = bytes([17]) * 32
        server, client = await self.connect(secret, secret)
        delivered = asyncio.get_running_loop().create_future()
        received = []
        server.on_request = lambda packet, reply: reply({"command": "focusReply", "callID": packet["callID"], "text": packet["text"]})
        client.on_authenticated = lambda: client.send({"command": "focusStart", "callID": "1", "text": "A private focus task"})

        def packet(p):
            received.append(p)
            if p.get("command") == "focusReply":
                delivered.set_result(p)
        client.on_packet = packet
        client.start()
        answer = await asyncio.wait_for(delivered, 5)
        self.assertEqual(answer["text"], "A private focus task")
        self.assertEqual(received[0], {"hello": "Test PC"})
        self.assertEqual(server.phone_names, ["iPhone"])
        client.cancel()
        await asyncio.sleep(0.05)
        self.assertEqual(server.phone_names, [])
        await server.stop()

    async def test_wrong_secret_never_becomes_a_phone(self):
        server, client = await self.connect(bytes([17]) * 32, bytes([18]) * 32)
        closed = asyncio.get_running_loop().create_future()
        client.on_closed = lambda: closed.set_result(True)
        client.start()
        await asyncio.wait_for(closed, 5)
        self.assertEqual(server.phone_names, [])
        self.assertFalse(client.authenticated)
        await server.stop()


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def make(self, **kwargs):
        self.time = 100.0
        file = Path(_home.name) / f"history-{id(self)}-{len(os.listdir(_home.name))}.json"
        defaults = dict(now=lambda: self.time, permission=lambda: True, has_key=lambda: True, screen_unlocked=lambda: True,
                        idle_seconds=lambda: 0.0, front_process=lambda: (1, "app"), history_file=file, settings=None)
        return FocusCompanion(**{**defaults, **kwargs})

    async def ticks(self, app, count):
        for _ in range(count):
            self.time += 1
            app.heartbeat()
            app.tick()
            for _ in range(5):
                await asyncio.sleep(0)

    async def test_repeated_off_task_checks_deliver_a_nudge_including_failures(self):
        for failure in ("none", "capture", "wording"):
            captures, wording, delivered = [0], [0], []

            async def capture(captures=captures, failure=failure):
                captures[0] += 1
                if failure == "capture" and captures[0] == 3:
                    raise RuntimeError("capture failed")
                return screen.ScreenSnapshot()

            async def classify(*_):
                return OFF

            async def prepare(_, __, wording=wording, failure=failure):
                wording[0] += 1
                if failure == "wording":
                    raise RuntimeError("wording failed")
                return "Oi, back to the mission.", None
            app = self.make(capture=capture, classify=classify, prepare_nudge=prepare)
            app.on_nudge = lambda message, target: delivered.append((message, target))
            app.set_developer_mode(True)
            app.heartbeat()
            app.start("Build Bluey in Codex")
            await self.ticks(app, 20)
            expected = "Oi, back to the mission." if failure == "none" else f.FALLBACK_NUDGE
            self.assertEqual(delivered, [(expected, None)], failure)
            self.assertEqual(len(app.current.checks), 2)
            self.assertTrue(app.current.checks[-1].nudged)
            self.assertEqual(wording[0], 0 if failure == "capture" else 1)
            app.end()

    async def test_pause_during_nudge_preparation_never_shows_a_late_bubble(self):
        pending = asyncio.get_running_loop().create_future()

        async def capture():
            return screen.ScreenSnapshot()

        async def classify(*_):
            return OFF

        async def prepare(*_):
            return await pending
        delivered = []
        app = self.make(capture=capture, classify=classify, prepare_nudge=prepare)
        app.on_nudge = lambda *a: delivered.append(a)
        app.set_developer_mode(True)
        app.heartbeat()
        app.start("Build Bluey")
        await self.ticks(app, 20)
        app.pause()
        await asyncio.sleep(0)
        self.assertEqual(delivered, [])
        self.assertFalse(any(c.nudged for c in app.current.checks))
        app.end()

    async def test_developer_cadence_switching_speed_and_pause(self):
        captures = [0]

        async def capture():
            captures[0] += 1
            return screen.ScreenSnapshot()

        async def classify(*_):
            return ON
        app = self.make(capture=capture, classify=classify)
        app.set_developer_mode(True)
        app.heartbeat()
        app.start("Test clock")
        await self.ticks(app, 9)
        self.assertEqual((app.current.elapsed, captures[0]), (36, 0))
        await self.ticks(app, 1)
        self.assertEqual((captures[0], app.current.checks[-1].sample_duration, app.current.real_active_elapsed), (1, 40, 10))
        app.pause()
        self.time += 2
        app.heartbeat()
        app.tick()
        self.assertEqual(app.current.elapsed, 40)
        app.set_developer_mode(False)
        app.resume()
        await self.ticks(app, 29)
        self.assertEqual((captures[0], app.current.elapsed), (1, 69))
        await self.ticks(app, 1)
        self.assertEqual((captures[0], app.current.elapsed), (2, 70))
        app.end()

    async def test_developer_setting_persists_and_switch_cancels_an_old_capture(self):
        gate = asyncio.get_running_loop().create_future()
        classified = []

        async def capture():
            return await gate

        async def classify(*_):
            classified.append(1)
            return ON
        store = link.Settings(Path(_home.name) / "settings-test.json")
        app = self.make(capture=capture, classify=classify, settings=store)
        self.assertFalse(app.developer_mode)
        app.set_developer_mode(True)
        app.heartbeat()
        app.start("Cancel old check")
        await self.ticks(app, 10)
        app.set_developer_mode(False)
        await asyncio.sleep(0)
        self.assertEqual((classified, app.current.checks), ([], []))
        app.set_developer_mode(True)
        reopened = self.make(settings=link.Settings(Path(_home.name) / "settings-test.json"))
        self.assertTrue(reopened.developer_mode)
        app.end()

    async def test_permission_failure_never_captures(self):
        captures = []
        app = self.make(permission=lambda: False, capture=lambda: captures.append(1))
        app.heartbeat()
        self.assertIn("Screen capture", app.start("Write"))
        app.tick()
        self.assertIsNone(app.current)
        self.assertEqual(captures, [])

    async def test_disconnect_lock_and_permission_loss_pause_before_capture(self):
        for mode in ("disconnect", "lock", "permission"):
            state = {"permission": True, "unlocked": True}
            captures = []
            app = self.make(permission=lambda: state["permission"], screen_unlocked=lambda: state["unlocked"],
                            capture=lambda: captures.append(1))
            app.heartbeat()
            app.start("Write")
            if mode == "disconnect":
                self.time += 6
            elif mode == "lock":
                state["unlocked"] = False
            else:
                state["permission"] = False
            app.tick()
            self.assertEqual((app.current.status, captures), ("paused", []), mode)

    async def test_thirty_second_cadence_cancellation_and_no_auto_resume(self):
        gate = asyncio.get_running_loop().create_future()
        captures = [0]

        async def capture():
            captures[0] += 1
            return await gate

        async def classify(*_):
            self.fail("Cancelled capture must not reach the API")
        app = self.make(capture=capture, classify=classify)
        app.heartbeat()
        app.start("Write")
        await self.ticks(app, 29)
        self.assertEqual(captures[0], 0)
        await self.ticks(app, 1)
        self.assertEqual(captures[0], 1)
        app.disconnected()
        app.heartbeat()
        app.tick()
        self.assertEqual(app.current.status, "paused")  # reconnection never resumes automatically
        await asyncio.sleep(0)
        self.assertEqual([c for c in app.current.checks if c.activity != "paused"], [])
        app.end()

    async def test_api_failure_pauses_instead_of_repeating_paid_calls(self):
        async def capture():
            return screen.ScreenSnapshot()

        async def classify(*_):
            raise f.Failure("OpenAI decisions returned HTTP 401. Check your OpenAI key.")
        app = self.make(capture=capture, classify=classify)
        app.heartbeat()
        app.start("Write")
        await self.ticks(app, 30)
        self.assertEqual(app.current.status, "paused")
        self.assertIn("401", app.current.detail)
        app.end()


class WindowsPortTests(unittest.TestCase):
    def test_decision_body_keeps_the_request_contract(self):
        s = FocusSession("Write")
        s.checks = [FocusCheck(i * 30, "on_task", 0.1, 0.9) for i in range(10)]
        body = f.decision_body(b"jpeg", s, 3.4, "Edge", "x" * 20000)
        self.assertEqual(body["model"], "gpt-6-luna")
        text = body["input"][0]["content"][0]["text"]
        self.assertEqual(text.count("P(off)=0.10"), 8)  # only the last eight checks
        self.assertEqual(text.count("x"), 12000 + text.split("x" * 12000)[0].count("x"))
        self.assertTrue(body["input"][0]["content"][1]["image_url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual([c["value"] for c in body["questions"][0]["choices"]], ["on_task", "off_task", "uncertain"])

    def test_nudge_rejects_invented_targets(self):
        line = screen.Target("L1", "Trending now", screen.Rect(10, 10, 100, 20))
        shot = screen.ScreenSnapshot(b"j", [(line, [])], (800.0, 600.0))

        def reply(**fields):
            text = json.dumps({"message": "Oi.", "target_id": None, "target_quote": None, **fields})
            return json.dumps({"output": [{"type": "message", "content": [{"text": text}]}]}).encode()
        original = f.post
        try:
            f.post = lambda *a: reply(target_id="L1", target_quote="Trending now")
            self.assertEqual(asyncio.run(f.prepare_nudge(shot, "t")), ("Oi.", line))
            f.post = lambda *a: reply(target_id="L1", target_quote="Something else")
            self.assertEqual(asyncio.run(f.prepare_nudge(shot, "t")), ("Oi.", None))
            f.post = lambda *a: reply(message="word " * 23)
            with self.assertRaises(f.Failure):
                asyncio.run(f.prepare_nudge(shot, "t"))
        finally:
            f.post = original

    def test_snapshot_targets(self):
        word = screen.Target("W1", "Hello", screen.Rect(10, 10, 40, 20))
        line = screen.Target("L1", "Hello there", screen.Rect(10, 10, 100, 20))
        shot = screen.ScreenSnapshot(b"", [(line, [word])], (1000.0, 1000.0), "Edge",
                                     [screen.Control("C1", "button", "", screen.Rect(500, 500, 20, 20))])
        self.assertEqual(shot.target("w1"), word)
        self.assertEqual(shot.target("C1").text, "button")
        self.assertIsNone(shot.target("L9"))
        self.assertEqual(shot.target_near((12, 12)), word)
        self.assertIsNone(shot.target_near((900, 900)))
        self.assertIn('L1 @60,20 "Hello there"', shot.target_list)

    def test_key_combos_and_refusals(self):
        self.assertEqual(control.parse_combo("ctrl+shift+n")[2], "Ctrl+Shift+N")
        self.assertEqual(control.parse_combo("cmd+t")[0], [0x11])  # the model's Mac habit means Ctrl here
        self.assertEqual(control.parse_combo("return")[1], 0x0D)
        with self.assertRaises(control.UnknownKey):
            control.parse_combo("ctrl+banana")
        with self.assertRaises(control.UnknownKey):
            control.parse_combo("ctrl")
        self.assertIn(frozenset(("l", "win")), realtime.REFUSED_SHORTCUTS)

    def test_find_app_prefers_exact_then_prefix_then_contains(self):
        apps = [("Microsoft Edge", "edge"), ("Notepad++", "npp"), ("Notepad", "np")]
        self.assertEqual(control.find_app("notepad", apps), ("Notepad", "np"))
        self.assertEqual(control.find_app("micro", apps), ("Microsoft Edge", "edge"))
        self.assertEqual(control.find_app("edge", apps), ("Microsoft Edge", "edge"))
        self.assertIsNone(control.find_app("excel", apps))

    def test_open_url_rejects_non_web_addresses(self):
        self.assertEqual(control.open_url("file:///c:/windows"), "That doesn't look like a web address.")
        self.assertEqual(control.open_url("javascript://alert(1)"), "That doesn't look like a web address.")

    def test_tools_follow_the_switches(self):
        names = lambda *a: {t["name"] for t in realtime.tools(*a)}
        self.assertNotIn("click", names(False, False))
        self.assertIn("click", names(False, True))
        self.assertNotIn("codex_start", names(False, False))
        self.assertIn("codex_start", names(True, False))
        self.assertIn("focus_start", names(False, False))
        config = realtime.session_config("state", False, False)
        self.assertIsNone(config["audio"]["input"]["turn_detection"])
        self.assertEqual(config["output_modalities"], ["text"])
        self.assertTrue(config["instructions"].endswith("Right now (state information only): state"))
        self.assertFalse(link.Settings.DEFAULTS["computerControl"])

    def test_research_report_strips_citations_and_tracking(self):
        report = notes.parse_report("q", {"output": [{"type": "message", "content": [{
            "text": "Title here\nFirst ([site.com](https://site.com)) para.\n**Second** [link](https://x.y) para.\nA\nB\nC",
            "annotations": [{"url": "https://site.com/a?utm_source=openai", "title": "Site"}]}]}]})
        self.assertEqual(report.title, "Title here")
        self.assertEqual(report.paragraphs, ["First para.", "Second link para.", "A", "B"])
        self.assertEqual(report.sources, [("Site", "https://site.com/a")])
        with self.assertRaises(notes.ApiError):
            notes.parse_report("q", {"output": []})

    def test_merge_turns_and_notes(self):
        turns = notes.merge_turns([{"speaker": "A", "start": 0, "end": 1, "text": " Hi "}, {"speaker": "A", "start": 2, "end": 3, "text": "there"},
                                   {"speaker": "B", "start": 3, "end": 4, "text": "Yo"}, {"speaker": "B", "start": 9, "end": 10, "text": "Later"}, {"speaker": "B", "text": " "}])
        self.assertEqual([t["text"] for t in turns], ["Hi there", "Yo", "Later"])
        text = notes.markdown({"started": 1_800_000_000, "ended": 1_800_003_660, "entries": [
            {"kind": "asked", "text": "What?", "time": 1_800_000_000}, {"kind": "report", "text": "a\nb"}, {"kind": "reply", "text": " "}]})
        self.assertIn("(1h 1m)", text)
        self.assertIn("> a\n> b", text)
        self.assertIn("You asked Bluey 1 question(s)", text)

    def test_project_resolution(self):
        projects = [{"name": "googly", "path": "a", "aliases": ["bluey-by-riley"]}, {"name": "scrapbook", "path": "b", "aliases": []},
                    {"name": "scrap-two", "path": "c", "aliases": []}]
        self.assertEqual(codex_jobs.resolve_project("Bluey by Riley", projects)["path"], "a")
        self.assertEqual(codex_jobs.resolve_project("scrapbook", projects)["path"], "b")
        self.assertEqual(sorted(codex_jobs.resolve_project("scrap", projects)), ["scrap-two", "scrapbook"])
        self.assertEqual(codex_jobs.resolve_project("nothing", projects), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
