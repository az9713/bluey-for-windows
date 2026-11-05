"""Storage, pairing and the encrypted phone link. Ports Shared/GooglyLink.swift, Shared/SecurePairing.swift,
PhoneServer.swift, PairingPanel.swift's MacPairing, Keychain.swift and Settings.swift.

The wire format must stay byte-compatible with iOS/Sources/MacLink.swift, so do not "tidy" it."""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import socket
import threading
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

SERVICE_TYPE = "_googly._tcp.local."
# ponytail: NTFS has no 0600. %APPDATA% is readable by you, SYSTEM and admins. Use DPAPI if admins are a threat.
APP_DIR = Path(os.environ.get("BLUEY_HOME") or Path(os.environ["APPDATA"]) / "Googly")
FONTS_DIR = Path(__file__).resolve().parent.parent / "Shared" / "Fonts"


def write_atomic(file: Path, data: bytes):
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_name(file.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, file)


def _read_json(file: Path, default):
    try:
        return json.loads(file.read_bytes())
    except (OSError, ValueError):
        return default


_settings_lock = threading.Lock()


class Settings:
    """Menu choices, kept between launches. Replaces UserDefaults."""
    DEFAULTS = {
        "cursorSize": 72.0, "showCursor": True, "phonePosition": 0.5, "followMouse": True,
        # Off on Windows: the Mac has an Accessibility prompt as a consent gate, and Windows has no equivalent.
        "computerControl": False,
        "trail": "comet", "mood": "listening", "focusDeveloperMode": False, "personality": "", "codexFolders": [],
    }

    def __init__(self, file: Path):
        object.__setattr__(self, "_file", file)
        object.__setattr__(self, "_data", _read_json(file, {}))

    def __getattr__(self, name):
        return self._data.get(name, self.DEFAULTS[name])

    def __setattr__(self, name, value):
        with _settings_lock:  # the menu writes from the Qt thread, focus from the asyncio thread
            self._data[name] = value
            write_atomic(self._file, json.dumps(self._data, indent=1).encode())


settings = Settings(APP_DIR / "settings.json")


def get_key(name="openai"):
    return _read_json(APP_DIR / "keys.json", {}).get(name) or None


def set_key(value, name="openai"):
    stored = _read_json(APP_DIR / "keys.json", {})
    stored[name] = value
    write_atomic(APP_DIR / "keys.json", json.dumps(stored, indent=1).encode())


# MARK: Pairing

def device_name():
    return socket.gethostname()


def pairing_secret() -> bytes:
    file = APP_DIR / "pairing-secret"
    try:
        saved = file.read_bytes()
        if len(saved) == 32:
            return saved
    except OSError:
        pass
    key = secrets.token_bytes(32)
    write_atomic(file, key)
    return key


def identifier(secret: bytes) -> str:
    """Public fingerprint for discovery: the first 12 bytes of SHA-256, as 24 hex characters."""
    return hashlib.sha256(secret).digest()[:12].hex()


def qr_text(service: str, secret: bytes) -> str:
    record = {"version": 1, "service": service, "secret": base64.b64encode(secret).decode()}
    return "bluey-pair:" + base64.b64encode(json.dumps(record).encode()).decode()


def parse_pairing(text: str):
    """Returns (service, secret), or None for anything that is not a valid code."""
    if not text.startswith("bluey-pair:") or len(text) >= 2048:
        return None
    try:
        record = json.loads(base64.b64decode(text[11:], validate=True))
        secret = base64.b64decode(record["secret"], validate=True)
        if record["version"] != 1 or len(secret) != 32 or not record["service"]:
            return None
        return record["service"], secret
    except (ValueError, KeyError, TypeError):
        return None


def proof(secret: bytes, challenge: bytes) -> bytes:
    return hmac.new(secret, b"bluey-pair-v1" + challenge, hashlib.sha256).digest()


class SecureChannel:
    """Challenge-derived direction keys and ordered AES-GCM records stop eavesdropping, tampering and replay."""

    def __init__(self, secret: bytes, challenge: bytes, server: bool):
        def key(direction):
            return AESGCM(HKDF(algorithm=hashes.SHA256(), length=32, salt=challenge,
                               info=b"bluey-v1-" + direction).derive(secret))
        self._send = key(b"mac-to-phone" if server else b"phone-to-mac")
        self._receive = key(b"phone-to-mac" if server else b"mac-to-phone")
        self._sent = 0
        self._received = 0

    def seal(self, packet: dict) -> bytes:
        data = json.dumps({"sequence": self._sent, "packet": packet}, allow_nan=False).encode()
        nonce = secrets.token_bytes(12)
        box = nonce + self._send.encrypt(nonce, data, None)  # CryptoKit's "combined" layout
        self._sent += 1
        return base64.b64encode(box)

    def open(self, line: bytes) -> dict:
        box = base64.b64decode(line, validate=True)
        record = json.loads(self._receive.decrypt(box[:12], box[12:], None))
        if record["sequence"] != self._received or not isinstance(record["packet"], dict):
            raise ValueError("record out of order")
        self._received += 1
        return record["packet"]


class LineConnection:
    """Newline-delimited records over one TCP connection, after a challenge/HMAC handshake."""

    def __init__(self, reader, writer, secret: bytes, server: bool):
        self.reader, self.writer, self.secret, self.server = reader, writer, secret, server
        self.on_packet = self.on_authenticated = self.on_closed = None
        self.authenticated = False
        self._challenge = None
        self._channel = None
        self._task = None

    def start(self):
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self):
        timeout = asyncio.get_running_loop().call_later(8, lambda: None if self.authenticated else self.cancel())
        buffer = b""
        try:
            if self.server:
                self._challenge = secrets.token_bytes(32)
                self._write_json({"challenge": base64.b64encode(self._challenge).decode()})
            while True:
                data = await self.reader.read(1024 * 1024)
                if not data:
                    return
                buffer += data
                if len(buffer) > 16 * 1024 * 1024:
                    return
                *lines, buffer = buffer.split(b"\n")
                for line in lines:
                    self._accept(line)
        except (OSError, ValueError, KeyError, TypeError, asyncio.CancelledError, InvalidRecord):
            pass
        finally:
            timeout.cancel()
            self.writer.close()
            if self.on_closed:
                self.on_closed()

    def send(self, packet: dict):
        if not self.authenticated or self.writer.is_closing():
            return
        self.writer.write(self._channel.seal(packet) + b"\n")

    def cancel(self):
        if self._task:
            self._task.cancel()

    def _write_json(self, fields):
        self.writer.write(json.dumps(fields).encode() + b"\n")

    def _accept(self, line: bytes):
        if self._channel:
            try:
                packet = self._channel.open(line)
            except Exception as error:  # cryptography raises its own InvalidTag
                raise InvalidRecord() from error
            if self.authenticated:
                if self.on_packet:
                    self.on_packet(packet)
            elif not self.server and packet.get("command") == "paired":
                self.authenticated = True
                if self.on_authenticated:
                    self.on_authenticated()
            else:
                raise InvalidRecord()
            return
        fields = json.loads(line)
        if self.server:
            if not hmac.compare_digest(base64.b64decode(fields["proof"]), proof(self.secret, self._challenge)):
                raise InvalidRecord()
            self._channel = SecureChannel(self.secret, self._challenge, server=True)
            self.authenticated = True
            self.send({"command": "paired"})
            if self.on_authenticated:
                self.on_authenticated()
        else:
            challenge = base64.b64decode(fields["challenge"])
            if len(challenge) != 32:
                raise InvalidRecord()
            self._channel = SecureChannel(self.secret, challenge, server=False)
            self._write_json({"proof": base64.b64encode(proof(self.secret, challenge)).decode()})


class InvalidRecord(Exception):
    pass


def _local_addresses():
    from zeroconf import get_all_addresses
    return sorted(a for a in get_all_addresses() if not a.startswith(("127.", "169.254.")))


class PhoneServer:
    """Advertises this PC on the local network and streams face updates to any paired phone."""

    def __init__(self, secret: bytes, name=None, host="0.0.0.0", advertise=True):
        self.secret, self.name, self.host, self.advertise = secret, name or device_name(), host, advertise
        self.port = None
        self.on_phones_changed = None
        self.on_request = None
        self._phones = {}
        self._last_sent = None
        self._last_sent_at = 0.0
        self._server = None
        self._zeroconf = None
        self._info = None
        self._addresses = []

    @property
    def phone_names(self):
        return [name or "iPhone" for name in self._phones.values()]

    async def start(self):
        self._server = await asyncio.start_server(self._accept, self.host, 0)
        self.port = self._server.sockets[0].getsockname()[1]
        if self.advertise:
            asyncio.get_running_loop().create_task(self._keep_advertising())

    async def stop(self):
        if self._zeroconf:
            await self._zeroconf.async_close()
        if self._server:
            self._server.close()

    async def _keep_advertising(self):
        """NWListener follows network changes by itself. Here a new Wi-Fi needs a new registration."""
        from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf
        while True:
            addresses = _local_addresses()
            if addresses != self._addresses:
                try:
                    if self._zeroconf:
                        await self._zeroconf.async_close()
                    self._zeroconf = AsyncZeroconf()
                    self._info = AsyncServiceInfo(
                        SERVICE_TYPE, f"{self.name}.{SERVICE_TYPE}", port=self.port,
                        addresses=[socket.inet_aton(a) for a in addresses],
                        properties={"bluey-id": identifier(self.secret)}, server=f"{socket.gethostname()}.local.")
                    await (await self._zeroconf.async_register_service(self._info, allow_name_change=True))
                    self._addresses = addresses
                except Exception as error:
                    print(f"Bluey: could not advertise on the network ({error}); retrying")
                    self._addresses = []
                    await asyncio.sleep(2)
                    continue
            await asyncio.sleep(15)

    async def _accept(self, reader, writer):
        sock = writer.get_extra_info("socket")
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 2)  # NWParameters.googly: keepaliveIdle = 2
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 1)
        link = LineConnection(reader, writer, self.secret, server=True)

        def authenticated():
            self._phones[link] = None
            link.send({"hello": self.name})
            if self._last_sent:
                link.send({"face": self._last_sent})
            self._changed()

        def packet(p):
            if link not in self._phones:
                return
            if p.get("hello"):
                self._phones[link] = p["hello"]
                self._changed()
            if p.get("command") is not None and self.on_request:
                self.on_request(p, link.send)

        def closed():
            if self._phones.pop(link, "gone") != "gone":
                self._changed()

        link.on_authenticated, link.on_packet, link.on_closed = authenticated, packet, closed
        link.start()

    def _changed(self):
        if self.on_phones_changed:
            self.on_phones_changed(self.phone_names)

    def broadcast(self, packet: dict):
        for link in list(self._phones):
            link.send(packet)

    def send_face(self, face: dict):
        """Skips updates too small to see, but resends each second so the phone knows the PC is driving."""
        now = time.monotonic()
        last = self._last_sent
        if last and now - self._last_sent_at < 1 and last["mood"] == face["mood"] \
                and abs(last["gazeX"] - face["gazeX"]) < 0.004 and abs(last["gazeY"] - face["gazeY"]) < 0.004 \
                and abs(last["talk"] - face["talk"]) < 0.02:
            return
        self._last_sent, self._last_sent_at = face, now
        self.broadcast({"face": face})
