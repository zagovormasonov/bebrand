# -*- coding: utf-8 -*-

import asyncio
import json
import logging
import os
import re
import sqlite3
import smtplib
from pathlib import Path
from typing import Any, Sequence, Mapping, MutableMapping

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

from openai import OpenAI  # ✅ Новый импорт

# ---------------------------------------------------------------------------
# Конфигурация из переменных окружения
# ---------------------------------------------------------------------------
API_TOKEN: str | None = os.getenv("API_TOKEN")
OPENAI_API_KEY: str | None = os.getenv("OPENAI_API_KEY")
ALERT_CHAT_ID_RAW: str | None = os.getenv("ALERT_CHAT_ID")
EMAIL_FROM: str | None = os.getenv("EMAIL_FROM")
EMAIL_TO: str | None = os.getenv("EMAIL_TO")
SMTP_PASSWORD: str | None = os.getenv("SMTP_PASSWORD", os.getenv("YANDEX_APP_PASSWORD"))
SMTP_HOST: str = os.getenv("SMTP_HOST", "smtp.gmail.com")
RENDER_DATA_DIR: str = os.getenv("RENDER_DATA_DIR", "/tmp")

_required: Sequence[tuple[str, str | None]] = [
    ("API_TOKEN", API_TOKEN),
    ("OPENAI_API_KEY", OPENAI_API_KEY),
    ("ALERT_CHAT_ID", ALERT_CHAT_ID_RAW),
    ("EMAIL_FROM", EMAIL_FROM),
    ("EMAIL_TO", EMAIL_TO),
    ("SMTP_PASSWORD", SMTP_PASSWORD),
]
_missing = [name for name, val in _required if not val]
if _missing:
    raise RuntimeError(f"Missing environment variables: {', '.join(_missing)}")

ALERT_CHAT_ID: int = int(ALERT_CHAT_ID_RAW)  # type: ignore[arg-type]

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OpenAI & Telegram-бот
# ---------------------------------------------------------------------------
client = OpenAI(api_key=OPENAI_API_KEY)  # ✅ Новый клиент
bot = Bot(token=API_TOKEN)  # type: ignore[arg-type]
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ---------------------------------------------------------------------------
# Регулярное выражение для телефонов
# ---------------------------------------------------------------------------
PHONE_REGEX = re.compile(r"(\+?\d[\d\s\-]{7,}\d)")

# ---------------------------------------------------------------------------
# Системный промт для ChatGPT
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """
Отвечай только на русском языке.
Ответь как можно правдивее, используя предоставленный контекст выше.
Тебе нужно не только ответить на вопросы, но продать клиенту бесплатную проверку его товарного знака.
Для этого можно задавать вопросы по регистрации.
Тебе нужно подсветить всевозможные подводные камни и риски — то есть, причины получить как можно скорее бесплатную проверку названия и логотипа клиента как потенциального товарного знака.
Не надо писать сразу большие сообщения.
Задавай вопросы, чтобы беседа шла по сценарию, ведущему клиента к пониманию его болей и проблемы.
Не надо писать «Спасибо, что поделились, что у вас уже есть название и логотип», и подобные благодарности за ничего не стоящие человеку действия.
В первых нескольких сообщениях важно задать уточняющие вопросы.
Очень важно получить ответы на следующие вопросы:
— на каких площадках? вывеска будет у вас? на сайте? в соц сетях? маркетплейсы? реклама, радио, видео?
— будете продавать, производить продукцию или оказывать услуги?
Если клиент готов к проверке его товарного знака, надо сказать «напишите свой номер телефона, и бот подключит к диалогу менеджера по проверке названия». Говорить от лица менеджера.
Делай вид, что ты человек. Не представляйся ботом и не упоминай слово «бот».
Первым сообщением должно быть:
«Здравствуйте! Пока я зову менеджера, ответьте на вопрос: есть ли у вас уже название или логотип для вашего бизнеса?».
Следующий ответ на сообщение пользователя должен начинаться с:
«Здравствуйте, меня зовут Алексей Баженов, я руководитель удмуртского филиала компании BeBrand в Ижевске» и попытаться узнать имя.
"""

# ---------------------------------------------------------------------------
# Инициализация БД
# ---------------------------------------------------------------------------
DB_PATH = Path(RENDER_DATA_DIR) / "messages.db"

def init_db(path: Path = DB_PATH) -> sqlite3.Connection:
    if path.exists():
        try:
            conn = sqlite3.connect(path)
            cur = conn.cursor()
            cur.execute("PRAGMA integrity_check;")
            if cur.fetchone()[0] != "ok":
                logger.warning("DB integrity check failed. Recreating DB file…")
                path.unlink()
        except sqlite3.DatabaseError:
            path.unlink()
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            user_id   INTEGER,
            username  TEXT,
            role      TEXT,
            message   TEXT,
            image     BLOB,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    return conn

db_conn = init_db()

# ---------------------------------------------------------------------------
# Отправка почты
# ---------------------------------------------------------------------------
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

def send_email_alert(subject: str, body: str, images: Sequence[bytes] | None = None) -> None:
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO  # type: ignore[arg-type]
    msg.attach(MIMEText(body))
    if images:
        for i, data in enumerate(images):
            part = MIMEBase("application", "octet-stream")
            part.set_payload(data)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename=img{i}.jpg")
            msg.attach(part)
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, 465) as srv:
            srv.login(EMAIL_FROM, SMTP_PASSWORD)  # type: ignore[arg-type]
            srv.send_message(msg)
    except Exception as exc:
        logger.error("Email send failed: %s", exc)

# ---------------------------------------------------------------------------
# Хэндлеры Aiogram
# ---------------------------------------------------------------------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext) -> None:
    history: list[MutableMapping[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "assistant", "content": (
            "Здравствуйте! Пока я зову менеджера, ответьте на вопрос: "
            "есть ли у вас уже название или логотип для вашего бизнеса?"
        )},
    ]
    await state.update_data(chat_history=history)
    await message.answer(history[-1]["content"])

@dp.message()
async def handle(message: types.Message, state: FSMContext) -> None:
    user_text = (message.text or "").strip()
    data = await state.get_data()
    history = data.get("chat_history", []) or [{"role": "system", "content": SYSTEM_PROMPT}]
    history.append({"role": "user", "content": user_text})

    # Сохраняем в БД
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO messages(user_id, username, role, message) VALUES(?,?,?,?)",
        (message.from_user.id, message.from_user.username or "", "user", user_text),
    )
    db_conn.commit()

    # Телефон
    m = PHONE_REGEX.search(user_text)
    if m:
        txt = f"Пользователь оставил тел.: {m.group(1)}"
        await bot.send_message(ALERT_CHAT_ID, txt)
        send_email_alert("Телефон", txt)

    # Команда "ананас"
    if user_text.lower() == "ананас":
        rows = cur.execute(
            "SELECT role, message, timestamp FROM messages WHERE user_id=? ORDER BY timestamp",
            (message.from_user.id,),
        ).fetchall()
        txt = "".join(f"[{ts}] {role}: {msg}\n" for role, msg, ts in rows)
        send_email_alert("Переписка", txt)
        await message.answer("Отправлено менеджеру")
        return

    # ChatGPT (новая версия)
    try:
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=history,
            max_tokens=500,
            temperature=0.9,
        )
        reply = resp.choices[0].message.content
    except Exception as exc:
        logger.exception("OpenAI API error")
        reply = "Ошибка. Попробуйте позже."

    history.append({"role": "assistant", "content": reply})
    await state.update_data(chat_history=history)
    cur.execute(
        "INSERT INTO messages(user_id, username, role, message, image, timestamp) VALUES(?,?,?,?,NULL,datetime('now'))",
        (message.from_user.id, message.from_user.username or "", "assistant", reply),
    )
    db_conn.commit()

    await asyncio.sleep(3)
    await message.answer(reply)

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info("Bot starting…")
    asyncio.run(dp.start_polling(bot))
