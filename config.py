"""Конфиг без python-dotenv: меньше зависимостей — меньше сюрпризов."""
from __future__ import annotations

import os
from pathlib import Path


def _load_env(path: str = ".env") -> None:
    env_file = Path(path)
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
FISH_API_KEY = os.environ.get("FISH_API_KEY", "")
FISH_MODEL = os.environ.get("FISH_MODEL", "s2.1-pro-free")

MAX_TEXT_CHARS = int(os.environ.get("MAX_TEXT_CHARS", 1000))
MAX_VOICES_PER_USER = int(os.environ.get("MAX_VOICES_PER_USER", 5))
REF_MIN_SECONDS = float(os.environ.get("REF_MIN_SECONDS", 8))
REF_MAX_SECONDS = float(os.environ.get("REF_MAX_SECONDS", 30))
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", 5))
WORKERS = int(os.environ.get("WORKERS", 3))

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
TMP_DIR = DATA_DIR / "tmp"
DB_PATH = DATA_DIR / "voxfish.db"


def ensure_dirs() -> None:
    for directory in (DATA_DIR, TMP_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def validate() -> None:
    missing = [
        name
        for name, value in (("BOT_TOKEN", BOT_TOKEN), ("FISH_API_KEY", FISH_API_KEY))
        if not value
    ]
    if missing:
        raise SystemExit(
            f"Не заданы переменные: {', '.join(missing)}. "
            "Скопируй .env.example в .env и заполни."
        )
