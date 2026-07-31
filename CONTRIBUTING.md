# Contributing to Aware

Aware's design philosophy, in four rules:

1. **Stdlib only.** The core is pure Python 3.11+ standard library. No pip,
   no venv, no dependency rot. Capabilities come from subprocess seams to
   great tools (ffmpeg, whisper.cpp, an AI CLI), each swappable in config.
2. **Files are the API.** Transcripts are JSONL. Memory is markdown.
   Playbooks are markdown. Access's constitution is markdown. Anything a
   human can read, a human can audit, edit, or delete — that's the trust
   model, so don't bury state in databases or hidden formats.
3. **The brain is a commodity; the senses are the product.** Access is "any
   command that reads a prompt on stdin and prints a reply" (see
   `brain.command`). Never couple the core to one AI vendor.
4. **Every claim in the docs must be true in the code.** This project asks
   for a microphone; its honesty is a feature. If a privacy statement and
   the code disagree, that's a release-blocking bug.

## Repo tour

| Path | What it is |
|---|---|
| `bin/aware` | Launcher (sets PATH/PYTHONPATH, runs the package) |
| `aware/cli.py` | The `aware` command: start, log, brain, proposals, approve, test, doctor |
| `aware/capture.py` | Ears: ffmpeg per source → 30 s WAV chunks (device re-resolved by name on every restart) |
| `aware/transcribe.py` | whisper.cpp on closed chunks; silence gate; hallucination filter; provably-closed chunk lifecycle |
| `aware/transcript.py` | Daily JSONL transcript store + time-window reads |
| `aware/activity.py` | Eyes: frontmost-app log via `lsappinfo` |
| `aware/brain.py` | Access: prompt assembly, AI invocation, NOTIFY/REMEMBER/PLAYBOOK protocol, heartbeat scheduler |
| `aware/devices.py` | avfoundation device discovery/resolution |
| `aware/notify.py` | macOS notifications |
| `aware/config.py` | Defaults + `config.toml` loading (tomllib) |
| `prompts/brain-system.md` | Access's constitution — identity, discipline, learning loop, output protocol |
| `playbooks/` | Active automations; `playbooks/proposed/` holds Access's own drafts awaiting approval |
| `service/` | launchd plist for always-on mode |

Runtime data (all git-ignored, all local): `chunks/` in-flight audio,
`transcripts/`, `context/` activity logs, `memory/` Access's observations,
`state/` brain state, `logs/`.

## Developing

```
./bin/aware doctor        # environment check
./bin/aware test          # say → ffmpeg → whisper round-trip
./bin/aware brain --dry-run   # inspect the exact prompt Access receives
```

`aware start` uses your real microphone; develop the chunk pipeline against
synthetic input instead (ffmpeg `-f lavfi -i sine=frequency=440` writing
into a scratch dir exercises the same segment lifecycle).

## PRs welcome

Diarization, the menu-bar widget, deeper eyes (window titles / screen OCR),
calendar sense, new starter playbooks, Linux/Windows capture backends —
see the README roadmap. Keep rule 4 sacred.
