"""The rolling transcript store.

One JSONL file per day: transcripts/YYYY-MM-DD.jsonl
Each line: {"ts": iso, "end": iso, "source": "mic"|"system", "text": "..."}
Audio is deleted after transcription by default — Aware keeps words, not
recordings.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path


def path_for(transcripts_dir: Path, day: datetime) -> Path:
    return transcripts_dir / f"{day.strftime('%Y-%m-%d')}.jsonl"


def append(transcripts_dir: Path, entry: dict) -> None:
    day = datetime.fromisoformat(entry["ts"])
    path = path_for(transcripts_dir, day)
    with path.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _read_file(path: Path) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def read_window(transcripts_dir: Path, minutes: int) -> list[dict]:
    """Entries from the last `minutes` minutes (handles midnight rollover)."""
    now = datetime.now()
    cutoff = now - timedelta(minutes=minutes)
    entries = _read_file(path_for(transcripts_dir, cutoff))
    if cutoff.date() != now.date():
        entries += _read_file(path_for(transcripts_dir, now))
    out = []
    for e in entries:
        try:
            if datetime.fromisoformat(e["ts"]) >= cutoff:
                out.append(e)
        except (KeyError, ValueError):
            continue
    out.sort(key=lambda e: e["ts"])
    return out


def render(entries: list[dict]) -> str:
    lines = []
    for e in entries:
        ts = datetime.fromisoformat(e["ts"]).strftime("%H:%M:%S")
        lines.append(f"[{ts}] {e.get('source', '?')}: {e.get('text', '')}")
    return "\n".join(lines)
