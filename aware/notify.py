"""macOS notifications via osascript."""

import subprocess


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def send(title: str, message: str) -> None:
    script = f'display notification "{_esc(message)}" with title "{_esc(title)}"'
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        pass
