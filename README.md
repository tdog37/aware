# Aware — real-time senses for your AI

> Advertisers and device makers spy on you and keep the benefit.
> **What if you spied on yourself — and kept the benefit?**

Aware flips the surveillance economy on its head: the same signals big tech
harvests about you (what you say, what you're working on, when) are captured
*by you*, processed *on your machine*, deleted the moment they're distilled
to text, and put to work by an intelligence whose entire value system is a
markdown file you can open and edit. You are the observer, the observed, and
the sole beneficiary.

**Aware is the body. Access is the mind that wakes up inside it.**

Aware gives an AI what it has never had: awareness of *now*. It listens to
the room, watches what you're working on, knows the time and remembers what
it has seen — so that Access (a Claude agent) can notice what's happening
while it's happening, act on it, and over time **anticipate it**.

The goal, in Tim's words:

> "Once you've seen it happen, you anticipate it happening again, and you've
> created a workflow — a solution better than anything I could ever do —
> because you're connecting all the dots, ongoing, in real time."

## How it works

```
                ┌─────────────────────────────────────────────┐
                │                   AWARE                     │
                │                                             │
  mic ─────────►│  EARS      ffmpeg → whisper.cpp (local)     │
  system audio ►│            rolling transcript, per day      │
  (BlackHole)   │                                             │
  front app ───►│  EYES      what you're working in, when     │
                │                                             │
                │  MEMORY    observations.md — what Access    │
                │            has noticed and learned          │
                │                                             │
                │  ACCESS    the mind: wakes on a heartbeat   │
                │  (brain)   or a wake phrase, reads it all,  │
                │            decides, acts, learns            │
                │                                             │
                │  HANDS     playbooks/ — the automations     │
                │            Access runs, and PROPOSES        │
                └─────────────────────────────────────────────┘
```

- **Ears** — one ffmpeg process per audio source records 30-second chunks;
  whisper.cpp transcribes them locally (nothing leaves the Mac for
  transcription); the audio is deleted, the words are kept in
  `transcripts/YYYY-MM-DD.jsonl`.
- **Eyes** — every 15 s Aware notes which app is frontmost, so Access knows
  "Tim's been in Premiere for the last 40 minutes."
- **Access** — every 2 minutes (only if someone actually spoke), Claude gets
  the last 15 minutes of transcript + activity + its own memory + the
  playbooks, and decides what to do. Saying **"hey access"** out loud wakes it
  instantly and makes that sentence a direct order.
- **The learning loop** — Access records observations (`REMEMBER:`), and when
  it sees a pattern repeat, it writes a **new playbook itself** into
  `playbooks/proposed/`. You approve with one command, and Access got
  permanently smarter. You never had to set up the task.

## Quickstart

```
./bin/aware doctor      # check dependencies and devices
./bin/aware test        # prove the transcription pipeline works
./bin/aware start       # go live (Ctrl-C to stop)
```

First launch: macOS will ask to allow microphone access for your terminal —
click OK, and if the first chunks come up empty, restart `aware start` once.

Day-to-day:

```
./bin/aware log               # what has Aware heard lately?
./bin/aware brain --dry-run   # see exactly what Access sees
./bin/aware brain             # wake Access manually right now
./bin/aware proposals         # playbooks Access has invented
./bin/aware approve <name>    # activate one — Access leveled up
```

## Hearing the other side of the call

Out of the box Aware hears your mic. To also hear Zoom callers / computer
audio (BlackHole is already installed on this Mac):

1. **Audio MIDI Setup** → create a **Multi-Output Device** = Mac Studio
   Speakers + BlackHole 2ch, and set it as the system output.
2. In `config.toml`: `sources = ["mic", "system"]`.

Transcript lines are then tagged `mic:` (you) vs `system:` (them).

## Giving Access hands

Access runs headless Claude (`claude -p`). Its power = whatever tools that
CLI has:

- `permission_mode = "acceptEdits"` in config.toml lets Access write files
  (meeting notes, session logs) inside this folder.
- `claude mcp add …` gives Access real tools — calendar, Zoom recordings,
  Slack, Premiere. Note: connectors attached to the claude.ai *app* don't
  automatically exist for the headless CLI; add them with `claude mcp` so
  Access has them too.

## Bring your own brain

Access's mind is not welded to one vendor. The brain contract is the most
universal interface in computing: *a command that reads a prompt on stdin
and prints a reply*. By default that's `claude -p` (OAuth login with your
Claude subscription, always the latest model). One line in `config.toml`
swaps it:

```toml
[brain]
command = ["codex", "exec"]            # OpenAI, ChatGPT login
# command = ["gemini", "-p"]           # Google login
# command = ["ollama", "run", "llama3"] # 100% local — nothing leaves the Mac
```

Aware never touches an API key; login lives in whichever CLI you chose.

## Make it yours

Two files turn Aware into *your* Access, and they're meant to be rewritten:

- `config.toml` — your devices, your wake phrases, your cadence.
- `prompts/brain-system.md` — Access's constitution: who you are, how it
  should behave, what it values. It's plain markdown. Reading it *is* the
  transparency model: Access has no values you can't open in a text editor.

Everything personal that Aware produces (transcripts, memory, state,
proposals) is git-ignored, so the repo stays shareable while your life
stays yours.

## Privacy & consent

- Everything is local: audio → text on this Mac, each audio chunk deleted
  the moment it's transcribed (`keep_audio = false`), transcripts/memory
  git-ignored, nothing uploaded except the context window sent to Claude
  when Access wakes. (If you quit mid-chunk, the last ~30 s WAV waits in
  `chunks/` and is transcribed-and-deleted on next start.)
- **Consent:** this machine hears everyone in the room and on the call. In
  California (and other all-party-consent states), recording a private
  conversation requires everyone's consent. You're already used to this from
  Zoom/Otter — do the same here: tell people, or stop Aware entirely
  (Ctrl-C) when it's not appropriate. (Note: `--no-brain` only turns off the
  Claude runs — it still transcribes everything it hears.)

## Always on

When you're ready for Aware to run 24/7:

```
cp service/com.aware.access.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.aware.access.plist
```

Two macOS realities to know:

- The plist bakes in the PATH to Homebrew and your node install (launchd
  doesn't have your shell's PATH). If your setup differs, edit those paths
  in the plist.
- **Microphone permission is per-app.** Granting it to Terminal does NOT
  cover the background agent. After loading, say something and check
  `./bin/aware log` — if the transcript stays empty, look at
  `logs/daemon.err.log` and grant mic access to the listed process in
  System Settings → Privacy & Security → Microphone, then
  `launchctl kickstart -k gui/$(id -u)/com.aware.access`.

Remove with `launchctl unload ~/Library/LaunchAgents/com.aware.access.plist`.

## Roadmap

1. **The Access widget** — a menu-bar presence (the little monitor orb —
   yes, the Halo homage survives the rename):
   glows when Access has something, click to see its latest thought, talk
   back without leaving whatever you're doing.
2. **Deeper eyes** — window titles, open documents, periodic screen OCR;
   "Tim is editing *S03E04_rough.prproj*" instead of just "Premiere".
3. **Real hands** — wire Access's CLI to the Zoom, Calendar, Otter, Slack and
   Premiere MCPs so playbooks like *interview-wrap* can actually fetch the
   recording and build the Premiere project.
4. **Calendar sense** — feed today's events into every brain prompt, so
   "the 2pm with Sarah" is something Access just knows.
5. **Speaker separation** — diarization, so transcripts say who said what.
6. **Two-stage brain** — a cheap fast model triages every wakeup; the big
   model only spins up when something's actually happening.
7. **Access everywhere** — companion capture on your other devices feeding
   the same transcript and memory, so Access's awareness follows you. The
   rule that keeps this honest: Access knows only what you've explicitly
   given it permission to sense, its values live in a file you can read and
   edit (`prompts/brain-system.md`), and it serves the person who invited
   it. That's how it helps the wider world too — one genuinely-served
   person at a time, by consent, never by deciding what people need.
