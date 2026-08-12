from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


class AudioError(Exception):
    pass


def check_ffmpeg() -> None:
    for binary in ("ffmpeg", "ffprobe"):
        if not shutil.which(binary):
            raise SystemExit(f"{binary} не найден. В Termux: pkg install ffmpeg")


def _run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise AudioError("ffmpeg завис") from exc


def probe_duration(path: Path) -> float:
    proc = _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "json", str(path),
    ], timeout=30)
    if proc.returncode != 0:
        raise AudioError("не удалось прочитать аудио")
    try:
        return float(json.loads(proc.stdout)["format"]["duration"])
    except (KeyError, ValueError) as exc:
        raise AudioError("аудио без длительности") from exc


def to_reference_wav(src: Path, dst: Path, max_seconds: float) -> float:
    """Моно 44.1 кГц, чистка шума, обрезка тишины, нормализация громкости.
    Качество референса решает больше, чем любые параметры модели."""
    proc = _run([
        "ffmpeg", "-y", "-i", str(src),
        "-af",
        "highpass=f=70,afftdn=nf=-25,"
        "silenceremove=start_periods=1:start_silence=0.1:start_threshold=-45dB,"
        "loudnorm=I=-18:TP=-2:LRA=11",
        "-ac", "1", "-ar", "44100", "-t", str(max_seconds),
        "-c:a", "pcm_s16le", str(dst),
    ])
    if proc.returncode != 0 or not dst.exists():
        tail = proc.stderr.decode("utf-8", "ignore")[-300:]
        raise AudioError(f"конвертация не удалась: {tail}")
    return probe_duration(dst)


def mp3_to_voice(src: Path, dst: Path) -> Path:
    """Telegram рисует «волну» только для ogg/opus."""
    proc = _run([
        "ffmpeg", "-y", "-i", str(src),
        "-c:a", "libopus", "-b:a", "48k", "-ar", "48000", "-ac", "1",
        str(dst),
    ])
    if proc.returncode != 0 or not dst.exists():
        raise AudioError("не удалось упаковать")
    return dst
