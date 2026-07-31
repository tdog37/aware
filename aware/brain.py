"""The Brain: a Claude agent that wakes up on a heartbeat (and instantly
on a wake phrase), reads everything Aware currently knows — transcript,
screen activity, memory, playbooks, state — and decides what, if
anything, to do about it.

Output protocol (parsed from the model's reply):
  NOTIFY: <message>          -> macOS notification to Tim
  REMEMBER: <observation>    -> appended to memory/observations.md
  <<<PLAYBOOK slug>>> ... <<<END PLAYBOOK>>>
                             -> saved to playbooks/proposed/<slug>.md
                                for Tim to approve
Everything else is reasoning, kept in the brain log.
"""

import json
import os
import queue
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import activity, notify, transcript

PLAYBOOK_BLOCK = re.compile(
    r"<<<PLAYBOOK\s+([A-Za-z0-9_-]+)>>>\s*\n(.*?)<<<END PLAYBOOK>>>", re.DOTALL
)


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def _state_path(cfg) -> Path:
    return cfg.state_dir / "brain.json"


def load_state(cfg, strict: bool = False) -> dict:
    """strict=True raises on an unreadable file, so gates can fail CLOSED
    rather than resurrecting a briefing that already ran."""
    p = _state_path(cfg)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            if strict:
                raise
    return {"runs": 0, "last_run": None, "last_transcript_ts": None,
            "recent_notifications": []}


def save_state(cfg, state: dict) -> None:
    # Atomic: a concurrent reader sees the old file or the new one, never a
    # half-written one (the daemon and manual `aware brain` runs overlap).
    p = _state_path(cfg)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, p)


# --------------------------------------------------------------------------
# Prompt assembly
# --------------------------------------------------------------------------

def _read(path: Path, default: str = "") -> str:
    return path.read_text() if path.exists() else default


def _memory_tail(cfg, lines: int = 60) -> str:
    text = _read(cfg.memory_dir / "observations.md")
    return "\n".join(text.splitlines()[-lines:])


_SCHEDULE_CACHE: tuple[float, str] | None = None
_SCHEDULE_TTL = 600  # the day's schedule does not change every two minutes


def _today_schedule(cfg) -> str:
    """Calendar sense: the mind should never have to ask what day it is or
    what's on it. Injected into every wake, memoized so a network feed (or a
    slow AppleScript fallback) isn't paid for on every heartbeat."""
    global _SCHEDULE_CACHE
    if _SCHEDULE_CACHE and time.time() - _SCHEDULE_CACHE[0] < _SCHEDULE_TTL:
        return _SCHEDULE_CACHE[1]
    script = cfg.home / "bin" / "today-calendar"
    if not script.exists():
        return "(no calendar reader installed)"
    try:
        out = subprocess.run([str(script)], capture_output=True, text=True,
                             timeout=45, cwd=str(cfg.home)).stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return "(calendar unavailable)"
    result = out or "(nothing on the calendar today)"
    _SCHEDULE_CACHE = (time.time(), result)
    return result


def _dossier_index(cfg) -> str:
    """One line per dossier so the mind knows what files it already keeps."""
    d = cfg.memory_dir / "dossiers"
    if not d.exists():
        return "(no dossiers yet)"
    lines = []
    for p in sorted(d.glob("*.md")):
        first = next((ln.strip("# ").strip()
                      for ln in p.read_text().splitlines() if ln.strip()), "")
        lines.append(f"- {p.stem}: {first[:80]}")
    return "\n".join(lines) if lines else "(no dossiers yet)"


def _playbooks_section(cfg) -> str:
    parts = []
    for p in sorted(cfg.playbooks_dir.glob("*.md")):
        parts.append(f"### Playbook: {p.stem}\n{p.read_text().strip()}")
    return "\n\n".join(parts) if parts else "(no playbooks yet)"


def _proposed_list(cfg) -> str:
    names = [p.stem for p in sorted(cfg.proposed_dir.glob("*.md"))]
    return ", ".join(names) if names else "(none)"


def build_prompt(cfg, reason: str | None = None, entries: list[dict] | None = None) -> str:
    bcfg = cfg.brain
    now = datetime.now()
    window = bcfg["window_minutes"]

    if entries is None:
        entries = transcript.read_window(cfg.transcripts_dir, window)
    tx = transcript.render(entries) or "(no speech in this window)"

    acts = activity.read_window(cfg.context_dir, window)
    act = activity.render(acts) or "(no app switches observed in this window)"

    state = load_state(cfg)

    sections = [
        _read(cfg.home / "prompts" / "brain-system.md"),
        f"## Current time\n{now.strftime('%A, %B %d, %Y — %I:%M %p')}",
        f"## Today's calendar\n{_today_schedule(cfg)}",
        f"## Why you woke up\n{reason or 'Scheduled heartbeat.'}",
        f"## What Tim is doing on screen (last {window} min)\n{act}",
        f"## Rolling transcript (last {window} min)\n{tx}",
        f"## Your memory (recent observations)\n{_memory_tail(cfg) or '(empty)'}",
        f"## Dossiers you keep\n{_dossier_index(cfg)}",
        f"## Active playbooks\n{_playbooks_section(cfg)}",
        f"## Proposed playbooks awaiting Tim's approval\n{_proposed_list(cfg)}",
        f"## Brain state\n```json\n{json.dumps(state, indent=2)}\n```",
    ]
    return "\n\n".join(s for s in sections if s and s.strip())


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------

def _log_path(cfg) -> Path:
    return cfg.logs_dir / f"brain-{datetime.now().strftime('%Y-%m-%d')}.log"


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return slug or "playbook"


def _handle_output(cfg, output: str, log=print) -> None:
    state = load_state(cfg)
    persona = cfg.brain["persona"]

    for m in PLAYBOOK_BLOCK.finditer(output):
        slug, body = _slugify(m.group(1)), m.group(2).strip() + "\n"
        dest = cfg.proposed_dir / f"{slug}.md"
        n = 2
        while dest.exists():
            dest = cfg.proposed_dir / f"{slug}-{n}.md"
            n += 1
        dest.write_text(body)
        log(f"[brain] proposed new playbook: {dest.name}")
        if cfg.brain["notify"]:
            notify.send(f"{persona} learned something",
                        f"New playbook proposed: {dest.stem} — review with `aware proposals`")

    for line in output.splitlines():
        # Models decorate protocol lines ("**NOTIFY: …**") — normalize first.
        line = line.strip().lstrip("#*_ ").rstrip("*_ ").strip()
        if line.startswith("NOTIFY:"):
            msg = line[len("NOTIFY:"):].strip()
            if msg:
                if cfg.brain["notify"]:
                    notify.send(persona, msg)
                state["recent_notifications"] = (
                    state.get("recent_notifications", []) + [msg]
                )[-10:]
                log(f"[brain] NOTIFY: {msg}")
        elif line.startswith("REMEMBER:"):
            obs = line[len("REMEMBER:"):].strip()
            if obs:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M")
                with (cfg.memory_dir / "observations.md").open("a") as f:
                    f.write(f"- [{ts}] {obs}\n")
                log(f"[brain] REMEMBER: {obs}")

    save_state(cfg, state)


def run_once(cfg, reason: str | None = None, dry_run: bool = False, log=print) -> str:
    # Snapshot the transcript BEFORE thinking starts: speech that arrives
    # while the model is running must count as unseen for the next cycle.
    entries = transcript.read_window(cfg.transcripts_dir, cfg.brain["window_minutes"])
    newest_ts = entries[-1]["ts"] if entries else None
    prompt = build_prompt(cfg, reason=reason, entries=entries)
    if dry_run:
        return prompt

    bcfg = cfg.brain
    # Bring Your Own Brain: a non-empty brain.command in config.toml is used
    # verbatim (prompt on stdin) — codex, gemini, a local model, anything.
    cmd = list(bcfg.get("command") or [])
    if not cmd:
        cmd = [
            bcfg["claude_bin"],
            "-p",
            "--model", bcfg["model"],
            "--permission-mode", bcfg["permission_mode"],
        ] + list(bcfg.get("extra_args", []))

    started = datetime.now()
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=bcfg["timeout_seconds"],
            cwd=str(cfg.home),
        )
        output = proc.stdout
        if proc.returncode != 0:
            output += f"\n[brain] claude exited {proc.returncode}: {(proc.stderr or '').strip()[:500]}"
    except subprocess.TimeoutExpired:
        output = "[brain] claude timed out"
    except FileNotFoundError:
        output = f"[brain] claude binary not found: {bcfg['claude_bin']}"

    with _log_path(cfg).open("a") as f:
        f.write(
            f"\n{'=' * 70}\n{started.isoformat(timespec='seconds')} "
            f"(reason: {reason or 'heartbeat'})\n{'-' * 70}\n{output.strip()}\n"
        )

    _handle_output(cfg, output, log=log)

    state = load_state(cfg)
    state["runs"] = state.get("runs", 0) + 1
    state["last_run"] = started.isoformat(timespec="seconds")
    if newest_ts:
        state["last_transcript_ts"] = newest_ts
    save_state(cfg, state)
    return output


TRIAGE_PROMPT = """You are the fast triage stage of Aware, an ambient assistant on
Tim's Mac. Below is what was heard in the room (and seen on screen) recently.
Decide whether the full mind — expensive, thorough — should wake to look at it.

ESCALATE for: anyone addressing the assistant directly; a real two-way
conversation or meeting between people who are present; an event a playbook
covers (an interview or recording session wrapping up, a meeting ending);
anything clearly actionable for Tim.

SKIP for: TV, movies, YouTube, music, ads, podcasts, or game audio; fragments
and background chatter; Tim talking to himself with nothing actionable;
anything that is plainly just media playing in the room.

Reply with EXACTLY one line, nothing else:
ESCALATE: <one short reason>
or
SKIP: <one short reason>
"""


def triage(cfg, entries: list[dict]) -> tuple[bool, str]:
    """Cheap first-stage look. Returns (should_wake_full_mind, reason).
    Fails open — a broken triage must never make Aware miss real life."""
    bcfg = cfg.brain
    model = bcfg.get("triage_model") or ""
    if not model:
        return True, "triage disabled"
    acts = activity.read_window(cfg.context_dir, bcfg["window_minutes"])
    prompt = (
        TRIAGE_PROMPT
        + "\n## Heard\n" + (transcript.render(entries) or "(nothing)")
        + "\n\n## On screen\n" + (activity.render(acts) or "(no app switches)")
    )
    cmd = [bcfg["claude_bin"], "-p", "--model", model]
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              timeout=120, cwd=str(cfg.home))
        lines = [ln for ln in (proc.stdout or "").strip().splitlines() if ln.strip()]
        verdict = lines[-1].strip() if lines else ""
    except (subprocess.TimeoutExpired, OSError):
        return True, "triage failed -> escalating"
    if verdict.upper().startswith("SKIP"):
        return False, verdict
    return True, verdict or "empty triage reply -> escalating"


def _has_new_speech(cfg) -> bool:
    entries = transcript.read_window(cfg.transcripts_dir, cfg.brain["window_minutes"])
    if not entries:
        return False
    try:
        last_seen = load_state(cfg, strict=True).get("last_transcript_ts")
    except json.JSONDecodeError:
        return False  # unreadable state: stay quiet rather than re-run
    return last_seen is None or entries[-1]["ts"] > last_seen


BRIEFING_REASON = (
    "MORNING BRIEFING. Tim's day is starting. This is the one wake where you "
    "speak first and at length. Look at today's calendar, `./bin/recent-mail`, "
    "your recent observations, and your dossiers, then send ONE briefing that "
    "earns its place: what's on today, what you noticed yesterday that still "
    "matters, anything that needs him before it becomes urgent, and where you "
    "can help. Be specific and useful, not a status report. If there is "
    "genuinely nothing worth saying, say so in one line."
)


BRIEFING_WINDOW_MINUTES = 120  # how late a "morning" briefing may still fire
_briefing_warned: set[str] = set()


def _briefing_state(cfg, log=print) -> str:
    """'due' -> brief now · 'stale' -> the window passed, mark the day done
    WITHOUT briefing · 'no' -> nothing to do.

    A lower bound alone means "your day is starting" can fire at 8 PM after a
    late daemon start — which it did, at 1:54 PM, during development."""
    at = (cfg.briefing.get("at") or "").strip()
    if not cfg.briefing.get("enabled", True) or not at:
        return "no"
    hh = mm = -1
    try:
        hh, mm = (int(x) for x in at.split(":", 1))
    except ValueError:
        pass
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        if at not in _briefing_warned:  # warn once, not every heartbeat
            _briefing_warned.add(at)
            log(f'[brain] [briefing] at = "{at}" is not 24-hour "HH:MM" '
                f"— briefing disabled")
        return "no"

    now = datetime.now()
    try:
        last = load_state(cfg, strict=True).get("last_briefing")
    except json.JSONDecodeError:
        return "no"  # unreadable state: assume already briefed, never repeat
    if last == now.strftime("%Y-%m-%d"):
        return "no"

    start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now < start:
        return "no"
    if now - start > timedelta(minutes=BRIEFING_WINDOW_MINUTES):
        return "stale"
    return "due"


def scheduler(cfg, stop_event, wake_queue: queue.Queue, log=print) -> None:
    """Heartbeat loop. Runs the brain when there's new speech, immediately
    when a wake phrase arrives, and once a day for the morning briefing.
    Stays quiet otherwise."""
    interval = cfg.brain["interval_seconds"]
    while not stop_event.is_set():
        reason = None
        try:
            entry = wake_queue.get(timeout=interval)
            reason = f'Wake phrase heard: "{entry["text"]}"'
        except queue.Empty:
            pass
        if stop_event.is_set():
            return

        # The briefing speaks first — it doesn't wait for Tim to say anything.
        if reason is None:
            due = _briefing_state(cfg, log=log)
            if due != "no":
                state = load_state(cfg)
                state["last_briefing"] = datetime.now().strftime("%Y-%m-%d")
                save_state(cfg, state)
                if due == "stale":
                    log("[brain] briefing window passed — skipping today's")
                    continue
                try:
                    run_once(cfg, reason=BRIEFING_REASON, log=log)
                except Exception as e:
                    log(f"[brain] briefing error: {e}")
                continue

        if reason is None and not _has_new_speech(cfg):
            continue
        try:
            if reason is None:  # heartbeat: cheap mind decides first
                entries = transcript.read_window(
                    cfg.transcripts_dir, cfg.brain["window_minutes"])
                go, why = triage(cfg, entries)
                log(f"[brain] triage: {why}")
                with _log_path(cfg).open("a") as f:
                    f.write(f"{datetime.now().isoformat(timespec='seconds')} "
                            f"triage: {why}\n")
                if not go:
                    # Mark this speech as seen so the same TV scene doesn't
                    # get re-triaged every heartbeat forever.
                    state = load_state(cfg)
                    if entries:
                        state["last_transcript_ts"] = entries[-1]["ts"]
                    state["triage_skips"] = state.get("triage_skips", 0) + 1
                    save_state(cfg, state)
                    continue
                reason = f"Triage escalated — {why}"
            run_once(cfg, reason=reason, log=log)
        except Exception as e:  # never let the brain kill the daemon
            log(f"[brain] error: {e}")
