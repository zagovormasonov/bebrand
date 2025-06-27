# requirements:
# pip install aiogram gspread google-auth openai

import os
import asyncio
import logging
import sqlite3
import random
import re
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
import openai

# Для работы с Google Sheets
from google.oauth2.service_account import Credentials
import gspread

# Конфигурация из переменных окружения
API_TOKEN = os.environ.get('API_TOKEN')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
ALERT_CHAT_ID = os.environ.get('ALERT_CHAT_ID')
EMAIL_FROM = os.environ.get('EMAIL_FROM')
EMAIL_TO = os.environ.get('EMAIL_TO')
YANDEX_APP_PASSWORD = os.environ.get('YANDEX_APP_PASSWORD')
RENDER_DATA_DIR = os.environ.get('RENDER_DATA_DIR', '/tmp')

# Переменные для Google Sheets
GOOGLE_CREDS_JSON = os.environ.get('creds.json')  # путь к service_account.json
GOOGLE_SHEET_NAME = os.environ.get('GOOGLE_SHEET_NAME')  # имя таблицы

# Проверка обязательных переменных
missing_vars = []
for var_name, var_value in [
    ('API_TOKEN', API_TOKEN),
    ('OPENAI_API_KEY', OPENAI_API_KEY),
    ('ALERT_CHAT_ID', ALERT_CHAT_ID),
    ('EMAIL_FROM', EMAIL_FROM),
    ('EMAIL_TO', EMAIL_TO),
    ('YANDEX_APP_PASSWORD', YANDEX_APP_PASSWORD),
    ('GOOGLE_CREDS_JSON', GOOGLE_CREDS_JSON),
    ('GOOGLE_SHEET_NAME', GOOGLE_SHEET_NAME),
]:
    if not var_value:
        missing_vars.append(var_name)

if missing_vars:
    raise RuntimeError(f"Missing environment variables: {', '.join(missing_vars)}")

ALERT_CHAT_ID = int(ALERT_CHAT_ID)

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Инициализация OpenAI и бота
openai.api_key = OPENAI_API_KEY
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Настройка подключения к Google Sheets
SCOPES = ['https://www.googleapis.com/auth/spreadsheets',
          'https://www.googleapis.com/auth/drive']
creds = Credentials.from_service_account_file(GOOGLE_CREDS_JSON, scopes=SCOPES)
gc = gspread.authorize(creds)
sheet = gc.open(GOOGLE_SHEET_NAME).sheet1

# Регулярка для телефонов
PHONE_REGEX = re.compile(r"(\+?\d[\d\s\-]{7,}\d)")

# Системный промт (ваш полный текст сюда)
SYSTEM_PROMPT = '''
... ваш системный промт ...
'''

# Путь к файлу базы
DB_PATH = os.path.join(RENDER_DATA_DIR, "messages.db")

def init_db(path=DB_PATH):
    if os.path.exists(path):
        try:
            conn = sqlite3.connect(path)
            cur = conn.cursor()
            cur.execute("PRAGMA integrity_check;")
            result = cur.fetchone()
            conn.close()
            if result and result[0] != 'ok':
                os.remove(path)
                logging.warning("Corrupted DB detected and removed.")
        except sqlite3.DatabaseError:
            os.remove(path)
            logging.warning("DB removal on exception.")
    conn = sqlite3.connect(path, check_same_thread=False)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            user_id   INTEGER,
            username  TEXT,
            role      TEXT,
            message   TEXT,
            image     BLOB,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn

# Инициализация БД
db_conn = init_db()

# Функция отправки почты
def send_email_alert(subject: str, body: str, images=None):
    msg = MIMEMultipart()
    msg['Subject'] = subject
    msg['From'] = EMAIL_FROM
    msg['To'] = EMAIL_TO
    msg.attach(MIMEText(body, 'plain'))
    if images:
        for i, image_data in enumerate(images):
            part = MIMEBase('application', 'octet-stream')
            part.set_payload(image_data)
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', f'attachment; filename="image_{i+1}.jpg"')
            msg.attach(part)
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(EMAIL_FROM, YANDEX_APP_PASSWORD)
            server.send_message(msg)
        logging.info("Email sent successfully.")
    except Exception as e:
        logging.error(f"Failed to send email: {e}")

@dp.message(Command(commands=["start"]))
async def cmd_start_handler(message: types.Message, state: FSMContext):
    history = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "assistant", "content": (
            "Здравствуйте! Пока я зову менеджера, ответьте на вопрос: "
            "есть ли у вас уже название или логотип для вашего бизнеса?"
        )}
    ]
    await state.update_data(chat_history=history)
    await message.answer(history[-1]['content'])

@dp.message()
async def handle_message(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username or str(user_id)
    user_text = message.text.strip() if message.text else ""

    # Специальная команда: отправить данные из Google Таблицы
    if user_text.lower() == 'отправь данные':
        records = sheet.get_all_records()
        if not records:
            await message.answer("В таблице нет данных.")
        else:
            lines = [", ".join(f"{k}: {v}" for k, v in row.items())
                     for row in records[:5]]
            reply = "Вот первые записи из таблицы:\n" + "\n".join(lines)
            await message.answer(reply)
        return

    data = await state.get_data()
    history = data.get('chat_history') or [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "assistant", "content": (
            "Здравствуйте! Пока я зову менеджера, ответьте на вопрос: "
            "есть ли у вас уже название или логотип для вашего бизнеса?"
        )}
    ]
    history.append({"role": "user", "content": user_text})

    db_conn = init_db()
    cursor = db_conn.cursor()
    cursor.execute(
        "INSERT INTO messages (user_id, username, role, message) VALUES (?, ?, ?, ?)",
        (user_id, username, 'user', user_text)
    )
    db_conn.commit()

    if user_text.lower() == "ананас":
        cursor.execute(
            "SELECT role, message, timestamp FROM messages WHERE user_id = ? ORDER BY timestamp",
            (user_id,)
        )
        rows = cursor.fetchall()
        history_text = f"Переписка с @{username} (id {user_id}):\n\n"
        history_text += "\n".join(f"[{ts}] ({role}) {msg}" for role, msg, ts in rows)
        send_email_alert(f"Переписка с @{username}", history_text)
        await message.answer("Вся переписка отправлена менеджеру по почте.")
        return

    match = PHONE_REGEX.search(user_text)
    if match:
        phone = match.group(1)
        alert_text = f"Пользователь @{username} оставил номер телефона: {phone}"
        await bot.send_message(ALERT_CHAT_ID, alert_text)
        send_email_alert('Новый номер клиента', alert_text)

    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=history,
            max_tokens=500,
            temperature=0.9
        )
        reply = response.choices[0].message.content
    except Exception as e:
        logging.error(f"OpenAI API error: {e}")
        reply = "Извините, произошла ошибка. Попробуйте позже."

    history.append({"role": "assistant", "content": reply})
    cursor.execute(
        "INSERT INTO messages (user_id, username, role, message) VALUES (?, ?, ?, ?)",
        (user_id, username, 'assistant', reply)
    )
    db_conn.commit()
    await state.update_data(chat_history=history)

    await asyncio.sleep(5)
    await message.answer(reply)

async def main():
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())