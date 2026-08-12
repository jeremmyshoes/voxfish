
from __future__ import annotations

import logging
import time
from pathlib import Path

import requests

API_BASE = "https://api.fish.audio"
log = logging.getLogger(__name__)


class FishError(Exception):
    """Ошибка, текст которой не стыдно показать пользователю."""


class FishClient:
    def __init__(self, api_key: str, model: str = "s2.1-pro-free") -> None:
        self.model = model
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}"})

    # --- вспомогательное ---
    @staticmethod
    def _explain(status: int, body: str) -> str:
        table = {
            401: "неверный FISH_API_KEY",
            402: "нет кредитов — проверь, что FISH_MODEL=s2.1-pro-free",
            403: "доступ запрещён (ключ без нужных прав)",
            422: f"API отклонил параметры: {body[:150]}",
            429: "слишком много запросов, подожди немного",
        }
        return table.get(status, f"Fish Audio вернул {status}: {body[:150]}")

    # --- клонирование ---
    def create_voice(self, title: str, wav_path: Path, transcript: str | None = None) -> str:
        """Создаёт приватную модель голоса. train_mode=fast — доступна сразу.
        Если transcript не передан, Fish сам распознаёт речь."""
        fields = [
            ("type", (None, "tts")),
            ("title", (None, title)),
            ("train_mode", (None, "fast")),
            ("visibility", (None, "private")),
            ("enhance_audio_quality", (None, "true")),
        ]
        if transcript:
            fields.append(("texts", (None, transcript)))

        with wav_path.open("rb") as handle:
            fields.append(("voices", ("sample.wav", handle, "audio/wav")))
            response = self.session.post(
                f"{API_BASE}/model", files=fields, timeout=180
            )

        if response.status_code not in (200, 201):
            raise FishError(self._explain(response.status_code, response.text))

        model_id = response.json().get("_id")
        if not model_id:
            raise FishError("Fish не вернул идентификатор модели")
        return str(model_id)

    def delete_voice(self, reference_id: str) -> bool:
        try:
            response = self.session.delete(
                f"{API_BASE}/model/{reference_id}", timeout=60
            )
            return response.status_code in (200, 204)
        except requests.RequestException:
            return False

    # --- синтез ---
    def tts(
        self,
        text: str,
        out_path: Path,
        reference_id: str | None = None,
        speed: float = 1.0,
        retries: int = 2,
    ) -> Path:
        payload: dict = {
            "text": text,
            "format": "mp3",
            "mp3_bitrate": 128,
            "normalize": True,
            "latency": "normal",
            "chunk_length": 200,
            "prosody": {"speed": speed, "volume": 0},
        }
        if reference_id:
            payload["reference_id"] = reference_id

        headers = {"Content-Type": "application/json", "model": self.model}
        last_error = "неизвестная ошибка"

        for attempt in range(retries + 1):
            try:
                response = self.session.post(
                    f"{API_BASE}/v1/tts", json=payload, headers=headers, timeout=180
                )
            except requests.RequestException as exc:
                last_error = f"сеть недоступна ({type(exc).__name__})"
                time.sleep(2 ** attempt)
                continue

            if response.status_code == 200:
                if len(response.content) < 1024:
                    raise FishError("пришёл пустой аудиофайл")
                out_path.write_bytes(response.content)
                return out_path

            last_error = self._explain(response.status_code, response.text)
            # повторяем только то, что имеет шанс починиться само
            if response.status_code not in (429, 500, 502, 503, 504):
                break
            time.sleep(2 ** attempt + 1)

        raise FishError(last_error)

    def ping(self) -> None:
        """Быстрая проверка ключа на старте: одно короткое слово."""
        headers = {"Content-Type": "application/json", "model": self.model}
        response = self.session.post(
            f"{API_BASE}/v1/tts",
            json={"text": "ok", "format": "mp3"},
            headers=headers,
            timeout=60,
        )
        if response.status_code != 200:
            raise FishError(self._explain(response.status_code, response.text))
