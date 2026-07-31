"""Audio device discovery via ffmpeg's avfoundation backend."""

import re
import subprocess


def list_audio_devices() -> list[tuple[int, str]]:
    """Return [(index, name), ...] of avfoundation audio input devices."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:  # ffmpeg not installed — let doctor report it
        return []
    # ffmpeg prints the device list to stderr and exits non-zero; that's expected.
    devices: list[tuple[int, str]] = []
    in_audio = False
    for line in proc.stderr.splitlines():
        if "AVFoundation audio devices" in line:
            in_audio = True
            continue
        if "AVFoundation video devices" in line:
            in_audio = False
            continue
        if not in_audio:
            continue
        m = re.search(r"\[(\d+)\]\s+(.+)$", line)
        if m:
            devices.append((int(m.group(1)), m.group(2).strip()))
    return devices


def resolve(name: str) -> tuple[int, str] | None:
    """Find a device by exact (case-insensitive) name, then by substring."""
    devices = list_audio_devices()
    for idx, dev_name in devices:
        if dev_name.lower() == name.lower():
            return idx, dev_name
    for idx, dev_name in devices:
        if name.lower() in dev_name.lower():
            return idx, dev_name
    return None
