#!/data/data/com.termux/files/usr/bin/bash
set -e
cd "$(dirname "$0")"
command -v termux-wake-lock >/dev/null && termux-wake-lock
source .venv/bin/activate
exec python bot.py
