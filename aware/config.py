"""Configuration loading for Aware.

Reads config.toml from the project root, layered over sane defaults.
Stdlib only (tomllib) — no third-party dependencies anywhere in Aware.
"""

import tomllib
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "audio": {
        "sources": ["mic"],  # "mic", "system"
        "mic_device": "HyperX QuadCast S",
        "system_device": "BlackHole 2ch",
        "chunk_seconds": 30,
        "keep_audio": False,
    },
    "transcribe": {
        "whisper_bin": "whisper-cli",
        "model": "models/ggml-base.en.bin",
        "language": "en",
        "min_rms": 60,  # int16 RMS below this = silence, chunk skipped
    },
    "activity": {
        "enabled": True,
        "interval_seconds": 15,
    },
    "brain": {
        "persona": "Spinther",  # the mind's name — yours to change
        "enabled": True,
        "interval_seconds": 120,
        "window_minutes": 15,
        "claude_bin": "claude",
        "model": "claude-sonnet-5",
        "permission_mode": "default",
        "timeout_seconds": 300,
        "extra_args": [],
        "command": [],  # non-empty = Bring Your Own Brain, used verbatim
        "notify": True,
    },
    "wake": {
        "phrases": ["hey spinther", "hey aware", "hey claude"],
        "sources": ["mic"],  # only these sources can issue wake commands
    },
}


class Config:
    def __init__(self, data: dict):
        self._data = data

    def __getattr__(self, section: str) -> dict:
        if section.startswith("_"):
            raise AttributeError(section)
        merged = dict(DEFAULTS.get(section, {}))
        merged.update(self._data.get(section, {}))
        return merged

    # Resolved paths ---------------------------------------------------
    @property
    def home(self) -> Path:
        return HOME

    @property
    def chunks_dir(self) -> Path:
        return HOME / "chunks"

    @property
    def transcripts_dir(self) -> Path:
        return HOME / "transcripts"

    @property
    def context_dir(self) -> Path:
        return HOME / "context"

    @property
    def logs_dir(self) -> Path:
        return HOME / "logs"

    @property
    def state_dir(self) -> Path:
        return HOME / "state"

    @property
    def memory_dir(self) -> Path:
        return HOME / "memory"

    @property
    def playbooks_dir(self) -> Path:
        return HOME / "playbooks"

    @property
    def proposed_dir(self) -> Path:
        return HOME / "playbooks" / "proposed"

    @property
    def audio_archive_dir(self) -> Path:
        return HOME / "audio"

    @property
    def whisper_model_path(self) -> Path:
        p = Path(self.transcribe["model"])
        return p if p.is_absolute() else HOME / p

    def ensure_dirs(self) -> None:
        for d in (
            self.chunks_dir,
            self.transcripts_dir,
            self.context_dir,
            self.logs_dir,
            self.state_dir,
            self.memory_dir,
            self.playbooks_dir,
            self.proposed_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


def load() -> Config:
    cfg_path = HOME / "config.toml"
    data = {}
    if cfg_path.exists():
        data = tomllib.loads(cfg_path.read_text())
    cfg = Config(data)
    cfg.ensure_dirs()
    return cfg
