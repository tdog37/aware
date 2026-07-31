"""Transcription of completed audio chunks with whisper.cpp.

A chunk is "ready" when ffmpeg has stopped writing to it (mtime stable).
Silent chunks are dropped before ever reaching Whisper, and a small
hallucination filter drops the junk Whisper emits on near-silence.
"""

import array
import re
import shutil
import subprocess
import time
import wave
from datetime import datetime, timedelta
from pathlib import Path

from . import transcript

# Whisper produces these on silence/noise; never worth keeping.
HALLUCINATIONS = {
    "you",
    "thank you",
    "thank you.",
    "thanks for watching",
    "thanks for watching.",
    "thanks for watching!",
    "thank you for watching",
    "thank you for watching.",
    ".",
    "bye",
    "bye.",
}

_ANNOTATION = re.compile(r"[\[\(][^\]\)]{0,60}[\]\)]")  # [BLANK_AUDIO], (door slams)


def chunk_rms(path: Path) -> int:
    """Root-mean-square amplitude of a 16-bit mono WAV (0–32767)."""
    try:
        with wave.open(str(path), "rb") as w:
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError, OSError):
        return 0
    if not frames:
        return 0
    samples = array.array("h")
    samples.frombytes(frames[: len(frames) - (len(frames) % 2)])
    if not samples:
        return 0
    acc = 0
    for s in samples:
        acc += s * s
    return int((acc / len(samples)) ** 0.5)


def clean_text(raw: str) -> str:
    text = _ANNOTATION.sub(" ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    if text.lower().strip(" .!?") == "" or text.lower() in HALLUCINATIONS:
        return ""
    return text


def transcribe_file(path: Path, cfg) -> str:
    tcfg = cfg.transcribe
    cmd = [
        tcfg["whisper_bin"],
        "-m", str(cfg.whisper_model_path),
        "-f", str(path),
        "-np",  # no runtime prints
        "-nt",  # no timestamps
        "-l", tcfg["language"],
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"whisper failed: {(proc.stderr or '').strip()[:300]}")
    return clean_text(proc.stdout)


def parse_chunk_start(path: Path) -> tuple[str, datetime] | None:
    """mic__20260730-214511.wav -> ("mic", datetime)."""
    m = re.match(r"(.+)__(\d{8})-(\d{6})$", path.stem)
    if not m:
        return None
    label, d, t = m.groups()
    try:
        return label, datetime.strptime(d + t, "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _header_finalized(p: Path) -> bool:
    """True if the WAV's RIFF size field has been written (ffmpeg finalizes
    it on segment close; while writing it holds a 0/0xFFFFFFFF placeholder)."""
    try:
        with p.open("rb") as f:
            hdr = f.read(8)
    except OSError:
        return False
    if len(hdr) < 8 or hdr[:4] != b"RIFF":
        return False
    size = int.from_bytes(hdr[4:8], "little")
    return size not in (0, 0xFFFFFFFF)


def ready_chunks(chunks_dir: Path, chunk_seconds: int, settle_seconds: float = 2.5) -> list[Path]:
    """A chunk is ready only when it is provably closed.

    ffmpeg buffers writes (~256 KiB), so mtime alone CANNOT be trusted — a
    live segment can look stale for many seconds. Per source:
    - any file with a SUCCESSOR is closed (ffmpeg rolled over), ready once
      its mtime settles;
    - the NEWEST file may still be open (even across sleep/wake stalls), so
      it's ready only when long-abandoned AND its WAV header was finalized
      (clean shutdown leaves exactly this). Anything else is left alone —
      never delete a file ffmpeg might still hold."""
    now = time.time()
    by_source: dict[str, list[Path]] = {}
    for p in sorted(chunks_dir.glob("*.wav")):
        by_source.setdefault(p.stem.split("__")[0], []).append(p)
    ready = []
    for files in by_source.values():
        for i, p in enumerate(files):
            try:
                age = now - p.stat().st_mtime
            except FileNotFoundError:
                continue
            if i < len(files) - 1:
                if age > settle_seconds:
                    ready.append(p)
            elif age > chunk_seconds + 5 and _header_finalized(p):
                ready.append(p)
    return ready


def process_chunk(path: Path, cfg, log=print) -> dict | None:
    """Transcribe one chunk, append to the transcript, dispose of audio.

    Returns the transcript entry, or None if the chunk was silence/junk.
    """
    parsed = parse_chunk_start(path)
    if parsed is None:
        path.unlink(missing_ok=True)
        return None
    label, start = parsed

    entry = None
    rms = chunk_rms(path)
    if rms >= cfg.transcribe["min_rms"]:
        try:
            text = transcribe_file(path, cfg)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            log(f"[transcribe] {path.name}: {e}")
            text = ""
        if text:
            end = start + timedelta(seconds=cfg.audio["chunk_seconds"])
            entry = {
                "ts": start.isoformat(timespec="seconds"),
                "end": end.isoformat(timespec="seconds"),
                "source": label,
                "text": text,
            }
            transcript.append(cfg.transcripts_dir, entry)

    if cfg.audio["keep_audio"]:
        day_dir = cfg.audio_archive_dir / start.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), day_dir / path.name)
    else:
        path.unlink(missing_ok=True)
    return entry


def watch(cfg, stop_event, on_entry=None, log=print) -> None:
    """Loop: pick up ready chunks, transcribe, notify listeners."""
    while not stop_event.is_set():
        for chunk in ready_chunks(cfg.chunks_dir, cfg.audio["chunk_seconds"]):
            if stop_event.is_set():
                return
            entry = process_chunk(chunk, cfg, log=log)
            if entry and on_entry:
                on_entry(entry)
        stop_event.wait(2)
