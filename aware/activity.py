"""The Eyes (v0.1): know what Tim is working on while he's working on it.

Samples the frontmost application every few seconds via `lsappinfo`
(no permissions needed) and logs app switches to
context/activity-YYYY-MM-DD.jsonl. Window titles come later — they need
Accessibility permission and a deeper integration (see README roadmap).
"""

import json
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path


def front_app() -> str | None:
    try:
        asn = subprocess.run(
            ["lsappinfo", "front"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        if not asn:
            return None
        info = subprocess.run(
            ["lsappinfo", "info", "-only", "name", asn],
            capture_output=True, text=True, timeout=5,
        ).stdout
        m = re.search(r'"(?:LSDisplayName|name)"\s*=\s*"(.+)"', info)
        return m.group(1) if m else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _path_for(context_dir: Path, day: datetime) -> Path:
    return context_dir / f"activity-{day.strftime('%Y-%m-%d')}.jsonl"


def log_switch(context_dir: Path, app: str) -> None:
    now = datetime.now()
    entry = {"ts": now.isoformat(timespec="seconds"), "app": app}
    with _path_for(context_dir, now).open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def log_focus(context_dir: Path, app: str, title: str | None) -> None:
    now = datetime.now()
    entry = {"ts": now.isoformat(timespec="seconds"), "app": app}
    if title:
        entry["title"] = title
    with _path_for(context_dir, now).open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def watch(cfg, stop_event, log=print) -> None:
    from . import eyes

    last = None
    interval = cfg.activity["interval_seconds"]
    deep = cfg.activity.get("window_titles", True)
    while not stop_event.is_set():
        app, title = eyes.front_window() if deep else (front_app(), None)
        if app is None and deep:  # no Accessibility permission — degrade
            app = front_app()
        if app and (app, title) != last:
            log_focus(cfg.context_dir, app, title)
            last = (app, title)
        stop_event.wait(interval)


def read_window(context_dir: Path, minutes: int) -> list[dict]:
    now = datetime.now()
    cutoff = now - timedelta(minutes=minutes)
    entries = []
    for p in {_path_for(context_dir, cutoff), _path_for(context_dir, now)}:
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            try:
                e = json.loads(line)
                if datetime.fromisoformat(e["ts"]) >= cutoff:
                    entries.append(e)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    entries.sort(key=lambda e: e["ts"])
    return entries


def render(entries: list[dict]) -> str:
    lines = []
    for e in entries:
        ts = datetime.fromisoformat(e["ts"]).strftime("%H:%M:%S")
        what = e["app"]
        if e.get("title"):
            what += f" — {e['title']}"
        lines.append(f"[{ts}] {what}")
    return "\n".join(lines)
