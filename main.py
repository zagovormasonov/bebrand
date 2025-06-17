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
from aiogram import executor
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from openai import OpenAI

# Configuration from environment variables
API_TOKEN = os.environ.get('API_TOKEN')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
ALERT_CHAT_ID = os.environ.get('ALERT_CHAT_ID')
EMAIL_FROM = os.environ.get('EMAIL_FROM')
EMAIL_TO = os.environ.get('EMAIL_TO')
YANDEX_APP_PASSWORD = os.environ.get('YANDEX_APP_PASSWORD')

# Проверки обязательных переменных
missing_vars = []
for var_name, var_value in [('API_TOKEN', API_TOKEN), ('OPENAI_API_KEY', OPENAI_API_KEY),
                           ('ALERT_CHAT_ID', ALERT_CHAT_ID), ('EMAIL_FROM', EMAIL_FROM),
                           ('EMAIL_TO', EMAIL_TO), ('YANDEX_APP_PASSWORD', YANDEX_APP_PASSWORD)]:
    if not var_value:
        missing_vars.append(var_name)

if missing_vars:
    raise RuntimeError(f"Missing environment variables: {', '.join(missing_vars)}")

ALERT_CHAT_ID = int(ALERT_CHAT_ID)  # ID должен быть числом

# Logging
logging.basicConfig(level=logging.INFO)

# OpenAI client
client = OpenAI(api_key=OPENAI_API_KEY)

# Telegram bot
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# System prompt (оставляем как есть)
SYSTEM_PROMPT = (
    "Отвечай только на русском языке. "
    "Ответь как можно правдивее, используя предоставленный контекст выше. "
    "Тебе нужно не только ответить на вопросы, но продать клиенту бесплатную проверку его товарного знака. "
    "Для этого можно задавать вопросы по регистрации. "
    "Тебе нужно подсветить всевозможные подводные камни и риски — то есть, причины получить как можно скорее бесплатную проверку названия и логотипа клиента как потенциального товарного знака. "
    "Не надо писать сразу большие сообщения. "
    "Задавай вопросы, чтобы беседа шла по сценарию, ведущему клиента к пониманию его болей и проблемы. "
    "Не надо писать \"Спасибо, что поделились, что у вас уже есть название и логотип\", и подобные благодарности за ничего не стоящие человеку действия, это бред. "
    "В первых нескольких сообщениях важно задать уточняющие вопросы. "
    "Очень важно получить ответы на следующие вопросы: [-на каких площадках? вывеска будет у вас? на сайте? в соц сетях? маркетплейсы? реклама, радио, видео? будете продавать, производить продукцию или оказывать услуги?]. "
    "Если клиент готов к проверке его товарного знака, надо сказать [напишите свой номер телефона, и бот подключит к диалогу менеджера по проверке названия] (в этой фразе нужно говорить от лица менеджера про бота, который подключит). "
    "Делай вид, что ты человек. Не представляйся ботом и не упоминай слово \"бот\". "
    "Первым сообщением должно быть: \"Здравствуйте! Пока я зову менеджера, ответьте на вопрос: есть ли у вас уже название или логотип для вашего бизнеса?\". "
    "Следующий ответ на сообщение пользователя должен начинаться с \"Здравствуйте, меня зовут Алексей Баженов, я руководитель удмуртского филиала компании BeBrand в Ижевске, а вас как зовут?\""
)

PHONE_REGEX = re.compile(r"(\+?\d[\d\s\-]{7,}\d)")

# SQLite и остальной код без изменений
conn = sqlite3.connect("messages.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        user_id INTEGER,
        username TEXT,
        message TEXT,
        image BLOB,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
""")
try:
    cursor.execute("ALTER TABLE messages ADD COLUMN image BLOB")
except sqlite3.OperationalError:
    pass
conn.commit()

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
        with smtplib.SMTP_SSL('smtp.yandex.ru', 465) as server:
            server.login(EMAIL_FROM, YANDEX_APP_PASSWORD)
            server.send_message(msg)
        logging.info("Email sent successfully.")
    except Exception as e:
        logging.error(f"Failed to send email: {e}")

@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    await message.reply("Здравствуйте! Пока я зову менеджера, ответьте на вопрос: есть ли у вас уже название или логотип для вашего бизнеса?")

@dp.message_handler(content_types=types.ContentTypes.ANY)
async def handle_message(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or str(user_id)
    user_text = message.text.strip() if message.text else ""
    image_data = None

    if message.photo:
        photo = message.photo[-1]
        photo_file = await photo.download(destination_dir=None)
        with open(photo_file.name, 'rb') as f:
            image_data = f.read()

    cursor.execute("INSERT INTO messages (user_id, username, message, image) VALUES (?, ?, ?, ?)",
                   (user_id, username, user_text, image_data))
    conn.commit()

    if user_text.lower() == "ананас":
        cursor.execute("SELECT message, timestamp, image FROM messages WHERE user_id = ? ORDER BY timestamp", (user_id,))
        rows = cursor.fetchall()
        history = f"История переписки с @{username} (id {user_id}):\n\n"
        images = []
        for msg, ts, img in rows:
            history += f"[{ts}] {msg or '[изображение]'}\n"
            if img:
                images.append(img)

        send_email_alert(f"Переписка с @{username}", history, images=images)
        await message.reply("Вся переписка с изображениями отправлена менеджеру по почте.")
        return

    match = PHONE_REGEX.search(user_text)
    if match:
        phone = match.group(1)
        alert_text = f"Пользователь @{username} оставил номер телефона: {phone}"
        try:
            await bot.send_message(ALERT_CHAT_ID, alert_text)
        except Exception as e:
            logging.error(f"Failed to send alert to {ALERT_CHAT_ID}: {e}")
        send_email_alert('Новый номер клиента', alert_text)

    conversation = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "assistant", "content": "Здравствуйте, меня зовут Алексей Баженов, я руководитель удмуртского филиала компании BeBrand в Ижевске, а вас как зовут?"},
        {"role": "user", "content": user_text}
    ]
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=conversation,
            max_tokens=500,
            temperature=0.7
        )
        reply = response.choices[0].message.content
    except Exception as e:
        logging.error(f"OpenAI API error: {e}")
        reply = "Извините, произошла ошибка. Попробуйте позже."

    delay = random.uniform(5.0, 5.0)
    logging.info(f"Delaying response by {delay:.2f} seconds")
    await asyncio.sleep(delay)
    await message.reply(reply)

if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True)
