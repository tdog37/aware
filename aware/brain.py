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
import queue
import re
import subprocess
from datetime import datetime
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


def load_state(cfg) -> dict:
    p = _state_path(cfg)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            pass
    return {"runs": 0, "last_run": None, "last_transcript_ts": None,
            "recent_notifications": []}


def save_state(cfg, state: dict) -> None:
    _state_path(cfg).write_text(json.dumps(state, indent=2))


# --------------------------------------------------------------------------
# Prompt assembly
# --------------------------------------------------------------------------

def _read(path: Path, default: str = "") -> str:
    return path.read_text() if path.exists() else default


def _memory_tail(cfg, lines: int = 60) -> str:
    text = _read(cfg.memory_dir / "observations.md")
    return "\n".join(text.splitlines()[-lines:])


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
        f"## Why you woke up\n{reason or 'Scheduled heartbeat.'}",
        f"## What Tim is doing on screen (last {window} min)\n{act}",
        f"## Rolling transcript (last {window} min)\n{tx}",
        f"## Your memory (recent observations)\n{_memory_tail(cfg) or '(empty)'}",
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
        line = line.strip()
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


def _has_new_speech(cfg) -> bool:
    entries = transcript.read_window(cfg.transcripts_dir, cfg.brain["window_minutes"])
    if not entries:
        return False
    last_seen = load_state(cfg).get("last_transcript_ts")
    return last_seen is None or entries[-1]["ts"] > last_seen


def scheduler(cfg, stop_event, wake_queue: queue.Queue, log=print) -> None:
    """Heartbeat loop. Runs the brain when there's new speech, or
    immediately when a wake phrase arrives. Stays quiet otherwise."""
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
        if reason is None and not _has_new_speech(cfg):
            continue
        try:
            run_once(cfg, reason=reason, log=log)
        except Exception as e:  # never let the brain kill the daemon
            log(f"[brain] error: {e}")
