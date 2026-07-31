"""The `aware` command."""

import argparse
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from . import activity, brain, capture, config, devices, transcribe, transcript


def _banner(msg: str) -> None:
    print(f"\033[1m{msg}\033[0m")


# --------------------------------------------------------------------------
# aware devices
# --------------------------------------------------------------------------

def cmd_devices(cfg, args) -> int:
    devs = devices.list_audio_devices()
    if not devs:
        print("No avfoundation audio devices found — is ffmpeg installed?")
        return 1
    _banner("Audio input devices:")
    for idx, name in devs:
        tags = []
        if name.lower() == cfg.audio["mic_device"].lower():
            tags.append("<- mic source")
        if name.lower() == cfg.audio["system_device"].lower():
            tags.append("<- system source")
        print(f"  [{idx}] {name} {' '.join(tags)}")
    return 0


# --------------------------------------------------------------------------
# aware start
# --------------------------------------------------------------------------

def _resolve_sources(cfg) -> list[tuple[str, int, str]]:
    resolved = []
    for label in cfg.audio["sources"]:
        key = "mic_device" if label == "mic" else "system_device"
        want = cfg.audio.get(key, "")
        found = devices.resolve(want)
        if found is None:
            print(f"  ! could not find audio device '{want}' for source '{label}' — skipping")
            continue
        resolved.append((label, found[0], found[1]))
    return resolved


def cmd_start(cfg, args) -> int:
    # Two daemons = double recording, clobbered pidfile, and a lying orb.
    existing = _daemon_pid(cfg)
    if existing:
        print(f"Aware is already running (pid {existing}).")
        print("Use `aware off` first — or `tail -f logs/daemon.log` for the live view.")
        return 1
    (cfg.state_dir / "capture_error").unlink(missing_ok=True)

    persona = cfg.brain["persona"]
    _banner(f"⚡ Aware — {persona} coming online · {datetime.now().strftime('%A %B %d, %I:%M %p')}")

    sources = _resolve_sources(cfg)
    if not sources:
        print("No audio sources available. Check `aware devices` and config.toml.")
        return 1
    for label, idx, name in sources:
        print(f"  ear:   {label} = [{idx}] {name}")
    print(f"  model: {cfg.whisper_model_path.name}")

    brain_on = cfg.brain["enabled"] and not args.no_brain
    if brain_on:
        print(f"  mind:  {persona} · {cfg.brain['model']}, heartbeat every "
              f"{cfg.brain['interval_seconds']}s (wakes only on new speech)")
    else:
        print(f"  mind:  off (senses only)")
    if cfg.activity["enabled"]:
        print(f"  eyes:  frontmost-app tracking every {cfg.activity['interval_seconds']}s")
    print(f"  wake:  {', '.join(repr(p) for p in cfg.wake['phrases'])}")
    print("\nListening. Ctrl-C to stop.\n")

    stop = threading.Event()
    wake_q: queue.Queue = queue.Queue()

    # Whisper punctuates freely ("Hey. Spinther,"), so match wake phrases on
    # punctuation-stripped text. And only the mic can give the mind orders —
    # a voice on a Zoom call or in a video saying the wake phrase is not Tim.
    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

    wake_phrases = [_norm(p) for p in cfg.wake["phrases"]]
    wake_sources = set(cfg.wake["sources"])

    def on_entry(entry: dict) -> None:
        ts = datetime.fromisoformat(entry["ts"]).strftime("%H:%M:%S")
        print(f"[{ts}] {entry['source']}: {entry['text']}")
        if entry["source"] in wake_sources and any(
            p in _norm(entry["text"]) for p in wake_phrases
        ):
            print(f"        ^ wake phrase — waking {persona} now")
            wake_q.put(entry)

    caps = [capture.Capture(label, name, cfg) for label, idx, name in sources]
    for c in caps:
        c.start()

    threads = [
        threading.Thread(
            target=transcribe.watch, args=(cfg, stop),
            kwargs={"on_entry": on_entry}, name="transcribe", daemon=True,
        )
    ]
    if cfg.activity["enabled"]:
        threads.append(threading.Thread(
            target=activity.watch, args=(cfg, stop), name="eyes", daemon=True))
    if brain_on:
        threads.append(threading.Thread(
            target=brain.scheduler, args=(cfg, stop, wake_q), name="brain", daemon=True))
    for t in threads:
        t.start()

    # SIGTERM (launchctl unload, `aware off`) must clean up like Ctrl-C —
    # an orphaned ffmpeg silently recording is the one unforgivable bug.
    def _sigterm(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _sigterm)

    pid_path = cfg.state_dir / "aware.pid"
    pid_path.write_text(str(os.getpid()))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down…")
        stop.set()
        for c in caps:
            c.stop()
    finally:
        # Only remove the pidfile if it is still OURS — never erase the
        # record of a daemon that outlives us.
        try:
            if pid_path.read_text().strip() == str(os.getpid()):
                pid_path.unlink()
        except OSError:
            pass
    return 0


# --------------------------------------------------------------------------
# aware on / off / toggle / status — the quick switch
# --------------------------------------------------------------------------

def _pid_is_daemon(pid: int) -> bool:
    """The pid must actually BE an aware daemon — pids get reused."""
    out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                         capture_output=True, text=True).stdout
    return "-m aware start" in out


def _daemon_pid(cfg) -> int | None:
    pid_path = cfg.state_dir / "aware.pid"
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)
            if _pid_is_daemon(pid):
                return pid
            pid_path.unlink(missing_ok=True)
        except (ValueError, ProcessLookupError, PermissionError):
            pid_path.unlink(missing_ok=True)
    proc = subprocess.run(["pgrep", "-f", "--", "-m aware start"],
                          capture_output=True, text=True)
    for line in proc.stdout.split():
        if line.strip().isdigit() and int(line) != os.getpid():
            return int(line)
    return None


def _capture_alive(cfg, window: float = 45) -> bool:
    """ffmpeg flushes the live chunk every ~8-10s; a recent wav mtime is
    proof audio is actually flowing, not just that a pid exists."""
    now = time.time()
    try:
        return any(now - p.stat().st_mtime < window
                   for p in cfg.chunks_dir.glob("*.wav"))
    except OSError:
        return False


def cmd_on(cfg, args) -> int:
    pid = _daemon_pid(cfg)
    if pid:
        print(f"⚡ Aware is already ON (pid {pid}).")
        return 0
    log_path = cfg.logs_dir / "daemon.log"
    with log_path.open("a") as logf:
        proc = subprocess.Popen(
            [str(cfg.home / "bin" / "aware"), "start"],
            stdout=logf, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    time.sleep(2)
    if proc.poll() is None:
        print(f"⚡ Aware is ON — listening (pid {proc.pid}). Live log: logs/daemon.log")
        return 0
    print(f"Aware failed to start — see {log_path}")
    return 1


def cmd_off(cfg, args) -> int:
    pid = _daemon_pid(cfg)
    if not pid:
        print("Aware is already OFF.")
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        try:
            os.kill(pid, 0)
            time.sleep(0.25)
        except ProcessLookupError:
            break
    # Belt and suspenders: no recorder may ever outlive the daemon.
    subprocess.run(["pkill", "-f", str(cfg.chunks_dir)], capture_output=True)
    print("Aware is OFF — microphone released.")
    return 0


def cmd_toggle(cfg, args) -> int:
    return cmd_off(cfg, args) if _daemon_pid(cfg) else cmd_on(cfg, args)


def cmd_status(cfg, args) -> int:
    pid = _daemon_pid(cfg)
    persona = cfg.brain["persona"]
    if pid:
        print(f"⚡ ON — listening (pid {pid})")
        err_path = cfg.state_dir / "capture_error"
        pid_path = cfg.state_dir / "aware.pid"
        started_recently = (
            pid_path.exists() and time.time() - pid_path.stat().st_mtime < 60
        )
        if err_path.exists():
            hint = err_path.read_text().strip().splitlines()[-1]
            print(f"⚠ AUDIO NOT FLOWING — {hint}")
        elif not started_recently and not _capture_alive(cfg):
            print("⚠ AUDIO NOT FLOWING — no capture activity for 45s+ "
                  "(device gone or mic blocked); check `aware devices` and mic permission")
    else:
        print("OFF — microphone released")
    entries = transcript.read_window(cfg.transcripts_dir, 120)
    if entries:
        last = entries[-1]
        ts = datetime.fromisoformat(last["ts"]).strftime("%I:%M %p")
        text = last["text"]
        print(f'last heard at {ts}: "{text[:70]}{"…" if len(text) > 70 else ""}"')
    else:
        print("nothing heard in the last 2 hours")
    state = brain.load_state(cfg)
    if state.get("last_run"):
        print(f"{persona} last woke: {state['last_run']} ({state.get('runs', 0)} wakes total)")
    return 0


# --------------------------------------------------------------------------
# aware log / brain / proposals / approve
# --------------------------------------------------------------------------

def cmd_log(cfg, args) -> int:
    entries = transcript.read_window(cfg.transcripts_dir, args.minutes)
    if not entries:
        print(f"(no transcript in the last {args.minutes} minutes)")
        return 0
    print(transcript.render(entries))
    return 0


def cmd_brain(cfg, args) -> int:
    if args.dry_run:
        print(brain.build_prompt(cfg, reason="Manual run (dry run)"))
        return 0
    _banner("Waking the brain…")
    out = brain.run_once(cfg, reason="Manual run via `aware brain`")
    print(out.strip() or "(no output)")
    return 0


def cmd_proposals(cfg, args) -> int:
    proposals = sorted(cfg.proposed_dir.glob("*.md"))
    if not proposals:
        print("No proposed playbooks. When Aware notices a repeating pattern, "
              "its proposals will appear here.")
        return 0
    _banner("Proposed playbooks (approve with `aware approve <name>`):")
    for p in proposals:
        first = next((ln for ln in p.read_text().splitlines() if ln.strip()), "")
        print(f"  {p.stem:30} {first[:70]}")
    return 0


def cmd_approve(cfg, args) -> int:
    src = cfg.proposed_dir / f"{args.name}.md"
    if not src.exists():
        print(f"No proposed playbook named '{args.name}'. See `aware proposals`.")
        return 1
    dest = cfg.playbooks_dir / src.name
    shutil.move(str(src), dest)
    print(f"Approved: {dest.name} is now an active playbook.")
    return 0


# --------------------------------------------------------------------------
# aware widget — the menu-bar orb
# --------------------------------------------------------------------------

def cmd_widget(cfg, args) -> int:
    widget_dir = cfg.home / "widget"
    src = widget_dir / "AwareBar.swift"
    plist = widget_dir / "Info.plist"
    # A real .app bundle: macOS only gives notification identity to real apps.
    app_dir = widget_dir / "Aware.app"
    binary = app_dir / "Contents" / "MacOS" / "AwareBar"

    if args.stop:
        subprocess.run(["pkill", "-x", "AwareBar"], capture_output=True)
        print("Widget stopped.")
        return 0

    if shutil.which("swiftc") is None:
        print("The widget needs the Swift compiler (one-time):  xcode-select --install")
        return 1

    stale = (not binary.exists()
             or binary.stat().st_mtime < src.stat().st_mtime
             or binary.stat().st_mtime < plist.stat().st_mtime)
    if stale:
        print("Building the widget (one-time, ~30s)…")
        binary.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["swiftc", "-O", str(src), "-o", str(binary)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"Build failed:\n{(result.stderr or '').strip()[:600]}")
            return 1
        shutil.copy(plist, app_dir / "Contents" / "Info.plist")

    subprocess.run(["pkill", "-x", "AwareBar"], capture_output=True)
    time.sleep(0.5)
    subprocess.run(["open", str(app_dir)], capture_output=True)
    print("⚡ Widget is live — look at your menu bar (top right).")
    print("   First run: macOS will ask to allow notifications from Aware — click Allow.")
    print("   Click the bolt: on/off, wake the mind, read the last thing it heard.")
    return 0


# --------------------------------------------------------------------------
# aware test / doctor
# --------------------------------------------------------------------------

TEST_PHRASE = "Aware is online. This is a test of the transcription pipeline."


def cmd_test(cfg, args) -> int:
    _banner("End-to-end test: synthesize speech -> transcribe -> compare")
    with tempfile.TemporaryDirectory() as td:
        aiff = Path(td) / "test.aiff"
        wav = Path(td) / "test.wav"
        subprocess.run(["say", "-o", str(aiff), TEST_PHRASE], check=True)
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(aiff), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
            check=True,
        )
        rms = transcribe.chunk_rms(wav)
        text = transcribe.transcribe_file(wav, cfg)
    print(f'  said:  "{TEST_PHRASE}"')
    print(f'  heard: "{text}" (rms {rms})')
    want = set(TEST_PHRASE.lower().replace(".", "").split())
    got = set(text.lower().replace(".", "").replace(",", "").split())
    overlap = len(want & got)
    if overlap >= len(want) // 2:
        print(f"  PASS ({overlap}/{len(want)} words matched)")
        return 0
    print("  FAIL — transcription did not match")
    return 1


def cmd_doctor(cfg, args) -> int:
    ok = True

    def check(label: str, good: bool, hint: str = "") -> None:
        nonlocal ok
        mark = "\033[32m✓\033[0m" if good else "\033[31m✗\033[0m"
        print(f"  {mark} {label}" + (f"  ({hint})" if hint and not good else ""))
        ok = ok and good

    _banner("Aware doctor")
    check("python 3.11+", sys.version_info >= (3, 11), "need tomllib")
    check("ffmpeg", shutil.which("ffmpeg") is not None, "brew install ffmpeg")
    check(f"whisper ({cfg.transcribe['whisper_bin']})",
          shutil.which(cfg.transcribe["whisper_bin"]) is not None,
          "brew install whisper-cpp")
    model = cfg.whisper_model_path
    check(f"whisper model ({model.name})", model.exists(),
          "download a ggml model into models/")
    check(f"claude CLI ({cfg.brain['claude_bin']})",
          shutil.which(cfg.brain["claude_bin"]) is not None,
          "npm install -g @anthropic-ai/claude-code")
    mic = devices.resolve(cfg.audio["mic_device"])
    check(f"mic device ('{cfg.audio['mic_device']}')", mic is not None,
          "see `aware devices`, update config.toml")
    system = devices.resolve(cfg.audio["system_device"])
    check(f"system-audio device ('{cfg.audio['system_device']}')", system is not None,
          "optional: install BlackHole to capture system audio")
    print()
    print("All good." if ok else "Fix the items above, then re-run `aware doctor`.")
    return 0 if ok else 1


# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="aware",
        description="Aware — real-time senses (ears, eyes, memory) for your AI.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("devices", help="list audio input devices")

    p = sub.add_parser("widget", help="launch the menu-bar widget (builds it first time)")
    p.add_argument("--stop", action="store_true", help="quit the widget")

    sub.add_parser("on", help="switch Aware ON (background)")
    sub.add_parser("off", help="switch Aware OFF — kills the daemon, releases the mic")
    sub.add_parser("toggle", help="flip between ON and OFF")
    sub.add_parser("status", help="is it listening? what did it last hear?")

    p = sub.add_parser("start", help="start listening in the foreground (live console)")
    p.add_argument("--no-brain", action="store_true", help="senses only, no Claude runs")

    p = sub.add_parser("log", help="show recent transcript")
    p.add_argument("--minutes", type=int, default=60)

    p = sub.add_parser("brain", help="wake the brain once, right now")
    p.add_argument("--dry-run", action="store_true",
                   help="print the prompt the brain would receive, don't run Claude")

    sub.add_parser("proposals", help="list playbooks Aware has proposed")

    p = sub.add_parser("approve", help="promote a proposed playbook to active")
    p.add_argument("name")

    sub.add_parser("test", help="self-test the transcription pipeline")
    sub.add_parser("doctor", help="check dependencies and devices")

    # Behave like a proper Unix citizen when piped (`aware log | head`).
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    args = parser.parse_args(argv)
    cfg = config.load()

    commands = {
        "devices": cmd_devices,
        "widget": cmd_widget,
        "on": cmd_on,
        "off": cmd_off,
        "toggle": cmd_toggle,
        "status": cmd_status,
        "start": cmd_start,
        "log": cmd_log,
        "brain": cmd_brain,
        "proposals": cmd_proposals,
        "approve": cmd_approve,
        "test": cmd_test,
        "doctor": cmd_doctor,
    }
    sys.exit(commands[args.command](cfg, args))
