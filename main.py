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
    "project_id": "dbtplus",
    "private_key_id": "b310d3618d0c95a507c54727b99b07fec3ca0478",
    "private_key": "-----BEGIN PRIVATE KEY-----\nMIIEvAIBADANBgkqhkiG9w0BAQEFAASCBKYwggSiAgEAAoIBAQCzPt5Xm2jrfrSP\nZovP7zsWQThTedCmWCyE6vAc5q99XR8wUZO86pmd1KiOGQeQlpeiaWSBi4ZBY6Bc\nQPnqyK0WGAb0TJ6vCd1eNq4EEv1e5opTZmrFmZev4x4c4LP083Fqk8RijC1YHMp7\nxuJ/YVBcWM4oE50WxRRyxU5bDPHDZXIn45ob7M+hXzp2I6jNQjnhSrB+nywJxSBu\ngtr+NEW/zqA3SBJ3hgaBmY7s8J0OB5+xPlAetV4Bu3qOawYWiUbXxYUe5yORGtqI\nZ1FetsTejCNeUUu5eEJBEQ3n1o2C5RcP052w+zPWDdJrt1QikhJoKYzZanjtaoGx\nil2VcgX/AgMBAAECggEABP80rZaMhzwiBnliXmqZ6BXrXxBfS7Pbkd0G0pdvvvvw\nmaU8jCCyJZ8/D68M8e/wzDtJ7P6ZwIrpdojtLqlngd0rnHXiWpjYzf6SPVTWMWYV\n5xtM0LNmciXPuhhdi++ZctIpwGGOBg3Pa0HxIIHy/pAPNzjMwUy/NC/h7lHfD4Zh\nhZW/SIEG14Y9hdfd+i2Zy6tuKbjcLFwlQEM/4YP3ETvUEViasX3BmI8ZqMUOgKUF\ny/mppeItW+6OgMPD9oOTtPhMez9w2Vy8+vKG2/fMJ1lkhjQVZmJBGTt9CL9TUA0F\nEw7ZGfojvrOBCEzdIAZ5rfNoFMJeso1vSQLDUjepoQKBgQDYmRgt+pUq/yI7ELT8\nDJ1oWS+6f89eu71bYqyacHuJLKKcIY8rXoQMqfy9pbyHhJKUP6QVdP2cPl4dh/35\nfOKXAxuw5EAXSBWraU1u7aFe6OUt/k7+++rbvbu0iTgp0naeQMZvur873fS/7cS5\nFsh3v4bLPiF+48iU7EQ4VGO4jwKBgQDT2kgl5QwR2bblUnYgWHQPbU+HjyHtXI7m\nelJg9E+0a4hQJVJUPPSm/KWdT8plKs1mGhpaHQ+38H7KjeKCH0AWdlyPNpmVNeGv\nhA6P12tqSGMEkIusYI+TJKt8Xd4iwr2fVLy6YxLzya7l9gugqpmVjgA2Tbw7WDi3\nmkAS5q4zkQKBgDIwd2vgDsShzfrFykpFWgwd7nNWvmSDOEN+v+QhgF6u2xc2p4gz\nJIISuZ/wUZlNXPHBNXJLY6DaytAo/O7cw1yeucHpgfhjGbJYejrkEWp+qOxZa1Cm\naytz8ZTJ3xvByv6sn86wBTQIIHiAzf7diqJE3SUnRneyrH3lqYEr/Nd9AoGAFGGe\nqU6k357DcsKBLNF1sPpCOXdyuyQ5d0DzZfJ7LI9f2N4OUp5epyYNRNolTaBVjGoc\neOjs1zRi7lfCH+SjxMV0WC7XjbxWTw10XTBLXDlElW7WkSnlBjHz8Y4STePQXGDJ\nm2DmtN+FXQhTzAw9pF659H98CXWOV1OWsHrS7ZECgYAvOcERwJbVGqFtadzw35WK\nhZUy5BSgVwQHz+9346aVC6D/4kfJPHFkLx31o4MbVv0fMekZqNvMB+OqZDJN5eEd\nHxLrVYZj/ydzF5TZN3VkQHdo9dpk5wEMihNMZa6z4gnz3DOaIlFqbzdAzt1bi+ge\nFU1az75M4iNIcY4eVcf1nw==\n-----END PRIVATE KEY-----\n",
    "client_email": "google-sheets@dbtplus.iam.gserviceaccount.com",
    "client_id": "106118640893119954817",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
    "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/google-sheets%40dbtplus.iam.gserviceaccount.com",
    "universe_domain": "googleapis.com"
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
Ответ нужно дать следующий, в точности как тут написано, без изменений, это важно: “Представьте, вы работаете под названием Х, успешно ведете бизнес, продаете на сайте ваши товары, всё идет хорошо. И БАЦ!!!!! В один момент замечаете, что продажи падают. Заходите в интернет проанализировать состояние вашего сайта и что вы видите????? Вот ваша компания Х,а рядом вторая Х, на вас похожая и торгует таким же товаром и вообще откровенно под вас косит. Конечно, откуда вашим покупателям знать,где ваш сайт???? Что делать??? Как исправить ситуацию??? Как наказать клона??? Да один выход - РЕГИСТРАЦИЯ ВАШЕГО ИМЕНИ!!!!!!!!
это еще полбеды! А если Вас захотят скопировать и украсть ваш бизнес??? Если у вас нет регистрации, то это легко сделать. Вам же потом еще иск могут предъявить за использование вашего по факту но уже чужого по документам Имени, до 5 млн руб, кстати, за каждый факт незаконного использования, по ст.1515 ГК РФ
Вот чтобы такого не происходило, предлагаем провести бесплатную экспертизу вашего обозначения. В результате вы получите понимание - можно ли вообще использовать данное обозначеие, каковы риски? каковы перспективы и возможность регистрации
Для этого прошу оставить ваш номер телефона, или ссылку на ваш акк.в ТГ. Наш специалист по проверке свяжется с вами в ближайшее время”

База знаний: Срок действия товарного знака 10 лет, не варьируется.


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