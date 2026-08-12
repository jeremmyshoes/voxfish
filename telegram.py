from __future__ import annotations

import logging
from pathlib import Path

import requests

log = logging.getLogger(__name__)


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token: str) -> None:
        self.api = f"https://api.telegram.org/bot{token}"
        self.files = f"https://api.telegram.org/file/bot{token}"
        self.session = requests.Session()

    def call(self, method: str, timeout: int = 60, **params):
        response = self.session.post(f"{self.api}/{method}", json=params, timeout=timeout)
        data = response.json()
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description')}")
        return data["result"]

    def get_updates(self, offset: int, long_poll: int = 30) -> list[dict]:
        try:
            return self.call(
                "getUpdates",
                timeout=long_poll + 15,
                offset=offset,
                allowed_updates=["message", "callback_query"],
                **{"timeout": long_poll},
            )
        except requests.RequestException as exc:
            log.warning("polling: %s", exc)
            return []

    def send(self, chat_id: int, text: str, markup: dict | None = None) -> dict:
        params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if markup:
            params["reply_markup"] = markup
        return self.call("sendMessage", **params)

    def edit(self, chat_id: int, message_id: int, text: str, markup: dict | None = None):
        params = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
        }
        if markup:
            params["reply_markup"] = markup
        try:
            return self.call("editMessageText", **params)
        except TelegramError:
            return None  # "message is not modified" — не повод падать

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except TelegramError:
            pass

    def delete_message(self, chat_id: int, message_id: int) -> None:
        try:
            self.call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except TelegramError:
            pass

    def send_voice(self, chat_id: int, path: Path, caption: str = "") -> None:
        with path.open("rb") as handle:
            response = self.session.post(
                f"{self.api}/sendVoice",
                data={"chat_id": chat_id, "caption": caption},
                files={"voice": (path.name, handle, "audio/ogg")},
                timeout=180,
            )
        if not response.json().get("ok"):
            raise TelegramError(response.text[:200])

    def download(self, file_id: str, dst: Path) -> Path:
        info = self.call("getFile", file_id=file_id)
        url = f"{self.files}/{info['file_path']}"
        with self.session.get(url, stream=True, timeout=180) as response:
            response.raise_for_status()
            with dst.open("wb") as handle:
                for chunk in response.iter_content(65536):
                    handle.write(chunk)
        return dst
