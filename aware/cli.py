"""The `aware` command."""

import argparse
import queue
import re
import shutil
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

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down…")
        stop.set()
        for c in caps:
            c.stop()
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

    p = sub.add_parser("start", help="start listening (capture + transcribe + brain)")
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

    args = parser.parse_args(argv)
    cfg = config.load()

    commands = {
        "devices": cmd_devices,
        "start": cmd_start,
        "log": cmd_log,
        "brain": cmd_brain,
        "proposals": cmd_proposals,
        "approve": cmd_approve,
        "test": cmd_test,
        "doctor": cmd_doctor,
    }
    sys.exit(commands[args.command](cfg, args))
