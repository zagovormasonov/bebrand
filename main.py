# -*- coding: utf-8 -*-
"""Telegram‑бот BeBrand: диалог + выгрузка Google Sheets."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import smtplib
import sqlite3
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, MutableMapping, Sequence

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from openai import OpenAI

# Optional Google Sheets libs
try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    gspread = None
    Credentials = None

API_TOKEN = os.getenv("API_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ALERT_CHAT_ID_RAW = os.getenv("ALERT_CHAT_ID")
EMAIL_FROM = os.getenv("EMAIL_FROM")
EMAIL_TO = os.getenv("EMAIL_TO")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", os.getenv("YANDEX_APP_PASSWORD"))
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")
GOOGLE_SA_JSON = os.getenv("GOOGLE_SA_JSON")
RENDER_DATA_DIR = os.getenv("RENDER_DATA_DIR", "/tmp")

_required = [
    ("API_TOKEN", API_TOKEN),
    ("OPENAI_API_KEY", OPENAI_API_KEY),
    ("ALERT_CHAT_ID", ALERT_CHAT_ID_RAW),
    ("EMAIL_FROM", EMAIL_FROM),
    ("EMAIL_TO", EMAIL_TO),
    ("SMTP_PASSWORD", SMTP_PASSWORD),
]
_missing = [n for n, v in _required if not v]
if _missing:
    raise RuntimeError(f"Missing environment variables: {', '.join(_missing)}")

ALERT_CHAT_ID = int(ALERT_CHAT_ID_RAW)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

client = OpenAI(api_key=OPENAI_API_KEY)
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

def init_google_sheet():
    if not (GOOGLE_SHEET_NAME and gspread and Credentials and GOOGLE_SA_JSON):
        logger.info("Google Sheets not configured or libraries missing")
        return None
    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds_info = json.loads(GOOGLE_SA_JSON)
        creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
        gc = gspread.authorize(creds)
        sheet = gc.open(GOOGLE_SHEET_NAME).sheet1
        logger.info("Google Sheet '%s' connected", GOOGLE_SHEET_NAME)
        return sheet
    except Exception as exc:
        logger.error("Google Sheets init failed: %s", exc)
        return None

gsheet = init_google_sheet()

PHONE_REGEX = re.compile(r"(\+?\d[\d\s\-]{7,}\d)")

SYSTEM_PROMPT = """(твой большой системный промт)"""

DB_PATH = Path(RENDER_DATA_DIR) / "messages.db"

def init_db(path: Path = DB_PATH) -> sqlite3.Connection:
    if path.exists():
        try:
            conn = sqlite3.connect(path)
            cur = conn.cursor()
            cur.execute("PRAGMA integrity_check;")
            if cur.fetchone()[0] != "ok":
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

def send_email_alert(subject: str, body: str, images: Sequence[bytes] | None = None) -> None:
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
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
            srv.login(EMAIL_FROM, SMTP_PASSWORD)
            srv.send_message(msg)
    except Exception as exc:
        logger.error("Email send failed: %s", exc)

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext) -> None:
    history: list[MutableMapping[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "assistant",
            "content": (
                "Здравствуйте! Пока я зову менеджера, ответьте на вопрос: "
                "есть ли у вас уже название или логотип для вашего бизнеса?"
            ),
        },
    ]
    await state.update_data(chat_history=history)
    await message.answer(history[-1]["content"])

@dp.message()
async def handle(message: types.Message, state: FSMContext) -> None:
    user_text = (message.text or "").strip()

    if user_text.lower() in {"отправь данные", "отправить данные"}:
        if gsheet is None:
            await message.answer("Google Sheets не настроена.")
        else:
            values = gsheet.get_all_values()
            if not values:
                await message.answer("Таблица пуста.")
            else:
                text_rows = ["\t".join(map(str, row)) for row in values]
                full_text = "\n".join(text_rows)
                for i in range(0, len(full_text), 4000):
                    await message.answer(full_text[i : i + 4000])
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

    m = PHONE_REGEX.search(user_text)
    if m:
        txt = f"Пользователь оставил тел.: {m.group(1)}"
        await bot.send_message(ALERT_CHAT_ID, txt)
        send_email_alert("Телефон", txt)

    if user_text.lower() == "ананас":
        rows = cur.execute(
            "SELECT role, message, timestamp FROM messages WHERE user_id=? ORDER BY timestamp",
            (message.from_user.id,),
        ).fetchall()
        txt = "".join(f"[{ts}] {role}: {msg}\n" for role, msg, ts in rows)
        send_email_alert("Переписка", txt)
        await message.answer("Отправлено менеджеру")
        return

    # Запрос к OpenAI
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=history,
            temperature=0.7,
            max_tokens=1500,
        )
        assistant_message = resp.choices[0].message.content
        history.append({"role": "assistant", "content": assistant_message})

        # Сохраняем ответ ассистента в базу
        cur.execute(
            "INSERT INTO messages(user_id, username, role, message) VALUES(?,?,?,?)",
            (message.from_user.id, message.from_user.username or "", "assistant", assistant_message),
        )
        db_conn.commit()

        # Обновляем состояние с историей
        await state.update_data(chat_history=history)

        # Отправляем ответ пользователю
        await message.answer(assistant_message)

    except Exception as exc:
        logger.error("OpenAI request failed: %s", exc)
        await message.answer("Произошла ошибка при обработке вашего запроса. Попробуйте позже.")

if __name__ == "__main__":
    import asyncio

    from aiogram import executor

    executor.start_polling(dp, skip_updates=True)
