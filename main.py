# requirements.txt:
# aiogram
# gspread
# google-auth
# openai

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

import json
from google.oauth2.service_account import Credentials
import gspread

# Конфигурация из переменных окружения
API_TOKEN = os.environ.get('API_TOKEN')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
ALERT_CHAT_ID = os.environ.get('ALERT_CHAT_ID')
EMAIL_FROM = os.environ.get('EMAIL_FROM')
EMAIL_TO = os.environ.get('EMAIL_TO')
YANDEX_APP_PASSWORD = os.environ.get('YANDEX_APP_PASSWORD')
GOOGLE_SHEET_NAME = os.environ.get('GOOGLE_SHEET_NAME')
RENDER_DATA_DIR = os.environ.get('RENDER_DATA_DIR', '/tmp')

# Проверка обязательных переменных (без Google)
required = [('API_TOKEN', API_TOKEN),('OPENAI_API_KEY', OPENAI_API_KEY),
            ('ALERT_CHAT_ID', ALERT_CHAT_ID),('EMAIL_FROM', EMAIL_FROM),
            ('EMAIL_TO', EMAIL_TO),('YANDEX_APP_PASSWORD', YANDEX_APP_PASSWORD)]
missing = [name for name,val in required if not val]
if missing:
    raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")

ALERT_CHAT_ID = int(ALERT_CHAT_ID)

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Инициализация OpenAI и бота
openai.api_key = OPENAI_API_KEY
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Прямое встраивание creds.json в код (НЕ БЕЗОПАСНО для публичных реп)
creds_json = {
    "type": "service_account",
    "project_id": "your_project_id",
    "private_key_id": "your_private_key_id",
    "private_key": "-----BEGIN PRIVATE KEY-----\nYOUR_PRIVATE_KEY\n-----END PRIVATE KEY-----\n",
    "client_email": "your_service_account_email@project.iam.gserviceaccount.com",
    "client_id": "your_client_id",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
    "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/your_service_account_email%40project.iam.gserviceaccount.com"
}

# Инициализация Google Sheets (если настроено)
sheet = None
if GOOGLE_SHEET_NAME:
    try:
        scopes = ['https://www.googleapis.com/auth/spreadsheets','https://www.googleapis.com/auth/drive']
        creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
        gc = gspread.authorize(creds)
        sheet = gc.open(GOOGLE_SHEET_NAME).sheet1
    except Exception as e:
        logging.error(f"Google Sheets init failed: {e}")

# Регулярка для телефонов
PHONE_REGEX = re.compile(r"(\+?\d[\d\s\-]{7,}\d)")

# Системный промт
SYSTEM_PROMPT = '''
... ваш системный промт ...
'''

# Путь к БД
DB_PATH = os.path.join(RENDER_DATA_DIR, 'messages.db')

def init_db(path=DB_PATH):
    if os.path.exists(path):
        try:
            conn = sqlite3.connect(path)
            cur = conn.cursor()
            cur.execute("PRAGMA integrity_check;")
            if cur.fetchone()[0] != 'ok': os.remove(path)
        except sqlite3.DatabaseError:
            os.remove(path)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages(
            user_id INTEGER, username TEXT, role TEXT,
            message TEXT, image BLOB,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn

db_conn = init_db()

def send_email_alert(subject, body, images=None):
    msg = MIMEMultipart()
    msg['Subject']=subject; msg['From']=EMAIL_FROM; msg['To']=EMAIL_TO
    msg.attach(MIMEText(body))
    if images:
        for i,data in enumerate(images):
            part=MIMEBase('application','octet-stream')
            part.set_payload(data); encoders.encode_base64(part)
            part.add_header('Content-Disposition',f'attachment; filename="img{i}.jpg"')
            msg.attach(part)
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com',465) as srv:
            srv.login(EMAIL_FROM, YANDEX_APP_PASSWORD)
            srv.send_message(msg)
    except Exception as e:
        logging.error(f"Email send failed: {e}")

@dp.message(Command('start'))
async def cmd_start(message: types.Message, state: FSMContext):
    history=[{'role':'system','content':SYSTEM_PROMPT},
             {'role':'assistant','content':(
               'Здравствуйте! Пока я зову менеджера, ответьте на вопрос: '
               'есть ли у вас уже название или логотип для вашего бизнеса?'
             )}]
    await state.update_data(chat_history=history)
    await message.answer(history[-1]['content'])

@dp.message()
async def handle(message: types.Message, state: FSMContext):
    user_text=message.text.strip() if message.text else ''
    # "отправь данные"
    if user_text.lower()=='отправь данные':
        if sheet:
            recs=sheet.get_all_records()
            if not recs: return await message.answer('В таблице нет данных.')
            lines=[', '.join(f'{k}:{v}' for k,v in r.items()) for r in recs[:5]]
            return await message.answer('Первые записи:\n'+"\n".join(lines))
        else:
            return await message.answer('Google Sheets не настроена.')

    data=await state.get_data(); history=data.get('chat_history')
    if not history:
        history=[{'role':'system','content':SYSTEM_PROMPT}]
    history.append({'role':'user','content':user_text})

    # сохраняем в БД
    cur=db_conn.cursor()
    cur.execute("INSERT INTO messages(user_id,username,role,message) VALUES(?,?,?,?)",
                (message.from_user.id,message.from_user.username or '',
                 'user',user_text))
    db_conn.commit()

    # телефон
    m=PHONE_REGEX.search(user_text)
    if m:
        txt=f"Пользователь оставил тел.: {m.group(1)}"
        await bot.send_message(ALERT_CHAT_ID,txt); send_email_alert('телефон',txt)

    # переписка "ананас"
    if user_text.lower()=='ананас':
        rows=cur.execute("SELECT role,message,timestamp FROM messages WHERE user_id=? ORDER BY timestamp",
                         (message.from_user.id,)).fetchall()
        txt=''.join(f"[{ts}]({r}){m}\n" for r,m,ts in rows)
        send_email_alert('переписка',txt)
        return await message.answer('Отправлено менеджеру')

    # ChatGPT
    try:
        resp=openai.ChatCompletion.create(
            model='gpt-3.5-turbo', messages=history, max_tokens=500, temperature=0.9)
        reply=resp.choices[0].message.content
    except Exception:
        reply='Ошибка. Попробуйте позже.'

    history.append({'role':'assistant','content':reply})
    await state.update_data(chat_history=history)
    cur.execute("INSERT INTO messages VALUES(?,?,?,?,NULL,datetime('now'))",
                (message.from_user.id,message.from_user.username or '',
                 'assistant',reply))
    db_conn.commit()

    await asyncio.sleep(5)
    await message.answer(reply)

if __name__=='__main__':
    asyncio.run(dp.start_polling(bot))