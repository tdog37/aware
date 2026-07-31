"""Continuous audio capture: one ffmpeg process per source, writing
16 kHz mono WAV chunks named <label>__YYYYmmdd-HHMMSS.wav so each
chunk's filename carries its start time.

The device is stored by NAME and re-resolved to an avfoundation index on
every (re)start — indices shift when USB/virtual devices come and go, and
recording the wrong device silently would be far worse than a retry loop.
"""

import subprocess
import threading
import time

from . import devices


class Capture:
    def __init__(self, label: str, device_name: str, cfg, log=print):
        self.label = label
        self.device_name = device_name
        self.cfg = cfg
        self.log = log
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None

    def _command(self, device_index: int) -> list[str]:
        pattern = str(self.cfg.chunks_dir / f"{self.label}__%Y%m%d-%H%M%S.wav")
        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-f", "avfoundation",
            "-i", f":{device_index}",
            "-ac", "1",
            "-ar", "16000",
            "-c:a", "pcm_s16le",
            "-f", "segment",
            "-segment_time", str(self.cfg.audio["chunk_seconds"]),
            "-reset_timestamps", "1",
            "-strftime", "1",
            pattern,
        ]

    def _run(self) -> None:
        fast_exits = 0
        err_path = self.cfg.state_dir / "capture_error"
        while not self._stop.is_set():
            found = devices.resolve(self.device_name)
            if found is None:
                self.log(f"[capture:{self.label}] device '{self.device_name}' "
                         f"not found; retrying in 10s")
                err_path.write_text(
                    f"[{self.label}] audio device '{self.device_name}' not found\n"
                    f"Check `aware devices` and config.toml.\n")
                if self._stop.wait(10):
                    return
                continue
            idx, _ = found
            started = time.time()
            self._proc = subprocess.Popen(
                self._command(idx),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            _, err = self._proc.communicate()
            if self._stop.is_set():
                return
            # ffmpeg dying instantly, repeatedly = mic permission denied.
            # A privacy tool must SAY so, not crash-loop in silence.
            if time.time() - started < 2:
                fast_exits += 1
                if fast_exits >= 3:
                    err_path.write_text(
                        f"[{self.label}] recorder exits immediately "
                        f"({fast_exits}x): {(err or '').strip()[:300]}\n"
                        "Mic blocked? System Settings → Privacy & Security → Microphone.\n")
            else:
                fast_exits = 0
                err_path.unlink(missing_ok=True)
            self.log(
                f"[capture:{self.label}] ffmpeg exited unexpectedly "
                f"({(err or '').strip()[:200]}); restarting in 3s"
            )
            time.sleep(3)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"capture-{self.label}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
