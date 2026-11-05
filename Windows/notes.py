"""Meeting notes and web research. Ports MeetingNotes.swift and WebResearch.swift."""
import asyncio
import ctypes
import datetime
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

import link

API = "https://api.openai.com/v1/"
TRANSCRIBE_MODEL = "gpt-4o-transcribe-diarize"
FALLBACK_MODEL = "gpt-transcribe"
RESEARCH_MODEL = "gpt-5.5"


class ApiError(Exception):
    pass


def documents_folder() -> Path:
    """The real Documents folder, which OneDrive may have moved."""
    path = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, path)  # CSIDL_PERSONAL
    return Path(path.value)


def root() -> Path:
    return documents_folder() / "Bluey Notes"


README = """# Bluey Notes

Meeting notes recorded by the Bluey app (the blueberry on the iPhone). One folder per listening session, named by start time.

In each session folder:
- `notes.md`: the full transcript with timestamps and speaker labels, plus your questions to Bluey, Bluey's replies and any research reports, in time order. Start here.
- `session.json`: the same data, structured.
- `audio/`: the raw recording, as five-minute AAC chunks.

Speaker letters (A, B, ...) are assigned per five-minute chunk, so the same letter can mean different people in different chunks. Lines marked "(live)" are a rough live transcript for audio that hasn't been fully transcribed yet.
"""


def folder(session, started: float) -> Path:
    base = root()
    base.mkdir(parents=True, exist_ok=True)
    if not (base / "README.md").exists():
        (base / "README.md").write_text(README, encoding="utf-8")
    tag = session[:8].lower()
    for existing in base.iterdir():
        if existing.name.endswith(tag):
            return existing
    url = base / f"{datetime.datetime.fromtimestamp(started):%Y-%m-%d %H.%M} {tag}"
    (url / "audio").mkdir(parents=True, exist_ok=True)
    return url


def _post_audio(audio: bytes, fields: dict) -> dict:
    key = link.get_key()
    if not key:
        raise ApiError("I need an OpenAI API key. Add one under OpenAI Key in the tray menu.")
    boundary = f"googly-{uuid.uuid4()}"
    body = b""
    for name, value in fields.items():
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
    body += f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="chunk.m4a"\r\nContent-Type: audio/mp4\r\n\r\n'.encode()
    body += audio + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(API + "audio/transcriptions", body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise ApiError(f"transcription {error.code}") from None


def merge_turns(segments):
    """Joins one speaker's consecutive pieces into a single turn."""
    turns = []
    for s in segments:
        turn = {"speaker": s.get("speaker") or "?", "start": s.get("start") or 0.0, "end": s.get("end") or 0.0,
                "text": (s.get("text") or "").strip()}
        if not turn["text"]:
            continue
        if turns and turns[-1]["speaker"] == turn["speaker"] and turn["start"] - turns[-1]["end"] < 2.5:
            turns[-1]["text"] += " " + turn["text"]
            turns[-1]["end"] = turn["end"]
        else:
            turns.append(turn)
    return turns


def _transcribe_blocking(audio):
    try:
        data = _post_audio(audio, {"model": TRANSCRIBE_MODEL, "response_format": "diarized_json", "chunking_strategy": "auto"})
        return merge_turns(data.get("segments") or [])
    except Exception as error:
        print(f"Bluey: diarized transcription failed ({error}), trying {FALLBACK_MODEL}")
    text = (_post_audio(audio, {"model": FALLBACK_MODEL, "response_format": "json"}).get("text") or "").strip()
    return [{"speaker": "?", "start": 0.0, "end": 0.0, "text": text}] if text else []


async def transcribe(audio: bytes, session, index, started: float):
    """Saves the chunk's audio into the session folder and transcribes it. Returns (turns, folder)."""
    path = folder(session, started)
    (path / "audio").mkdir(exist_ok=True)
    (path / "audio" / f"chunk-{index:03d}.m4a").write_bytes(audio)
    return await asyncio.to_thread(_transcribe_blocking, audio), path


def write_session(session: dict):
    """Writes session.json and notes.md from the phone's copy of the session. Returns the folder, or None."""
    if not isinstance(session.get("id"), str) or not isinstance(session.get("started"), (int, float)):
        return None
    path = folder(session["id"], session["started"])
    link.write_atomic(path / "session.json", json.dumps(session, indent=2, sort_keys=True).encode())
    link.write_atomic(path / "notes.md", markdown(session).encode())
    return path


def markdown(session: dict) -> str:
    def at(stamp):
        return datetime.datetime.fromtimestamp(stamp)
    started = at(session["started"])
    ended = session.get("ended")
    entries = session.get("entries") or []
    out = [f"# Bluey session: {started:%A, %B %d, %Y at %I:%M %p}", ""]
    if ended is not None:
        minutes = int((ended - session["started"]) / 60)
        out.append(f"Ended {at(ended):%I:%M:%S %p} ({minutes // 60}h {minutes % 60}m).")
    else:
        out.append("Still recording.")
    pending = session.get("transcribing") or 0
    if pending > 0:
        out.append(f"{pending} recording chunk(s) still being transcribed; this file updates when they're done.")
    asked = sum(1 for e in entries if e.get("kind") == "asked")
    if asked > 0:
        out.append(f"You asked Bluey {asked} question(s); they're marked inline below.")
    out += ["", "## Transcript", ""]
    for entry in entries:
        text = (entry.get("text") or "").strip()
        if not text:
            continue
        time = f"{at(entry['time']):%I:%M:%S %p}" if isinstance(entry.get("time"), (int, float)) else ""
        kind = entry.get("kind")
        if kind == "transcript":
            out.append(f"**[{time}] Speaker {entry.get('speaker') or '?'}:** {text}")
        elif kind == "heard":
            out.append(f"[{time}] (live) {text}")
        elif kind == "asked":
            out.append(f"> **[{time}] You asked Bluey:** {text}")
        elif kind == "reply":
            out.append(f"> **Bluey:** {text}")
        elif kind == "codex":
            out.append(f"> **[{time}] Codex:** {text}")
        elif kind == "report":
            quoted = "\n".join("> " + line for line in text.split("\n"))
            out.append("> **Research report:**\n" + quoted)
        else:
            out.append(f"[{time}] {text}")
        out.append("")
    return "\n".join(out)


# MARK: Research

@dataclass
class ResearchReport:
    question: str
    title: str
    paragraphs: list
    sources: list  # [(title, url)]

    @property
    def plain_text(self):
        return "\n\n".join([self.title] + self.paragraphs)


RESEARCH_INSTRUCTIONS = ("Research the question on the web and write a short, friendly report for a busy person. "
                         "First line: a plain title of under eight words. Then one to four short paragraphs (four at most), "
                         "leading with the direct answer, then the most useful details. Plain text only: no markdown, "
                         "no headings, no bullet lists, and no links or citations in the text.")


def _clean_link(url):
    """Removes tracking parameters the search tool adds."""
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k != "utm_source"]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def parse_report(question, response: dict) -> ResearchReport:
    text, sources = "", []
    for item in response.get("output") or []:
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            text += part.get("text") or ""
            for note in part.get("annotations") or []:
                if note.get("url"):
                    url = _clean_link(note["url"])
                    if url not in [s[1] for s in sources]:
                        sources.append((note.get("title") or urllib.parse.urlsplit(url).hostname or url, url))
    # Drop inline citations like "([site.com](https://...))" and any stray markdown.
    text = re.sub(r"\s*\(\[[^\]]*\]\([^)]*\)\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\s*\[https?://[^\]]*\]", "", text)
    text = text.replace("**", "").replace("#", "")
    blocks = [line.strip() for line in text.split("\n") if line.strip()]
    if not blocks:
        raise ApiError("The research didn't come back: empty answer")
    return ResearchReport(question, blocks[0], blocks[1:5], sources[:5])


def responses(body: dict, timeout) -> dict:
    key = link.get_key()
    if not key:
        raise ApiError("I need an OpenAI API key for research. Add one under OpenAI Key in the tray menu.")
    request = urllib.request.Request(API + "responses", json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise ApiError(f"The research didn't come back: {error.code} {error.read()[:160].decode(errors='replace')}") from None


async def research(question, context=None) -> ResearchReport:
    text = question + (f"\n\nWhat's on the user's screen, for context: {context}" if context else "")
    return parse_report(question, await asyncio.to_thread(responses, {
        "model": RESEARCH_MODEL, "reasoning": {"effort": "low"}, "tools": [{"type": "web_search"}],
        "instructions": RESEARCH_INSTRUCTIONS, "input": text}, 90))
