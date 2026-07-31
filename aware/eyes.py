"""Deeper eyes: what Tim is working ON, not just which app is open.

`Premiere Pro` is context. `S03E04_rough.prproj — Premiere Pro` is
awareness. Window titles come from System Events, which needs macOS
Accessibility permission; without it we degrade silently to the app name
alone rather than nagging.
"""

import subprocess

TITLE_SCRIPT = """
tell application "System Events"
    set procName to name of first process whose frontmost is true
    try
        set winName to name of front window of (first process whose frontmost is true)
    on error
        set winName to ""
    end try
end tell
return procName & "|" & winName
"""


def front_window() -> tuple[str | None, str | None]:
    """(app name, window title). Either may be None."""
    try:
        out = subprocess.run(["osascript", "-e", TITLE_SCRIPT],
                             capture_output=True, text=True, timeout=8).stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return None, None
    if not out:
        return None, None
    app, _, title = out.partition("|")
    app = app.strip() or None
    title = title.strip() or None
    # Many apps repeat their own name in the title; that's noise.
    if title and app and title.lower() == app.lower():
        title = None
    return app, title


def accessibility_granted() -> bool:
    app, _ = front_window()
    return app is not None
