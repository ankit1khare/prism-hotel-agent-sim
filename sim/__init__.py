"""Simulated hotels for Prism. No phone calls: hotels are Claude plus Rime TTS, and audio stays on this computer."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# JSON files in a run folder that are not calls.
NOT_CALLS = {"grades.json", "run.json"}


def load_env(path: Path = ROOT / ".env") -> None:
    """Read KEY=value lines. Empty values and keys already set in the shell are left alone."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if sep and key and not key.startswith("#") and value and not os.getenv(key):
            os.environ[key] = value
