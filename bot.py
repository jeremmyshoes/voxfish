import html
import logging
import signal
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import config as cfg
from audio import AudioError, check, opus, reference
from fish import Fish, FishError
from storage import Storage
from telegram import TG, TelegramError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("voxfish")

CONSENT = (
    "<b>Согласие</b>"
    "Я подтверждаю, что это мой голос или у меня есть разрешение владельца. "
    "Аудио отправится в Fish Audio и будет храниться как приватная модель. "
    "Я не буду выдавать синтез за человека или обходить голосовую аутентификацию."
)

HELP = (
    "<b>Команды</b>"
    "/clone - добавить голос"
    "/voices - выбрать или удалить"
    "/say текст - озвучить"
    "/forget - удалить данные"
    "Или просто пришли текст."
)

CONSENT_MARKUP = {
    "inline_keyboard": [
        [{"text": "Подтверждаю", "callback_data": "consent:yes"}],
        [{"text": "Отмена", "callback_data": "consent:no"}],
    ]
}


def voices_markup(voices, active_id):
    rows = []
    for voice in voices:
        mark = "🔊 " if voice.id == active_id else ""
        rows.append([
            {"text": f"{mark}{voice.name}", "callback_data": f"voice:use:{voice.id}"},
            {"text": "Удалить", "callback_data": f"voice:del:{voice.id}"},
        ])
    return {"inline_keyboard": rows}


class App:
    def __init__(self):
        cfg.init()
        check()
        self.tg = TG(cfg.BOT_TOKEN)
        self.fish = Fish(cfg.FISH_API_KEY, cfg.FISH_MODEL)
        self.db = Storage(cfg.DB_PATH)
        self.pool = ThreadPoolExecutor(max_workers=cfg.WORKERS)
        self.states = {}
        self.lock = threading.Lock()
        self.running = True

    def guard(self, chat_id, user_id):
        self.db.ensure(user_id)
        if self.db.consent(user_id):
            return True
        self.tg.send(chat_id, CONSENT, CONSENT_MARKUP)
        return False

    def get_state(self, user_id):
        with self.lock:
            return self.states.get(user_id)

    def set_state(self, user_id, value):
        with self.lock:
            if value is False:
                self.states.pop(user_id, None)
            else:
                self.states[user_id] = value

    def clone(self, chat_id, user_id, media, name):
        status = self.tg.send(chat_id, "Обрабатываю образец...")
        raw = cfg.TMP_DIR / f"raw_{uuid.uuid4().hex}"
        wav = cfg.TMP_DIR / f"ref_{uuid.uuid4().hex}.wav"
        try:
            self.tg.download(media["file_id"], raw)
            duration = reference(raw, wav, cfg.REF_MAX_SECONDS)
            if duration < cfg.REF_MIN_SECONDS:
                self.tg.edit(
                    chat_id,
                    status["message_id"],
                    f"Нужно минимум {cfg.REF_MIN_SECONDS:.0f} секунд чистой речи.",
                )
                return

            self.tg.edit(chat_id, status["message_id"], "Создаю модель голоса...")
            reference_id = self.fish.create_voice(name, wav)
            voice_id = self.db.add(user_id, name, reference_id)
            self.db.active(user_id, voice_id)
            self.set_state(user_id, False)
            self.tg.edit(
                chat_id,
                status["message_id"],
                f"Готово: «{html.escape(name)}» выбрано. Пришли текст.",
            )
        except (AudioError, FishError, TelegramError) as exc:
            self.tg.edit(chat_id, status["message_id"], f"Не получилось: {exc}")
        finally:
            raw.unlink(missing_ok=True)
            wav.unlink(missing_ok=True)

    def synthesize(self, chat_id, user_id, text):
        user = self.db.user(user_id)
        if not user or not user["active_voice"]:
            self.tg.send(chat_id, "Сначала /clone")
            return

        elapsed = int(time.time()) - (user["last_job_at"] or 0)
        if elapsed < cfg.COOLDOWN_SECONDS:
            self.tg.send(chat_id, "Погоди немного.")
            return
        if len(text) > cfg.MAX_TEXT_CHARS:
            self.tg.send(chat_id, f"Максимум {cfg.MAX_TEXT_CHARS} символов.")
            return

        voice = self.db.voice(user["active_voice"], user_id)
        if not voice:
            self.tg.send(chat_id, "Голос не найден.")
            return

        self.db.touch(user_id)
        status = self.tg.send(chat_id, "Синтезирую...")
        mp3 = cfg.TMP_DIR / f"{uuid.uuid4().hex}.mp3"
        ogg = mp3.with_suffix(".ogg")
        try:
            self.fish.tts(text, mp3, voice.reference_id)
            opus(mp3, ogg)
            self.tg.voice(chat_id, ogg, voice.name)
            self.tg.delete(chat_id, status["message_id"])
        except (AudioError, FishError, TelegramError) as exc:
            self.tg.edit(chat_id, status["message_id"], f"Ошибка: {exc}")
        finally:
            mp3.unlink(missing_ok=True)
            ogg.unlink(missing_ok=True)

    def handle_message(self, message):
        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]
        text = (message.get("text") or "").strip()
        media = message.get("voice") or message.get("audio") or message.get("document")
        state = self.get_state(user_id)

        if text.startswith("/"):
            command = text.split()[0].split("@")[0]
            argument = text[len(command):].strip()

            if command in ("/start", "/help"):
                self.db.ensure(user_id)
                if self.db.consent(user_id):
                    self.tg.send(chat_id, HELP)
                else:
                    self.tg.send(chat_id, CONSENT, CONSENT_MARKUP)
            elif command == "/clone":
                if self.guard(chat_id, user_id):
                    self.set_state(user_id, {"step": "name"})
                    self.tg.send(chat_id, "Название голоса?")
            elif command == "/voices":
                if self.guard(chat_id, user_id):
                    voices = self.db.voices(user_id)
                    user = self.db.user(user_id)
                    markup = voices_markup(voices, user["active_voice"]) if voices else None
                    self.tg.send(chat_id, "Голоса:", markup)
            elif command == "/forget":
                for voice in self.db.purge(user_id):
                    self.fish.delete_voice(voice.reference_id)
                self.set_state(user_id, False)
                self.tg.send(chat_id, "Данные удалены.")
            elif command == "/say" and argument:
                if self.guard(chat_id, user_id):
                    self.synthesize(chat_id, user_id, argument)
            else:
                self.tg.send(chat_id, "Используй /help")
            return

        if state and state.get("step") == "name" and text:
            self.set_state(user_id, {"step": "audio", "name": text[:32]})
            self.tg.send(chat_id, "Пришли голосовое на 8-30 секунд.")
            return

        if state and state.get("step") == "audio" and media:
            self.clone(chat_id, user_id, media, state["name"])
            return

        if text and self.guard(chat_id, user_id):
            self.synthesize(chat_id, user_id, text)

    def handle_callback(self, callback):
        data = callback.get("data", "")
        user_id = callback["from"]["id"]
        message = callback.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        message_id = message.get("message_id")

        if data == "consent:yes":
            self.db.set_consent(user_id)
            self.tg.edit(chat_id, message_id, HELP)
        elif data == "consent:no":
            self.tg.edit(chat_id, message_id, "Без согласия бот не работает.")
        elif data.startswith("voice:"):
            _, action, raw_id = data.split(":")
            voice = self.db.voice(int(raw_id), user_id)
            if voice:
                if action == "use":
                    self.db.active(user_id, voice.id)
                    self.tg.callback(callback["id"], f"Активный: {voice.name}")
                else:
                    self.db.delete(voice.id, user_id)
                    self.fish.delete_voice(voice.reference_id)
                    self.tg.callback(callback["id"], "Удалён")
        self.tg.callback(callback["id"])

    def dispatch(self, update):
        try:
            if "message" in update:
                self.handle_message(update["message"])
            elif "callback_query" in update:
                self.handle_callback(update["callback_query"])
        except Exception:
            log.exception("update failed")

    def run(self):
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "running", False))
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "running", False))
        self.fish.ping()
        self.tg.call("deleteWebhook", drop_pending_updates=True)
        offset = 0
        while self.running:
            for update in self.tg.updates(offset):
                offset = update["update_id"] + 1
                self.pool.submit(self.dispatch, update)
        self.pool.shutdown(wait=True)


if __name__ == "__main__":
    App().run()
