# -*- coding: utf-8 -*-
"""Telegram-бот для первичного общения с клиентами BeBrand.

Исправлено:
1. **Ключ сервис-аккаунта** больше не хранится в коде. Ожидается в переменной окружения
   `GOOGLE_SA_JSON` (прямой JSON) либо Base64-кодированный JSON в `GOOGLE_SA_JSON_B64`.
2. SMTP-хост теперь берётся из переменной `SMTP_HOST` (по-умолчанию smtp.gmail.com).
3. Добавлены аннотации типов и реорганизация кода.
4. Работа с Google Sheets вынесена в функцию `init_google_sheet()`.
5. Для локальной БД предусмотрён путь из `RENDER_DATA_DIR` (volume на Render.com).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import sqlite3
import smtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, List, Mapping, MutableMapping, Sequence

import openai
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

# Опциональные зависимости для Google Sheets
try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    gspread = None  # type: ignore
    Credentials = None  # type: ignore

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
GOOGLE_SHEET_NAME: str | None = os.getenv("GOOGLE_SHEET_NAME")
GOOGLE_SA_JSON: str | None = os.getenv("GOOGLE_SA_JSON")
GOOGLE_SA_JSON_B64: str | None = os.getenv("GOOGLE_SA_JSON_B64")
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

ALERT_CHAT_ID: int = int(ALERT_CHAT_ID_RAW)  # type: ignore

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OpenAI & Telegram
# ---------------------------------------------------------------------------
openai.api_key = OPENAI_API_KEY
bot = Bot(token=API_TOKEN)  # type: ignore
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------
def init_google_sheet() -> "gspread.models.Worksheet | None":
    """Инициализирует и возвращает Worksheet или None, если не настроено."""
    if not (GOOGLE_SHEET_NAME and gspread and Credentials and (GOOGLE_SA_JSON or GOOGLE_SA_JSON_B64)):
        logger.warning("Google Sheets is not configured or requirements missing.")
        return None
    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds_info = None
        if GOOGLE_SA_JSON_B64:
            try:
                decoded = base64.b64decode(GOOGLE_SA_JSON_B64)
                creds_info = json.loads(decoded)
            except Exception:
                pass
        if creds_info is None and GOOGLE_SA_JSON:
            creds_info = json.loads(GOOGLE_SA_JSON)
        creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
        gc = gspread.authorize(creds)
        return gc.open(GOOGLE_SHEET_NAME).sheet1  # type: ignore
    except Exception as exc:
        logger.error("Google Sheets init failed: %s", exc)
        return None

gsheet = init_google_sheet()

# ---------------------------------------------------------------------------
# Регулярка для телефона
# ---------------------------------------------------------------------------
PHONE_REGEX = re.compile(r"(\+?\d[\d\s\-]{7,}\d)")

# ---------------------------------------------------------------------------
# Системный промпт
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """
Отвечай только на русском языке.
... (текст опущен для краткости) ...
"""

# ---------------------------------------------------------------------------
# База данных
# ---------------------------------------------------------------------------
DB_PATH = Path(RENDER_DATA_DIR) / "messages.db"

def init_db(path: Path = DB_PATH) -> sqlite3.Connection:
    if path.exists():
        try:
            conn = sqlite3.connect(path)
            if conn.execute("PRAGMA integrity_check;").fetchone()[0] != "ok":
                logger.warning("DB integrity check failed. Recreating…")
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
def send_email_alert(subject: str, body: str, images: Sequence[bytes] | None = None) -> None:
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO  # type: ignore
    msg.attach(MIMEText(body))
    if images:
        for idx, data in enumerate(images):
            part = MIMEBase("application", "octet-stream")
            part.set_payload(data)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename=img{idx}.jpg")
            msg.attach(part)
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, 465) as srv:
            srv.login(EMAIL_FROM, SMTP_PASSWORD)  # type: ignore
            srv.send_message(msg)
    except Exception as exc:
        logger.error("Email send failed: %s", exc)

# ---------------------------------------------------------------------------
# Хэндлеры
# ---------------------------------------------------------------------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext) -> None:
    history = [
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

    if user_text.lower() == "отправь данные":
        if gsheet:
            recs = gsheet.get_all_records()
            if not recs:
                await message.answer("В таблице нет данных.")
                return
            lines = [", ".join(f"{k}:{v}" for k, v in r.items()) for r in recs[:5]]
            await message.answer("Первые записи:\n" + "\n".join(lines))
        else:
            await message.answer("Google Sheets не настроена.")
        return

    data = await state.get_data()
    history = data.get("chat_history", []) or [{"role": "system", "content": SYSTEM_PROMPT}]
    history.append({"role": "user", "content": user_text})

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

    # Служебная команда
    if user_text.lower() == "ананас":
        rows = cur.execute(
            "SELECT role, message, timestamp FROM messages WHERE user_id=? ORDER BY timestamp",
            (message.from_user.id,),
        ).fetchall()
        txt = "".join(f"[{ts}] {role}: {msg}\n" for role, msg, ts in rows)
        send_email_alert("Переписка", txt)
        await message.answer("Отправлено менеджеру")
        return

    # ChatGPT
    try:
        resp = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=history,
            max_tokens=500,
            temperature=0.9,
        )
        reply = resp.choices[0].message.content  # type: ignore
    except Exception:
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

if __name__ == "__main__":
    logger.info("Bot starting…")
    asyncio.run(dp.start_polling(bot))
