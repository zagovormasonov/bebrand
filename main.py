# -*- coding: utf-8 -*-
"""Telegram‑бот BeBrand: диалог + выгрузка Google Sheets + follow-up reminders."""

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
from typing import Sequence

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from openai import OpenAI

# ---------------------------------------------------------------------------
# Опциональные зависимости (Google)
# ---------------------------------------------------------------------------
try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    gspread = None  # type: ignore
    Credentials = None  # type: ignore

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------
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
_missing = [name for name, val in _required if not val]
if _missing:
    raise RuntimeError(f"Missing environment variables: {', '.join(_missing)}")

ALERT_CHAT_ID = int(ALERT_CHAT_ID_RAW)

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Клиенты
# ---------------------------------------------------------------------------
client = OpenAI(api_key=OPENAI_API_KEY)
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------
def init_google_sheet() -> gspread.models.Spreadsheet | None:
    if not (GOOGLE_SHEET_NAME and gspread and Credentials and GOOGLE_SA_JSON):
        logger.info("Google Sheets not configured or libraries missing")
        return None
    try:
        creds_info = json.loads(GOOGLE_SA_JSON)
        creds = Credentials.from_service_account_info(
            creds_info,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        gc = gspread.authorize(creds)
        return gc.open(GOOGLE_SHEET_NAME).sheet1
    except Exception as e:
        logger.error("Google Sheets init failed: %s", e)
        return None

gsheet = init_google_sheet()

# ---------------------------------------------------------------------------
# Регулярное выражение для телефона
# ---------------------------------------------------------------------------
PHONE_REGEX = re.compile(r"(\+?\d[\d\s\-]{7,}\d)")

# ---------------------------------------------------------------------------
# Системный промпт для ChatGPT
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    '''
    Отвечай только на русском языке. 

Говори о себе только в мужском роде.

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
Когда задают вопрос про стоимость, стоимость услуг и подобные вопросы:
Ответ: “Стоимость зависит от первичной экспертизы, которая покажет возможность использования обозначения. И от понимания, нарушает кто то ваши права, или может вы уже нарушаете? Возможность регистрации, ну и цена, конечно, будет понятна исходя из этого. Проведем экспертизу? Это бесплатно. Как могу к вам обращаться?”
Если это первое сообщение от клиента, то вначале добавь “Здравствуйте, меня зовут Алексей Баженов, я руководитель удмуртского филиала компании BeBrand в Ижевске.”

сообщение от клиента: “как давно вы на рынке?“
Ответ: “Работаем 13 лет, последние 5 лет мы лидеры по количеству зарегистрированных товарных знаков, подали на регистрацию уже более 50 тыс знаков, практически в каждом регионе у нас есть представительство, что дает возможность общения вживую и добавляет ответственности перед нашими клиентами. Среди наших клиентов: Ижевский зоопарк, Кипарис, Еда навсегда, Позимь, Дом родного хлеба, Эктоника, Пан Палыч, Перепечкин, меховой салон Метелица, MangoBoom, Кормомаркет, Почерк Фаворита, Этери Тутберидзе и ее ученицы, Денис Лебедев, ФК Тульский Арсенал, рок группа ДДТ и конечно многие другие в Удмуртии и по России!)
Наши сайты-https://bebrand-udmurtia.ru/
группа ВК https://vk.com/bizbrand_udm ”

если спрашивают про регистрацию, для чего и зачем регистрировать товарный знак и тд
Ответ: “Представьте, вы работаете под названием Х, успешно ведете бизнес, продаете на сайте ваши товары, всё идет хорошо. И БАЦ!!!!! В один момент замечаете, что продажи падают. Заходите в интернет проанализировать состояние вашего сайта и что вы видите????? Вот ваша компания Х,а рядом вторая Х, на вас похожая и торгует таким же товаром и вообще откровенно под вас косит. Конечно, откуда вашим покупателям знать,где ваш сайт???? Что делать??? Как исправить ситуацию??? Как наказать клона??? Да один выход - РЕГИСТРАЦИЯ ВАШЕГО ИМЕНИ!!!!!!!!
это еще полбеды! А если Вас захотят скопировать и украсть ваш бизнес??? Если у вас нет регистрации, то это легко сделать. Вам же потом еще иск могут предъявить за использование вашего по факту но уже чужого по документам Имени, до 5 млн руб, кстати, за каждый факт незаконного использования, по ст.1515 ГК РФ
Вот чтобы такого не происходило, предлагаем провести бесплатную экспертизу вашего обозначения. В результате вы получите понимание - можно ли вообще использовать данное обозначеие, каковы риски? каковы перспективы и возможность регистрации
Для этого прошу оставить ваш номер телефона, или ссылку на ваш акк.в ТГ. Наш специалист по проверке свяжется с вами в ближайшее время”

Если вопрос про повышение стоимости своего (!) бренда, нужно отвечать:
“1. Зарегистрируйте товарный знак, чтобы защитить уникальность. 
2. Разработайте сильный фирменный стиль и визуальную айдентику
3. Создайте репутацию качества и надёжности (отзывы клиентов, PR)
4. Вложитесь в маркетинг и узнаваемость
5. Оцените стоимость интеллектуальной собственности (НМА)

Если хотите полный алгоритм или помощь, оставьте номер телефона или аккаунт в telegram”

если вопрос о том, как долго будет проходить регистрация (товарного знака), ответ: “Сроки регистрации индивидуальные. Обычно занимает в среднем от 6 до 9 месяцев. Порой бывает и за 4 месяца.“

Если пользователь спрашивает про то, можно ли зарегистрировать одновременно и изображение, и слово, надо ответить: “Есть несколько вариаций регистрации: словесный - чисто название. Графический- логотип (картинка), комбинированный - слово и картинка”

Если клиент пишет “Можно картинку со словом зарегистрировать” - это он не просит картинку, а спрашивает, можно ли зарегистрировать одновременно и логотип, и слово (название). Тут тоже надо ответить: “Есть несколько вариаций регистрации: словесный - чисто название. Графический- логотип (картинка), комбинированный - слово и картинка”


Если пользователь говорит “запатентовать” (говоря о логотипе и названии), мы в текст ответа добавляем фразу:

“Маленький важный момент: товарные знаки - регистрируются. Патентуются только изобретения, полезные модели.” 


База знаний: Срок действия товарного знака 10 лет, не варьируется.

'''
)

# ---------------------------------------------------------------------------
# Инициализация базы данных
# ---------------------------------------------------------------------------
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
            user_id INTEGER,
            username TEXT,
            role TEXT,
            message TEXT,
            image BLOB,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    return conn

db = init_db()

# ---------------------------------------------------------------------------
# Отправка email-уведомлений
# ---------------------------------------------------------------------------
def send_email_alert(
    subject: str, body: str, images: Sequence[bytes] | None = None
) -> None:
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(body, "plain"))
    if images:
        for idx, data in enumerate(images):
            part = MIMEBase("application", "octet-stream")
            part.set_payload(data)
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition", f"attachment; filename=img{idx}.jpg"
            )
            msg.attach(part)
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, 465) as smtp:
            smtp.login(EMAIL_FROM, SMTP_PASSWORD)
            smtp.send_message(msg)
    except Exception as e:
        logger.error("Failed to send email: %s", e)

# ---------------------------------------------------------------------------
# Follow-up reminders management
# ---------------------------------------------------------------------------
# Хранит задачи напоминаний по chat_id
followup_tasks: dict[int, tuple[asyncio.Task, asyncio.Task]] = {}

async def schedule_followup_30(chat_id: int):
    try:
        await asyncio.sleep(30)
        await bot.send_message(chat_id, "Проведем бесплатную экспертизу?")
    except asyncio.CancelledError:
        pass

async def schedule_followup_180(chat_id: int):
    try:
        await asyncio.sleep(180)
        await bot.send_message(
            chat_id,
            "Понимаю, мой ответ возможно вас не устроил. "
            "Но на самом деле, чтобы ответить на ваши вопросы, "
            "необходимо провести первичную диагностику, "
            "чтобы не вводить вас в заблуждение и выдать вам точную, правдивую информацию"
        )
    except asyncio.CancelledError:
        # если отменили, просто выходим
        return
    # после успешной отправки — удаляем задачи, чтобы не повторять
    followup_tasks.pop(chat_id, None)

# ---------------------------------------------------------------------------
# Хэндлер команд и сообщений
# ---------------------------------------------------------------------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext) -> None:
    # Отмена старых задач, если есть
    chat_id = message.chat.id
    if chat_id in followup_tasks:
        for t in followup_tasks[chat_id]:
            t.cancel()
        followup_tasks.pop(chat_id, None)

    history = [
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

    # Планируем follow-up после ответа
    task30 = asyncio.create_task(schedule_followup_30(chat_id))
    task180 = asyncio.create_task(schedule_followup_180(chat_id))
    followup_tasks[chat_id] = (task30, task180)

@dp.message()
async def handle(message: types.Message, state: FSMContext) -> None:
    chat_id = message.chat.id
    text = message.text or ""
    user_text = text.strip()

    # Отменяем любые ранее запланированные напоминалки
    if chat_id in followup_tasks:
        for task in followup_tasks[chat_id]:
            task.cancel()
        followup_tasks.pop(chat_id, None)

    # … здесь остальной ваш код (Google Sheets, сохранение в БД, детект телефона, «ананас», OpenAI и т.д.)

    # Допустим, мы получили и отправили reply:
    # await message.answer(reply)

    # И снова планируем follow-up только один раз
    task30 = asyncio.create_task(schedule_followup_30(chat_id))
    task180 = asyncio.create_task(schedule_followup_180(chat_id))
    followup_tasks[chat_id] = (task30, task180)

# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info("Bot starting…")
    asyncio.run(dp.start_polling(bot))
