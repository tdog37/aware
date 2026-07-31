"""Notifications.

Primary channel: append to state/notify.jsonl — the menu-bar widget (a real
app registered with macOS notification center) polls it and posts each entry
as a native notification from "Aware". Fallback when the widget isn't
running: osascript, which macOS attributes to Script Editor and silently
drops unless that app has notification permission — the exact failure mode
that made this file grow a real channel.
"""

import json
import subprocess
from datetime import datetime

from .config import HOME


def _widget_running() -> bool:
    try:
        return subprocess.run(["pgrep", "-x", "AwareBar"],
                              capture_output=True, timeout=5).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def send(title: str, message: str) -> None:
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "title": title,
        "message": message,
    }
    queue = HOME / "state" / "notify.jsonl"
    try:
        queue.parent.mkdir(parents=True, exist_ok=True)
        with queue.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass

    if _widget_running():
        return  # the orb will deliver it natively
    script = (f'display notification "{_esc(message)}" '
              f'with title "{_esc(title)}" sound name "Glass"')
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        pass
