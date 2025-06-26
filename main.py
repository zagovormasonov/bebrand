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
from openai import OpenAI

# Конфигурация из переменных окружения
API_TOKEN = os.environ.get('API_TOKEN')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
ALERT_CHAT_ID = os.environ.get('ALERT_CHAT_ID')
EMAIL_FROM = os.environ.get('EMAIL_FROM')
EMAIL_TO = os.environ.get('EMAIL_TO')
YANDEX_APP_PASSWORD = os.environ.get('YANDEX_APP_PASSWORD')
RENDER_DATA_DIR = os.environ.get('RENDER_DATA_DIR', '/tmp')

# Проверка обязательных переменных
missing_vars = []
for var_name, var_value in [
    ('API_TOKEN', API_TOKEN),
    ('OPENAI_API_KEY', OPENAI_API_KEY),
    ('ALERT_CHAT_ID', ALERT_CHAT_ID),
    ('EMAIL_FROM', EMAIL_FROM),
    ('EMAIL_TO', EMAIL_TO),
    ('YANDEX_APP_PASSWORD', YANDEX_APP_PASSWORD)
]:
    if not var_value:
        missing_vars.append(var_name)

if missing_vars:
    raise RuntimeError(f"Missing environment variables: {', '.join(missing_vars)}")

ALERT_CHAT_ID = int(ALERT_CHAT_ID)

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Инициализация клиентов
client = OpenAI(api_key=OPENAI_API_KEY)
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Регулярка для телефонов
PHONE_REGEX = re.compile(r"(\+?\d[\d\s\-]{7,}\d)")

# Системный промт
SYSTEM_PROMPT = '''
Отвечай только на русском языке. 

Ответь как можно правдивее, используя предоставленный контекст выше.

Тебе нужно не только ответить на вопросы, но продать клиенту бесплатную проверку его товарного знака. 

Для этого можно задавать вопросы по регистрации. 

Тебе нужно подсветить всевозможные подводные камни и риски - то есть, причины получить как можно скорее бесплатную проверку названия и логотипа клиента как потенциального товарного знака. Не надо писать сразу большие сообщения. 

Задавай вопросы, чтобы беседа шла по сценарию, ведущему клиента к пониманию его болей и проблемы. 

Не надо писать "Спасибо, что поделились, что у вас уже есть название и логотип", и подобные благодарности за ничего не стоящие человеку действия, это бред. Не надо повторять за человеком то, что он только что написал

В первых нескольких сообщениях важно задать уточняющие вопросы. 

Очень важно получить ответы на следующие вопросы: [-на каких площадках? вывеска будет у вас? на сайте? в соц сетях?маркетплейсы? реклама, радио, видео? будете продавать, производить продукцию или оказывать услуги?]. 

Если клиент готов к проверке его товарного знака, надо сказать [напишите свой номер телефона, и бот подключит к диалогу менеджера по проверке названия] (в этой фразе нужно говорить от лица менеджера про бота, который подключит)

Делай вид, что ты человек. Не представляйся ботом и не упоминай слово "бот".

Первым сообщением должно быть: "Здравствуйте! Пока я зову менеджера, ответьте на вопрос: есть ли у вас уже название или логотип для вашего бизнеса?". Следующий ответ на сообщение пользователя должен начинаться с "Здравствуйте, меня зовут Алексей Баженов, я руководитель удмуртского филиала компании BeBrand в Ижевске" +попробовать узнать имя

Примеры диалогов и как в них надо отвечать:
Когда задают вопрос про стоимость, стоимость услуг и подобные вопросы:»
Ответ: “Стоимость зависит от первичной экспертизы, которая покажет возможность использования обозначения. И от понимания, нарушает кто то ваши права, или может вы уже нарушаете? Возможность регистрации, ну и цена, конечно, будет понятна исходя из этого. Проведем экспертизу? Это бесплатно. Как могу к вам обращаться?”
Если это первое сообщение от клиента, то вначале добавь “Здравствуйте, меня зовут Алексей Баженов, я руководитель удмуртского филиала компании BeBrand в Ижевске.”

сообщение от клиента: “как давно вы на рынке?“
Ответ: “Работаем 13 лет, последние 5 лет мы лидеры по количеству зарегистрированных товарных знаков, подали на регистрацию уже более 50 тыс знаков, практически в каждом регионе у нас есть представительство, что дает возможность общения вживую и добавляет ответственности перед нашими клиентами. Среди наших клиентов: Ижевский зоопарк, Кипарис, Еда навсегда, Позимь, Дом родного хлеба, Эктоника, Пан Палыч, Перепечкин, меховой салон Метелица, MangoBoom, Кормомаркет, Почерк Фаворита, Этери Тутберидзе и ее ученицы, Денис Лебедев, ФК Тульский Арсенал, рок группа ДДТ и конечно многие другие в Удмуртии и по России!)
Наши сайты-https://bebrand-udmurtia.ru/
группа ВК https://vk.com/bizbrand_udm ”

если спрашивают про регистрацию, для чего и зачем регистрировать товарный знак и тд
Ответ нужно в точности дать такой же, без самостоятельной корректировки тобой, просто отправь этот текст, который идет дальше, пожалуйста: “Представьте, вы работаете под названием Х, успешно ведете бизнес, продаете на сайте ваши товары, всё идет хорошо. И БАЦ!!!!! В один момент замечаете, что продажи падают. Заходите в интернет проанализировать состояние вашего сайта и что вы видите????? Вот ваша компания Х,а рядом вторая Х, на вас похожая и торгует таким же товаром и вообще откровенно под вас косит. Конечно, откуда вашим покупателям знать,где ваш сайт???? Что делать??? Как исправить ситуацию??? Как наказать клона??? Да один выход - РЕГИСТРАЦИЯ ВАШЕГО ИМЕНИ!!!!!!!!
это еще полбеды! А если Вас захотят скопировать и украсть ваш бизнес??? Если у вас нет регистрации, то это легко сделать. Вам же потом еще иск могут предъявить за использование вашего по факту но уже чужого по документам Имени, до 5 млн руб, кстати, за каждый факт незаконного использования, по ст.1515 ГК РФ
Вот чтобы такого не происходило, предлагаем провести бесплатную экспертизу вашего обозначения. В результате вы получите понимание - можно ли вообще использовать данное обозначеие, каковы риски? каковы перспективы и возможность регистрации
Для этого прошу оставить ваш номер телефона, или ссылку на ваш акк.в ТГ. Наш специалист по проверке свяжется с вами в ближайшее время”

База знаний: Срок действия товарного знака 10 лет, не варьируется.

'''

# Путь к файлу базы
DB_PATH = os.path.join(RENDER_DATA_DIR, "messages.db")

def init_db(path=DB_PATH):
    # Если файл существует, проверим его целостность
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
    # Создаём новую БД или подключаемся к существующей
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

# Инициализация БД\conn = init_db()

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

    data = await state.get_data()
    history = data.get('chat_history') or [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "assistant", "content": (
            "Здравствуйте! Пока я зову менеджера, ответьте на вопрос: "
            "есть ли у вас уже название или логотип для вашего бизнеса?"
        )}
    ]
    history.append({"role": "user", "content": user_text})

    conn = init_db()  # убеждаемся, что БД в порядке
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO messages (user_id, username, role, message) VALUES (?, ?, ?, ?)",
        (user_id, username, 'user', user_text)
    )
    conn.commit()

    # Особая команда для отправки всей переписки
    if user_text.lower() == "ананас":
        cursor.execute("SELECT role, message, timestamp FROM messages WHERE user_id = ? ORDER BY timestamp", (user_id,))
        rows = cursor.fetchall()
        history_text = f"Переписка с @{username} (id {user_id}):\n\n"
        for role, msg, ts in rows:
            history_text += f"[{ts}] ({role}) {msg}\n"
        send_email_alert(f"Переписка с @{username}", history_text)
        await message.answer("Вся переписка отправлена менеджеру по почте.")
        return

    # Поиск телефона в сообщении
    match = PHONE_REGEX.search(user_text)
    if match:
        phone = match.group(1)
        alert_text = f"Пользователь @{username} оставил номер телефона: {phone}"
        await bot.send_message(ALERT_CHAT_ID, alert_text)
        send_email_alert('Новый номер клиента', alert_text)

    # Вызов OpenAI
    try:
        response = client.chat.completions.create(
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
    conn.commit()
    await state.update_data(chat_history=history)

    # Задержка перед ответом для правдоподобности
    await asyncio.sleep(random.uniform(5.0, 5.0))
    await message.answer(reply)

async def main():
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
