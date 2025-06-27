# -*- coding: utf-8 -*-
"""Telegram-бот для первичного общения с клиентами BeBrand.

Исправлено:
1. Убрана интеграция с Google Sheets.
2. SMTP-хост берётся из переменной `SMTP_HOST` (по-умолчанию smtp.gmail.com).
3. Добавлены аннотации типов и реорганизация кода.
4. Для локальной БД предусмотрён путь из `RENDER_DATA_DIR` (volume на Render.com).
"""

from __future__ import annotations

import asyncio
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
from typing import Any, Sequence, Mapping, MutableMapping

import openai
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

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

_required: list[tuple[str, str | None]] = [
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
# Регулярка для телефона
# ---------------------------------------------------------------------------
PHONE_REGEX = re.compile(r"(\+?\d[\d\s\-]{7,}\d)")

# ---------------------------------------------------------------------------
# Системный промпт
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """
Отвечай только на русском языке.
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
    history: list[Mapping[str, Any]] = [
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
