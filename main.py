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
# ---------------------------------# -*- coding: utf-8 -*-
"""Telegram‑бот для первичного общения с клиентами BeBrand.

Исправлено:
1. **Ключ сервис‑аккаунта** больше не хранится в коде. Ожидается в переменной окружения
   `GOOGLE_SA_JSON` (полный JSON сервис‑аккаунта Google). Это устраняет ошибку
   `invalid_grant: Invalid JWT Signature` и убирает чувствительные данные из репозитория.
2. SMTP‑хост теперь берётся из переменной `SMTP_HOST` (по‑умолчанию smtp.gmail.com).
3. Добавлены аннотации типов и небольшая реорганизация кода.
4. Вся работа с Google Sheets вынесена в функцию `init_google_sheet()`.
5. Для локальной БД предусмотрен путь из `RENDER_DATA_DIR` (volume на Render.com).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import smtplib
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from pathlib import Path
from typing import Any, List, Mapping, MutableMapping, Sequence

import openai
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

# --- Опциональные зависимости (обёрнуты в try, чтобы бот стартовал без Google) ---
try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:  # pragma: no cover
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
RENDER_DATA_DIR: str = os.getenv("RENDER_DATA_DIR", "/tmp")

# Проверка обязательных переменных (без Google)
_required: Sequence[tuple[str, str | None]] = [
    ("API_TOKEN", API_TOKEN),
    ("OPENAI_API_KEY", OPENAI_API_KEY),
    ("ALERT_CHAT_ID", ALERT_CHAT_ID_RAW),
    ("EMAIL_FROM", EMAIL_FROM),
    ("EMAIL_TO", EMAIL_TO),
    ("SMTP_PASSWORD", SMTP_PASSWORD),
]
_missing: list[str] = [name for name, val in _required if not val]
if _missing:
    raise RuntimeError(f"Missing environment variables: {', '.join(_missing)}")

ALERT_CHAT_ID: int = int(ALERT_CHAT_ID_RAW)  # type: ignore[arg-type]

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OpenAI & Telegram‑бот
# ---------------------------------------------------------------------------
openai.api_key = OPENAI_API_KEY
bot = Bot(token=API_TOKEN)  # type: ignore[arg-type]
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------

def init_google_sheet() -> "gspread.models.Worksheet | None":  # noqa: D401
    """Инициализирует и возвращает объект Worksheet либо None, если Sheets не настроен."""
    if not (GOOGLE_SHEET_NAME and gspread and Credentials and GOOGLE_SA_JSON):
        logger.warning("Google Sheets is not configured—or required modules are missing.")
        return None

    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds_info = json.loads(GOOGLE_SA_JSON)
        creds = Credentials.from_service_account_info(creds_info, scopes=scopes)  # type: ignore[arg-type]
        gc = gspread.authorize(creds)
        return gc.open(GOOGLE_SHEET_NAME).sheet1  # type: ignore[return-value]
    except Exception as exc:  # pragma: no cover
        logger.error("Google Sheets init failed: %s", exc)
        return None


gsheet = init_google_sheet()

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

Тебе нужно подсветить всевозможные подводные камни и риски — то есть, причины получить как можно скорее бесплатную проверку названия и логотипа клиента как потенциального товарного знака. Не надо писать сразу большие сообщения.

Задавай вопросы, чтобы беседа шла по сценарию, ведущему клиента к пониманию его болей и проблемы.

Не надо писать «Спасибо, что поделились, что у вас уже есть название и логотип», и подобные благодарности за ничего не стоящие человеку действия, это бред. Не надо повторять за человеком то, что он только что написал.

В первых нескольких сообщениях важно задать уточняющие вопросы.

Очень важно получить ответы на следующие вопросы: [—на каких площадках? вывеска будет у вас? на сайте? в соц сетях? маркетплейсы? реклама, радио, видео? будете продавать, производить продукцию или оказывать услуги?].

Если клиент готов к проверке его товарного знака, надо сказать «напишите свой номер телефона, и бот подключит к диалогу менеджера по проверке названия» (в этой фразе нужно говорить от лица менеджера про бота, который подключит).

Делай вид, что ты человек. Не представляйся ботом и не упоминай слово «бот».

Первым сообщением должно быть: «Здравствуйте! Пока я зову менеджера, ответьте на вопрос: есть ли у вас уже название или логотип для вашего бизнеса?». Следующий ответ на сообщение пользователя должен начинаться с «Здравствуйте, меня зовут Алексей Баженов, я руководитель удмуртского филиала компании BeBrand в Ижевске» + попытаться узнать имя.

(…остальной сценарий опущен для краткости, без изменений)
"""

# ---------------------------------------------------------------------------
# Работа с базой данных
# ---------------------------------------------------------------------------
DB_PATH = Path(RENDER_DATA_DIR) / "messages.db"


def init_db(path: Path = DB_PATH) -> sqlite3.Connection:  # noqa: D401
    """Создаёт (или проверяет) файл SQLite и возвращает соединение."""
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
# Почта
# ---------------------------------------------------------------------------

def send_email_alert(subject: str, body: str, images: Sequence[bytes] | None = None) -> None:  # noqa: D401
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
    except Exception as exc:  # pragma: no cover
        logger.error("Email send failed: %s", exc)

# ---------------------------------------------------------------------------
# Хэндлеры Aiogram
# ---------------------------------------------------------------------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext) -> None:  # noqa: D401
    history: list[MutableMapping[str, str]] = [
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
async def handle(message: types.Message, state: FSMContext) -> None:  # noqa: D401
    user_text = (message.text or "").strip()

    # «отправь данные» — показываем первые строки из Google Sheets
    if user_text.lower() == "отправь данные":
        if gsheet:
            recs: List[Mapping[str, Any]] = gsheet.get_all_records()  # type: ignore[assignment]
            if not recs:
                await message.answer("В таблице нет данных.")
                return
            lines = [", ".join(f"{k}:{v}" for k, v in r.items()) for r in recs[:5]]
            await message.answer("Первые записи:\n" + "\n".join(lines))
        else:
            await message.answer("Google Sheets не настроена.")
        return

    # —–– обычный диалог –––
    data = await state.get_data()
    history: list[MutableMapping[str, str]] = data.get("chat_history", [])
    if not history:
        history.append({"role": "system", "content": SYSTEM_PROMPT})
    history.append({"role": "user", "content": user_text})

    # сохраняем в БД
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO messages(user_id, username, role, message) VALUES(?,?,?,?)",
        (message.from_user.id, message.from_user.username or "", "user", user_text),
    )
    db_conn.commit()

    # телефон?
    m = PHONE_REGEX.search(user_text)
    if m:
        txt = f"Пользователь оставил тел.: {m.group(1)}"
        await bot.send_message(ALERT_CHAT_ID, txt)
        send_email_alert("Телефон", txt)

    # служебная команда «ананас»
    if user_text.lower() == "ананас":
        rows = cur.execute(
            "SELECT role, message, timestamp FROM messages WHERE user_id=? ORDER BY timestamp",
            (message.from_user.id,),
        ).fetchall()
        txt = "".join(f"[{ts}]({r}){m}\n" for r, m, ts in rows)
        send_email_alert("Переписка", txt)
        await message.answer("Отправлено менеджеру")
        return

    # ChatGPT
    try:
        resp = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=history,  # type: ignore[arg-type]
            max_tokens=500,
            temperature=0.9,
        )
        reply = resp.choices[0].message.content  # type: ignore[attr-defined]
    except Exception:
        reply = "Ошибка. Попробуйте позже."

    history.append({"role": "assistant", "content": reply})
    await state.update_data(chat_history=history)

    cur.execute(
        "INSERT INTO messages VALUES(?,?,?,?,NULL,datetime('now'))",
        (message.from_user.id, message.from_user.username or "", "assistant", reply),
    )
    db_conn.commit()

    # легкая задержка, чтобы имитировать набор текста
    await asyncio.sleep(3)
    await message.answer(reply)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info("Bot starting…")
    asyncio.run(dp.start_polling(bot))
    
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
