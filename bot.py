from __future__ import annotations

import html
import logging
import signal
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import config as cfg
from audio import AudioError, check_ffmpeg, mp3_to_voice, to_reference_wav
from fish import FishClient, FishError
from storage import Storage
from telegram import Telegram, TelegramError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("voxfish")

CONSENT = (
    "<b>Прежде чем начать</b>

"
    "Бот клонирует голос из присланного аудио через сервис Fish Audio. "
    "Нажимая «Подтверждаю», ты заявляешь, что:
"
    "• это твой голос либо у тебя есть разрешение владельца;
"
    "• ты не будешь выдавать синтез за живого человека и обходить "
    "голосовую аутентификацию.

"
    "Аудио отправляется на серверы Fish Audio и хранится там как приватная "
    "модель голоса. /forget удаляет всё безвозвратно."
)

HELP = (
    "<b>Команды</b>
"
    "/clone — новый голос (10-30 сек чистой речи)
"
    "/voices — список, выбор активного, удаление
"
    "/say текст — озвучить
"
    "/forget — удалить все мои данные

"
    "Или просто пришли текст: озвучу активным голосом."
)

CONSENT_MARKUP = {
    "inline_keyboard": [
        [{"text": "✅ Подтверждаю", "callback_data": "consent:yes"}],
        [{"text": "❌ Отмена", "callback_data": "consent:no"}],
    ]
}


def voices_markup(voices, active_id) -> dict:
    rows = []
    for voice in voices:
        mark = "🔊 " if voice.id == active_id else ""
        rows.append([
            {"text": f"{mark}{voice.name}", "callback_data": f"voice:use:{voice.id}"},
            {"text": "🗑", "callback_data": f"voice:del:{voice.id}"},
        ])
    return {"inline_keyboard": rows}


class Bot:
    def __init__(self) -> None:
        cfg.validate()
        cfg.ensure_dirs()
        check_ffmpeg()

        self.tg = Telegram(cfg.BOT_TOKEN)
        self.fish = FishClient(cfg.FISH_API_KEY, cfg.FISH_MODEL)
        self.db = Storage(cfg.DB_PATH)
        self.pool = ThreadPoolExecutor(max_workers=cfg.WORKERS)
        self.states: dict[int, dict] = {}
        self.states_lock = threading.Lock()
        self.running = True

    # --- состояние диалога ---
    def set_state(self, user_id: int, state: dict | None) -> None:
        with self.states_lock:
            if state is None:
                self.states.pop(user_id, None)
            else:
                self.states[user_id] = state

    def get_state(self, user_id: int) -> dict | None:
        with self.states_lock:
            return self.states.get(user_id)

    def guard(self, chat_id: int, user_id: int) -> bool:
        self.db.ensure_user(user_id)
        if self.db.has_consent(user_id):
            return True
        self.tg.send(chat_id, CONSENT, CONSENT_MARKUP)
        return False

    # --- команды ---
    def cmd_start(self, chat_id: int, user_id: int) -> None:
        self.db.ensure_user(user_id)
        if not self.db.has_consent(user_id):
            self.tg.send(chat_id, CONSENT, CONSENT_MARKUP)
            return
        self.tg.send(chat_id, HELP)

    def cmd_clone(self, chat_id: int, user_id: int) -> None:
        if not self.guard(chat_id, user_id):
            return
        if len(self.db.list_voices(user_id)) >= cfg.MAX_VOICES_PER_USER:
            self.tg.send(
                chat_id,
                f"Лимит {cfg.MAX_VOICES_PER_USER} голосов. Удали лишние: /voices",
            )
            return
        self.set_state(user_id, {"step": "name"})
        self.tg.send(chat_id, "Как назовём голос? Одно короткое слово.")

    def cmd_voices(self, chat_id: int, user_id: int) -> None:
        if not self.guard(chat_id, user_id):
            return
        voices = self.db.list_voices(user_id)
        if not voices:
            self.tg.send(chat_id, "Голосов пока нет. /clone чтобы добавить.")
            return
        user = self.db.get_user(user_id)
        self.tg.send(
            chat_id,
            "Твои голоса (тап — сделать активным, 🗑 — удалить):",
            voices_markup(voices, user["active_voice"]),
        )

    def cmd_forget(self, chat_id: int, user_id: int) -> None:
        voices = self.db.purge_user(user_id)
        for voice in voices:
            self.fish.delete_voice(voice.reference_id)
        self.set_state(user_id, None)
        self.tg.send(chat_id, "Всё удалено — и здесь, и на стороне Fish Audio.")

    # --- приём образца голоса ---
    def handle_sample(self, chat_id: int, user_id: int, media: dict, name: str) -> None:
        if media.get("file_size", 0) > 20 * 1024 * 1024:
            self.tg.send(chat_id, "Файл больше 20 МБ, Telegram такой не отдаст боту.")
            return

        status = self.tg.send(chat_id, "Обрабатываю образец…")
        raw = cfg.TMP_DIR / f"raw_{uuid.uuid4().hex}"
        wav = cfg.TMP_DIR / f"ref_{uuid.uuid4().hex}.wav"
        try:
            self.tg.download(media["file_id"], raw)
            duration = to_reference_wav(raw, wav, cfg.REF_MAX_SECONDS)

            if duration < cfg.REF_MIN_SECONDS:
                self.tg.edit(
                    chat_id, status["message_id"],
                    f"После обрезки тишины осталось {duration:.1f} сек, "
                    f"нужно минимум {cfg.REF_MIN_SECONDS:.0f}. Запиши подлиннее.",
                )
                return

            self.tg.edit(chat_id, status["message_id"], "Создаю модель голоса…")
            reference_id = self.fish.create_voice(name, wav)

            voice_id = self.db.add_voice(user_id, name, reference_id)
            self.db.set_active_voice(user_id, voice_id)
            self.set_state(user_id, None)
            self.tg.edit(
                chat_id, status["message_id"],
                f"Готово: «{html.escape(name)}» ({duration:.1f} сек) сохранён "
                "и выбран активным.
Пришли любой текст — озвучу.",
            )
        except (AudioError, FishError) as exc:
            self.tg.edit(chat_id, status["message_id"], f"Не получилось: {exc}")
        except Exception:
            log.exception("clone failed")
            self.tg.edit(chat_id, status["message_id"], "Внутренняя ошибка, попробуй ещё раз.")
        finally:
            raw.unlink(missing_ok=True)
            wav.unlink(missing_ok=True)

    # --- синтез ---
    def synthesize(self, chat_id: int, user_id: int, text: str) -> None:
        user = self.db.get_user(user_id)
        if not user or not user["active_voice"]:
            self.tg.send(chat_id, "Сначала добавь голос: /clone")
            return

        elapsed = int(time.time()) - (user["last_job_at"] or 0)
        if elapsed < cfg.COOLDOWN_SECONDS:
            self.tg.send(chat_id, f"Погоди {cfg.COOLDOWN_SECONDS - elapsed} сек.")
            return

        text = text.strip()
        if len(text) > cfg.MAX_TEXT_CHARS:
            self.tg.send(
                chat_id,
                f"Максимум {cfg.MAX_TEXT_CHARS} символов, у тебя {len(text)}.",
            )
            return

        voice = self.db.get_voice(user["active_voice"], user_id)
        if not voice:
            self.tg.send(chat_id, "Активный голос пропал. Выбери другой: /voices")
            return

        self.db.touch_job(user_id)
        status = self.tg.send(chat_id, "🎙 Синтезирую…")
        mp3 = cfg.TMP_DIR / f"out_{uuid.uuid4().hex}.mp3"
        ogg = mp3.with_suffix(".ogg")
        try:
            self.fish.tts(text, mp3, reference_id=voice.reference_id)
            mp3_to_voice(mp3, ogg)
            self.tg.send_voice(chat_id, ogg, caption=f"🔊 {voice.name}")
            self.tg.delete_message(chat_id, status["message_id"])
        except (FishError, AudioError) as exc:
            self.tg.edit(chat_id, status["message_id"], f"Не вышло: {exc}")
        except Exception:
            log.exception("tts failed")
            self.tg.edit(chat_id, status["message_id"], "Внутренняя ошибка, попробуй ещё раз.")
        finally:
            mp3.unlink(missing_ok=True)
            ogg.unlink(missing_ok=True)

    # --- маршрутизация ---
    def on_message(self, message: dict) -> None:
        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]
        text = (message.get("text") or "").strip()
        media = message.get("voice") or message.get("audio") or message.get("document")

        if text.startswith("/"):
            command = text.split()[0].split("@")[0]
            argument = text[len(command):].strip()
            if command in ("/start", "/help"):
                self.cmd_start(chat_id, user_id)
            elif command == "/clone":
                self.cmd_clone(chat_id, user_id)
            elif command == "/voices":
                self.cmd_voices(chat_id, user_id)
            elif command == "/forget":
                self.cmd_forget(chat_id, user_id)
            elif command == "/say":
                if not argument:
                    self.tg.send(chat_id, "Пример: <code>/say привет, это мой клон</code>")
                elif self.guard(chat_id, user_id):
                    self.synthesize(chat_id, user_id, argument)
            else:
                self.tg.send(chat_id, "Не знаю такой команды. /help")
            return

        state = self.get_state(user_id)

        if state and state["step"] == "name" and text:
            name = text[:32]
            self.set_state(user_id, {"step": "audio", "name": name})
            self.tg.send(
                chat_id,
                f"Ок, «{html.escape(name)}». Теперь пришли голосовое или аудиофайл: "
                f"{int(cfg.REF_MIN_SECONDS)}-{int(cfg.REF_MAX_SECONDS)} секунд, "
                "без музыки, эха и второго голоса.",
            )
            return

        if state and state["step"] == "audio" and media:
            self.handle_sample(chat_id, user_id, media, state["name"])
            return

        if media:
            self.tg.send(chat_id, "Чтобы добавить этот голос, начни с /clone")
            return

        if text and self.guard(chat_id, user_id):
            self.synthesize(chat_id, user_id, text)

    def on_callback(self, callback: dict) -> None:
        data = callback.get("data", "")
        user_id = callback["from"]["id"]
        message = callback.get("message") or {}
        chat_id = message.get("chat", {}).get("id")
        message_id = message.get("message_id")

        if data == "consent:yes":
            self.db.set_consent(user_id)
            self.tg.edit(chat_id, message_id, "Принято.

" + HELP)
        elif data == "consent:no":
            self.tg.edit(chat_id, message_id, "Без согласия бот не работает. /start когда передумаешь.")
        elif data.startswith("voice:"):
            _, action, raw_id = data.split(":")
            voice = self.db.get_voice(int(raw_id), user_id)
            if not voice:
                self.tg.answer_callback(callback["id"], "Голос не найден")
                return
            if action == "use":
                self.db.set_active_voice(user_id, voice.id)
                self.tg.answer_callback(callback["id"], f"Активный: {voice.name}")
            else:
                self.db.delete_voice(voice.id, user_id)
                self.fish.delete_voice(voice.reference_id)
                user = self.db.get_user(user_id)
                if user and user["active_voice"] == voice.id:
                    self.db.set_active_voice(user_id, None)
                self.tg.answer_callback(callback["id"], f"Удалён: {voice.name}")

            voices = self.db.list_voices(user_id)
            user = self.db.get_user(user_id)
            if voices:
                self.tg.call(
                    "editMessageReplyMarkup",
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=voices_markup(voices, user["active_voice"]),
                )
            else:
                self.tg.edit(chat_id, message_id, "Голосов не осталось. /clone чтобы добавить.")

        self.tg.answer_callback(callback["id"])

    def dispatch(self, update: dict) -> None:
        try:
            if "message" in update:
                self.on_message(update["message"])
            elif "callback_query" in update:
                self.on_callback(update["callback_query"])
        except TelegramError as exc:
            log.warning("telegram: %s", exc)
        except Exception:
            log.exception("unhandled update")

    # --- основной цикл ---
    def stop(self, *_args) -> None:
        log.info("останавливаюсь…")
        self.running = False

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        log.info("проверяю ключ Fish Audio (модель %s)…", cfg.FISH_MODEL)
        self.fish.ping()
        me = self.tg.call("getMe")
        log.info("запущен как @%s", me["username"])

        self.tg.call("deleteWebhook", drop_pending_updates=True)
        offset = 0
        while self.running:
            for update in self.tg.get_updates(offset):
                offset = update["update_id"] + 1
                self.pool.submit(self.dispatch, update)

        self.pool.shutdown(wait=True)
        log.info("остановлен")


if __name__ == "__main__":
    Bot().run()
