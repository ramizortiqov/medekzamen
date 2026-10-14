""
import asyncio
import html
import logging
import os
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import asyncpg
from dotenv import load_dotenv

from aiogram import BaseMiddleware, Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

# -------------------- 1. ЛОГИРОВАНИЕ --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# -------------------- 2. НАСТРОЙКИ --------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("REDIS_URL")  # необязательно: если задан — состояния FSM переживут перезапуск

DEFAULT_ADMIN_IDS = {6720999592, 6520890849, 5743119052}


def _parse_admin_ids() -> set:
    raw = os.getenv("ADMIN_IDS", "")
    ids = {int(x) for x in raw.replace(";", ",").split(",") if x.strip().lstrip("-").isdigit()}
    return ids or set(DEFAULT_ADMIN_IDS)


ADMIN_IDS = _parse_admin_ids()
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1002034189536"))
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/fr_ray7")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://study.tj/")
BOT_SIGNATURE = os.getenv("BOT_SIGNATURE", "\n@MedEkzamenBot")
# 1 — запретить пересылку/сохранение выдаваемых материалов обычным пользователям
PROTECT_CONTENT = os.getenv("PROTECT_CONTENT", "0") == "1"
# 1 — если Telegram API недоступен / бот не админ канала, НЕ блокировать всех пользователей
SUB_FAIL_OPEN = os.getenv("SUB_FAIL_OPEN", "1") == "1"

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен в .env")


def build_storage():
    if REDIS_URL:
        from aiogram.fsm.storage.redis import RedisStorage  # требует пакет redis
        return RedisStorage.from_url(REDIS_URL)
    return MemoryStorage()


storage = build_storage()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=storage)

# -------------------- 3. ДАННЫЕ О КУРСАХ И ПРЕДМЕТАХ --------------------
GROUPS = {"ru": "🇷🇺 Русский", "tj": "🇹🇯 Таджикский"}
COURSES = [1, 2, 3, 4, 5, 6]
MAX_COURSE_FOR_SUMMARY = 3
SUMMARY_SECTIONS = 4
MAT_PREFIX = "mat"

# ЕДИНЫЙ источник правды: курс -> [(тег_экзамена, название)].
# Тег материалов = "mat" + тег_экзамена, полный тег материала = "<тип>_<mat-тег>".
SUBJECTS_BY_COURSE: Dict[int, List[Tuple[str, str]]] = {
    1: [
        ("chem1", "🧪 Химия"),
        ("bio1", "🧬 Биология"),
        ("anat1", "💀 Анатомия"),
        ("phys1", "⚛️ Физика"),
    ],
    2: [
        ("anat2", "💀 Анатомия (2)"),
        ("gisto2", "🔬 Гистология"),
        ("phys2", "🏃 Физиология"),
        ("biohim2", "🧪 Биохимия"),
    ],
    3: [
        ("microb3", "🦠 Микроб"),
        ("patfiz3", "🤢 Патфиз"),
        ("topanat3", "🧠 Топанатомия"),
        ("farmak3", "💊 Фармак"),
        ("hirurgia3", "🔪 Хирургия"),
        ("dentistry3", "🦷 Стоматология"),
        ("generalhygiene3", "🧼 Общая гигиена"),
        ("propv3", "👴 Пропедевтика взр"),
        ("propd3", "👶 Пропедевтика детск"),
    ],
    4: [
        ("dermatovenereology4", "🔬 Дерматовенерология"),
        ("pediatrics4", "👶 Педиатрия"),
        ("topanatomy4", "🗺️ Топографическая анатомия"),
        ("generalhygiene4", "🧼 Общая гигиена"),
        ("radiology4", "📡 Рентгенология"),
        ("internalmed4", "🫀 Внутренние болезни"),
        ("occupationalpath4", "🏭 Профессиональная патология"),
        ("neurology4", "🧠 Неврология"),
        ("obstetrics4", "🤰 Акушерство"),
        ("surgicaldiseases4", "🔪 Хирургические болезни"),
        ("socialhygiene4", "🏥 Социальная гигиена"),
        ("endocrinology4", "🦋 Эндокринология"),
        ("ent4", "👂 Оториноларингология"),
        ("militarytest14", "🎖️ Военный тест 1 семестр"),
        ("militarytest24", "🎖️ Военный тест 2 семестр"),
    ],
    5: [
        ("familymed5", "👨‍👩‍👧‍👦 Семейная медицина"),
        ("vascularsurg5", "🩸 Сосудистая хирургия"),
        ("internal5", "🫀 Внутренние болезни"),
        ("traumatology5", "🦴 Травматология"),
        ("epidemiology5", "📊 Эпидемиология"),
        ("gynecology5", "🌸 Гинекология"),
        ("socialhygiene5", "🏥 Социальная гигиена"),
        ("phthisiology5", "🫁 Фтизиатрия"),
        ("psychiatry5", "🧠 Психиатрия"),
        ("urology5", "💧 Урология"),
        ("politology5", "🏛️ Политология"),
        ("pediatricsurg5", "👶 Детская хирургия"),
        ("ophthalmology5", "👁️ Офтальмология"),
        ("anesthesiology5", "💤 Анестезиология"),
        ("pediatrics5", "👶 Педиатрия"),
    ],
    6: [
        ("combtrauma6", "🩺 Сочетанные травмы"),
        ("transplant6", "🧬 Трансплантология"),
        ("obstetrics6", "🤰 Акушерство"),
        ("internal6", "🫀 Внутренние болезни"),
        ("econtheory6", "📊 Экономическая теория"),
        ("childinfect6", "🌡️ Детские инфекционные болезни"),
        ("pediatrics6", "👶 Педиатрия"),
        ("surgery6", "🔪 Хирургия"),
        ("stateexam6", "📝 Государственный экзамен"),
        ("neurosurgery6", "🧠 Нейрохирургия"),
        ("familymed6", "👨‍👩‍👧‍👦 Семейная медицина"),
        ("infectious6", "🦠 Инфекционные болезни"),
        ("militarymed6", "🎖️ Военно-полевая терапия"),
        ("clinicalpharm6", "💊 Клиническая фармакология"),
        ("oncology6", "🎗️ Онкология"),
        ("exercisether6", "🏃‍♂️ Лечебная физкультура"),
        ("forensic6", "🔍 Судебная медицина"),
    ],
}

# --- Производные структуры (генерируются, руками не править) ---
ALL_SUBJECTS_MAP: Dict[str, str] = {t: n for subs in SUBJECTS_BY_COURSE.values() for t, n in subs}
MATERIALS_SUBJECTS_MAP: Dict[str, str] = {MAT_PREFIX + t: n for t, n in ALL_SUBJECTS_MAP.items()}
COURSE_SUBJECTS: Dict[int, List[str]] = {c: [t for t, _ in subs] for c, subs in SUBJECTS_BY_COURSE.items()}
MATERIALS_COURSE_SUBJECTS: Dict[int, List[str]] = {
    c: [MAT_PREFIX + t for t in tags] for c, tags in COURSE_SUBJECTS.items()
}
EXAM_NAME_TO_TAG: Dict[int, Dict[str, str]] = {
    c: {n: t for t, n in subs} for c, subs in SUBJECTS_BY_COURSE.items()
}
MATERIALS_NAME_TO_TAG: Dict[int, Dict[str, str]] = {
    c: {n: MAT_PREFIX + t for t, n in subs} for c, subs in SUBJECTS_BY_COURSE.items()
}
ALL_SUBJECT_NAMES: List[str] = list(set(ALL_SUBJECTS_MAP.values()))

MATERIAL_TYPES = {
    "lecture": "📖 Лекции+темплан",
    "practice": "🔬 Практика",
    "video": "🎥 Видео",
}

# Все допустимые теги (для проверки callback-данных)
VALID_TAGS = set(ALL_SUBJECTS_MAP.keys())
for _c in range(1, MAX_COURSE_FOR_SUMMARY + 1):
    for _i in range(1, SUMMARY_SECTIONS + 1):
        VALID_TAGS.add(f"summary{_c}.{_i}")
for _t in MATERIAL_TYPES:
    for _mtag in MATERIALS_SUBJECTS_MAP:
        VALID_TAGS.add(f"{_t}_{_mtag}")

# Типы вложений, которые умеем хранить и отправлять: тип -> (метод бота, имя аргумента)
MEDIA_SENDERS = {
    "photo": ("send_photo", "photo"),
    "video": ("send_video", "video"),
    "document": ("send_document", "document"),
    "audio": ("send_audio", "audio"),
    "voice": ("send_voice", "voice"),
    "animation": ("send_animation", "animation"),
}

# Фильтр «только админ» для хендлеров
ADMIN_ONLY = F.from_user.id.in_(ADMIN_IDS)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# -------------------- 4. БАЗА ДАННЫХ --------------------
USER_CACHE_TTL = 60
BLOCK_CACHE_TTL = 30
TOUCH_INTERVAL = 600


class PostgresDB:
    """Работа с PostgreSQL через asyncpg (с небольшим кэшем пользователей и блокировок)."""

    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None
        self._user_cache: Dict[int, Tuple[float, Any]] = {}
        self._blocked_cache: Dict[int, Tuple[float, bool]] = {}
        self._last_touch: Dict[int, float] = {}

    async def init_pool(self):
        if not DATABASE_URL:
            raise ValueError("DATABASE_URL не установлен в .env")
        try:
            self.pool = await asyncpg.create_pool(DATABASE_URL)
            logger.info("PostgreSQL pool initialized successfully.")
            await self._init_db_schema()
        except Exception as e:
            logger.error(f"FATAL: Не удалось подключиться к PostgreSQL. Ошибка: {e}")
            raise

    async def close(self):
        if self.pool:
            await self.pool.close()

    async def _init_db_schema(self):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    course INTEGER,
                    group_lang TEXT,
                    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS materials (
                    id SERIAL PRIMARY KEY,
                    tag TEXT NOT NULL,
                    type TEXT NOT NULL,
                    file_id TEXT,
                    file_name TEXT,
                    caption TEXT,
                    course INTEGER,
                    group_lang TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS blocked_users (
                    user_id BIGINT PRIMARY KEY,
                    blocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    blocked_by BIGINT,
                    reason TEXT
                );
                CREATE TABLE IF NOT EXISTS admin_log (
                    id SERIAL PRIMARY KEY,
                    admin_id BIGINT,
                    action TEXT NOT NULL,
                    details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_materials_tag ON materials (tag);
                CREATE INDEX IF NOT EXISTS idx_materials_course_group ON materials (course, group_lang);

                -- лёгкие «миграции»: новые колонки добавляются, если их ещё нет
                ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active TIMESTAMP;
                ALTER TABLE materials ADD COLUMN IF NOT EXISTS uploaded_by BIGINT;
            ''')
            logger.info("PostgreSQL schema initialized/checked.")

    # ---------- пользователи ----------
    async def get_user(self, user_id: int):
        cached = self._user_cache.get(user_id)
        if cached and time.monotonic() - cached[0] < USER_CACHE_TTL:
            return cached[1]
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow('SELECT * FROM users WHERE user_id = $1', user_id)
        if row is not None:
            if len(self._user_cache) > 5000:
                self._user_cache.clear()
            self._user_cache[user_id] = (time.monotonic(), row)
        return row

    async def save_user(self, user_id: int, username: str, full_name: str, course: int, group_lang: str):
        """Создаёт пользователя. Курс и группа существующего пользователя НЕ перезаписываются
        (их меняет только админ через /setcourse) — это соответствует правилу «курс изменить нельзя»."""
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO users (user_id, username, full_name, course, group_lang)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    full_name = EXCLUDED.full_name
            ''', user_id, username, full_name, course, group_lang)
        self._user_cache.pop(user_id, None)

    async def set_user_course(self, user_id: int, course: int, group_lang: Optional[str] = None) -> int:
        async with self.pool.acquire() as conn:
            if group_lang:
                result = await conn.execute(
                    'UPDATE users SET course = $2, group_lang = $3 WHERE user_id = $1', user_id, course, group_lang)
            else:
                result = await conn.execute('UPDATE users SET course = $2 WHERE user_id = $1', user_id, course)
        self._user_cache.pop(user_id, None)
        return int(result.split()[-1])

    async def promote_all(self) -> int:
        """Переводит всех пользователей на курс выше (кроме последнего курса)."""
        async with self.pool.acquire() as conn:
            count = await conn.fetchval('''
                WITH u AS (
                    UPDATE users SET course = course + 1 WHERE course < $1 RETURNING 1
                ) SELECT COUNT(*) FROM u
            ''', max(COURSES))
        self._user_cache.clear()
        return count

    async def touch_user(self, user_id: int):
        """Обновляет last_active не чаще раза в TOUCH_INTERVAL секунд на пользователя."""
        now = time.monotonic()
        if now - self._last_touch.get(user_id, -TOUCH_INTERVAL - 1) < TOUCH_INTERVAL:
            return
        self._last_touch[user_id] = now
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    'UPDATE users SET last_active = CURRENT_TIMESTAMP, is_active = TRUE WHERE user_id = $1', user_id)
        except Exception as e:
            logger.warning(f"touch_user failed for {user_id}: {e}")

    async def mark_inactive(self, user_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE users SET is_active = FALSE WHERE user_id = $1', user_id)

    # ---------- блокировки ----------
    async def block_user(self, user_id: int, blocked_by: int, reason: Optional[str] = None):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO blocked_users (user_id, blocked_by, reason)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id) DO UPDATE SET
                    blocked_at = CURRENT_TIMESTAMP,
                    blocked_by = EXCLUDED.blocked_by,
                    reason = EXCLUDED.reason
            ''', user_id, blocked_by, reason)
        self._blocked_cache.pop(user_id, None)

    async def unblock_user(self, user_id: int) -> int:
        async with self.pool.acquire() as conn:
            result = await conn.execute('DELETE FROM blocked_users WHERE user_id = $1', user_id)
        self._blocked_cache.pop(user_id, None)
        return int(result.split()[-1])

    async def is_user_blocked(self, user_id: int) -> bool:
        cached = self._blocked_cache.get(user_id)
        if cached and time.monotonic() - cached[0] < BLOCK_CACHE_TTL:
            return cached[1]
        async with self.pool.acquire() as conn:
            result = await conn.fetchval('SELECT user_id FROM blocked_users WHERE user_id = $1', user_id)
        blocked = result is not None
        if len(self._blocked_cache) > 5000:
            self._blocked_cache.clear()
        self._blocked_cache[user_id] = (time.monotonic(), blocked)
        return blocked

    async def get_blocked_users(self, limit: int = 50):
        async with self.pool.acquire() as conn:
            return await conn.fetch('''
                SELECT b.user_id, b.blocked_at, b.blocked_by, b.reason,
                       u.username, u.full_name, u.course, u.group_lang
                FROM blocked_users b
                LEFT JOIN users u ON b.user_id = u.user_id
                ORDER BY b.blocked_at DESC
                LIMIT $1
            ''', limit)

    # ---------- материалы ----------
    async def get_materials_by_tag(self, tag: str):
        async with self.pool.acquire() as conn:
            return await conn.fetch('SELECT * FROM materials WHERE tag = $1 ORDER BY created_at, id', tag)

    async def save_material(self, tag: str, type_: str, file_id: str = None, file_name: str = None,
                            caption: str = None, course: int = None, group_lang: str = None,
                            uploaded_by: int = None):
        async with self.pool.acquire() as conn:
            return await conn.fetchval('''
                INSERT INTO materials (tag, type, file_id, file_name, caption, course, group_lang, uploaded_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING id
            ''', tag, type_, file_id, file_name, caption, course, group_lang, uploaded_by)

    async def delete_material(self, material_id: int) -> int:
        async with self.pool.acquire() as conn:
            result = await conn.execute('DELETE FROM materials WHERE id = $1', material_id)
        return int(result.split()[-1])

    async def delete_materials_by_tag(self, tag: str) -> int:
        async with self.pool.acquire() as conn:
            result = await conn.execute('DELETE FROM materials WHERE tag = $1', tag)
        return int(result.split()[-1])

    async def get_materials_summary(self):
        async with self.pool.acquire() as conn:
            return await conn.fetch('''
                SELECT tag, type, course, group_lang, COUNT(*) AS cnt
                FROM materials
                GROUP BY tag, type, course, group_lang
                ORDER BY tag, type
            ''')

    # ---------- рассылка / статистика / журнал ----------
    async def get_users_for_broadcast(self, course: int, group_lang: str) -> List[int]:
        """course=0 — все курсы, group_lang='all' — все группы. Блокированные и ушедшие исключаются."""
        conditions = ["is_active IS NOT FALSE", "user_id NOT IN (SELECT user_id FROM blocked_users)"]
        args: List[Any] = []
        if course != 0:
            args.append(course)
            conditions.append(f"course = ${len(args)}")
        if group_lang != 'all':
            args.append(group_lang)
            conditions.append(f"group_lang = ${len(args)}")
        query = "SELECT user_id FROM users WHERE " + " AND ".join(conditions)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *args)
        return [row['user_id'] for row in rows]

    async def get_stats(self) -> Dict[str, Any]:
        async with self.pool.acquire() as conn:
            return {
                "total_users": await conn.fetchval('SELECT COUNT(*) FROM users'),
                "active_7d": await conn.fetchval(
                    "SELECT COUNT(*) FROM users WHERE last_active > NOW() - INTERVAL '7 days'"),
                "active_30d": await conn.fetchval(
                    "SELECT COUNT(*) FROM users WHERE last_active > NOW() - INTERVAL '30 days'"),
                "inactive": await conn.fetchval('SELECT COUNT(*) FROM users WHERE is_active = FALSE'),
                "blocked": await conn.fetchval('SELECT COUNT(*) FROM blocked_users'),
                "by_course": await conn.fetch(
                    'SELECT course, COUNT(*) AS cnt FROM users GROUP BY course ORDER BY course'),
                "by_group": await conn.fetch(
                    'SELECT group_lang, COUNT(*) AS cnt FROM users GROUP BY group_lang ORDER BY group_lang'),
                "total_materials": await conn.fetchval('SELECT COUNT(*) FROM materials'),
                "top_tags": await conn.fetch(
                    'SELECT tag, COUNT(*) AS cnt FROM materials GROUP BY tag ORDER BY cnt DESC LIMIT 10'),
            }

    async def log_admin_action(self, admin_id: int, action: str, details: str = ""):
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    'INSERT INTO admin_log (admin_id, action, details) VALUES ($1, $2, $3)',
                    admin_id, action, details)
        except Exception as e:
            logger.warning(f"admin_log failed: {e}")


db = PostgresDB()


# -------------------- 5. FSM СОСТОЯНИЯ --------------------
class RegistrationStates(StatesGroup):
    waiting_for_course = State()
    waiting_for_group = State()
    waiting_for_confirmation = State()


class AdminStates(StatesGroup):
    waiting_for_content = State()
    waiting_for_admin_group = State()
    waiting_for_exam_course = State()
    waiting_for_summary_course = State()
    waiting_for_materials_type = State()
    waiting_for_materials_course = State()
    waiting_for_broadcast_filter = State()
    waiting_for_broadcast_course = State()
    waiting_for_broadcast_content = State()
    waiting_for_broadcast_confirm = State()


class FeedbackStates(StatesGroup):
    waiting_for_feedback_message = State()


class AdminReplyStates(StatesGroup):
    waiting_for_reply_message = State()


class MaterialsStates(StatesGroup):
    waiting_for_material_type = State()
    waiting_for_subject = State()


class ExamStates(StatesGroup):
    waiting_for_subject = State()


# -------------------- 6. СЕРВИСНЫЕ ФУНКЦИИ --------------------
SUB_CACHE_TTL = 120
_sub_cache: Dict[int, float] = {}
_alert_times: Dict[str, float] = {}
_feedback_last: Dict[int, float] = {}
FEEDBACK_COOLDOWN = 30
_background_tasks: set = set()
_broadcast_running = False


def get_subscription_keyboard() -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔔 Подписка", url=CHANNEL_URL)
    builder.button(text="✅ Проверить", callback_data="check_subscription")
    builder.adjust(2)
    return builder.as_markup()


def get_no_access_message() -> str:
    return "🚫 <b>Доступ ограничен.</b> Для использования бота, пожалуйста, <b>подпишитесь</b> на наш канал."


async def alert_admins(key: str, text: str, interval: int = 3600):
    """Уведомляет админов, но не чаще раза в interval секунд для одного и того же key."""
    now = time.monotonic()
    last = _alert_times.get(key)
    if last is not None and now - last < interval:
        return
    _alert_times[key] = now
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            pass


async def check_subscription(user_id: int, force: bool = False) -> bool:
    if is_admin(user_id):
        return True
    now = time.monotonic()
    if not force and _sub_cache.get(user_id, 0) > now:
        return True
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        ok = member.status in ("member", "administrator", "creator") or (
            member.status == "restricted" and getattr(member, "is_member", False))
        if ok:
            _sub_cache[user_id] = now + SUB_CACHE_TTL
        else:
            _sub_cache.pop(user_id, None)
        return ok
    except Exception as e:
        logger.error(f"Ошибка при проверке подписки для user_id {user_id}: {e}")
        await alert_admins(
            "sub_check",
            f"⚠️ <b>Не удаётся проверить подписку</b> на канал.\n"
            f"Проверьте, что бот — администратор канала.\n<code>{html.escape(str(e))[:300]}</code>")
        return SUB_FAIL_OPEN


def clean_tags(text: str, tag: str) -> str:
    if not text:
        return ""
    cleaned = text.replace(f"#{tag}", "").replace(f"#{tag.upper()}", "")
    return ' '.join(cleaned.split())


def is_command(message: types.Message) -> bool:
    return bool(message.text and message.text.startswith("/"))


def plain_preview(html_text: Optional[str], limit: int = 40) -> str:
    """Короткий безопасный (для HTML) текст-превью подписи."""
    if not html_text:
        return ""
    text = html.unescape(re.sub(r"<[^>]+>", "", html_text))
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit] + "…"
    return html.escape(text)


async def send_long(chat_id: int, text: str, reply_markup=None):
    """Отправка длинного текста частями (лимит Telegram — 4096 символов)."""
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > 4000:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current)
    for i, chunk in enumerate(chunks):
        await bot.send_message(chat_id, chunk, reply_markup=reply_markup if i == len(chunks) - 1 else None)


async def safe_edit_text(message: types.Message, text: str, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


async def with_retry(factory: Callable[[], Awaitable[Any]]):
    """Выполняет запрос к Telegram; при flood-лимите ждёт и повторяет один раз."""
    try:
        return await factory()
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        return await factory()


# -------------------- 7. MIDDLEWARE --------------------
class SubscriptionMiddleware(BaseMiddleware):
    """Проверка блокировки и подписки для сообщений."""

    async def __call__(self, handler: Callable[[types.Message, Dict[str, Any]], Awaitable[Any]],
                       event: types.Message, data: Dict[str, Any]) -> Any:
        user = event.from_user
        if user is None or is_admin(user.id):
            return await handler(event, data)

        if await db.is_user_blocked(user.id):
            await event.answer(
                "🚫 <b>Ваш аккаунт заблокирован.</b>\n\n"
                "Если вы считаете, что это ошибка, свяжитесь с администратором.")
            return

        if not await check_subscription(user.id):
            await event.answer(get_no_access_message(), reply_markup=get_subscription_keyboard())
            return

        await db.touch_user(user.id)
        return await handler(event, data)


class CallbackSubscriptionMiddleware(BaseMiddleware):
    """Проверка блокировки и подписки для callback-запросов."""

    async def __call__(self, handler: Callable[[types.CallbackQuery, Dict[str, Any]], Awaitable[Any]],
                       event: types.CallbackQuery, data: Dict[str, Any]) -> Any:
        user = event.from_user
        if user is None or is_admin(user.id):
            return await handler(event, data)

        if event.data == "check_subscription":
            return await handler(event, data)

        if await db.is_user_blocked(user.id):
            await event.answer("🚫 Ваш аккаунт заблокирован.", show_alert=True)
            return

        if not await check_subscription(user.id):
            await event.answer("⚠️ Подпишитесь на канал для использования бота!", show_alert=True)
            if event.message:
                await event.message.answer(get_no_access_message(), reply_markup=get_subscription_keyboard())
            return

        await db.touch_user(user.id)
        return await handler(event, data)


# -------------------- 8. КЛАВИАТУРЫ --------------------
def build_rows_kb(items: List[str], nav_rows: List[List[str]], per_row: int = 2) -> ReplyKeyboardMarkup:
    """Кнопки-элементы по per_row в ряд, затем навигационные ряды (никогда не смешиваются)."""
    rows: List[List[KeyboardButton]] = []
    for i in range(0, len(items), per_row):
        rows.append([KeyboardButton(text=t) for t in items[i:i + per_row]])
    for nav in nav_rows:
        rows.append([KeyboardButton(text=t) for t in nav])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def get_admin_main_menu_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.button(text="📚 Экзамен")
    builder.button(text="📂 Материалы")
    builder.button(text="📋 Итог")
    builder.button(text="📢 Рассылка")
    builder.button(text="📊 Статистика")
    builder.button(text="🚫 Заблокированные")
    builder.button(text="🚪 Личный кабинет")
    builder.button(text="✉️ Обратная связь")
    builder.adjust(3, 3, 2)
    return builder.as_markup(resize_keyboard=True)


def get_reply_main_menu_keyboard(user_course: int = None, user_id: int = None):
    if user_id is not None and is_admin(user_id):
        return get_admin_main_menu_keyboard()

    builder = ReplyKeyboardBuilder()
    if user_course:
        builder.button(text="📚 Экзамен")
        builder.button(text="📂 Материалы")
        if user_course <= MAX_COURSE_FOR_SUMMARY:
            builder.button(text="📋 Итог")
    builder.button(text="🚪 Личный кабинет")
    builder.button(text="✉️ Обратная связь")

    if user_course and user_course <= MAX_COURSE_FOR_SUMMARY:
        builder.adjust(3, 2)
    elif user_course:
        builder.adjust(2, 2, 1)
    else:
        builder.adjust(2, 1)
    return builder.as_markup(resize_keyboard=True)


async def main_menu_kb(user_id: int):
    user = await db.get_user(user_id)
    return get_reply_main_menu_keyboard(user['course'] if user else None, user_id)


def get_course_selection_keyboard():
    builder = ReplyKeyboardBuilder()
    for course in COURSES:
        builder.button(text=f"{course}-курс")
    builder.adjust(3, 3)
    return builder.as_markup(resize_keyboard=True)


def get_group_selection_keyboard():
    builder = ReplyKeyboardBuilder()
    for value in GROUPS.values():
        builder.button(text=value)
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)


def get_confirmation_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.button(text="✅ Подтвердить выбор")
    builder.button(text="🔙 Назад к выбору курса")
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)


def get_reply_exam_course_keyboard(max_course: int):
    items = [f"{c}-й курс" for c in range(1, max_course + 1)]
    return build_rows_kb(items, [["🔙 Главное меню"]], per_row=3)


def get_reply_exam_subject_keyboard(course_num: int, user_id: int):
    names = [ALL_SUBJECTS_MAP[t] for t in COURSE_SUBJECTS.get(course_num, [])]
    if is_admin(user_id):
        nav = [["🔙 К выбору курса", "🔙 Главное меню"]]
    else:
        nav = [["🔙 Главное меню"]]
    return build_rows_kb(names, nav, per_row=2)


def get_reply_materials_menu_keyboard():
    builder = ReplyKeyboardBuilder()
    for name in MATERIAL_TYPES.values():
        builder.button(text=name)
    builder.button(text="🔙 Главное меню")
    builder.adjust(3, 1)
    return builder.as_markup(resize_keyboard=True)


def get_reply_summary_course_keyboard(user_course: int, user_id: int):
    max_visible = MAX_COURSE_FOR_SUMMARY if is_admin(user_id) else min(MAX_COURSE_FOR_SUMMARY, user_course)
    items = [f"Итог - {c} курс" for c in range(1, max_visible + 1)]
    return build_rows_kb(items, [["🔙 Главное меню"]], per_row=3)


def get_reply_final_summary_keyboard(course_num: int):
    builder = ReplyKeyboardBuilder()
    for i in range(1, SUMMARY_SECTIONS + 1):
        builder.button(text=f"Итог {course_num}.{i}")
    builder.button(text="🔙 Главное меню")
    builder.adjust(4, 1)
    return builder.as_markup(resize_keyboard=True)


def get_reply_materials_course_keyboard(max_course: int):
    items = [f"{c}-й курс" for c in range(1, max_course + 1)]
    return build_rows_kb(items, [["🔙 К типам материалов", "🔙 Главное меню"]], per_row=3)


def get_reply_materials_subject_keyboard(course_num: int, user_id: int):
    names = [MATERIALS_SUBJECTS_MAP[t] for t in MATERIALS_COURSE_SUBJECTS.get(course_num, [])]
    if is_admin(user_id):
        nav = [["🔙 К типам материалов", "🔙 К выбору курса (Материалы)"], ["🔙 Главное меню"]]
    else:
        nav = [["🔙 К типам материалов", "🔙 Главное меню"]]
    return build_rows_kb(names, nav, per_row=2)


# --- Inline-клавиатуры админа ---
def get_admin_menu(tag: str) -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=f"➕ Загрузить в #{tag}", callback_data=f"upload__{tag}")
    builder.button(text=f"🗑️ Удалить из #{tag}", callback_data=f"delete_indexed__{tag}")
    builder.adjust(1)
    return builder.as_markup()


def _inline_rows(buttons: List[InlineKeyboardButton], per_row: int) -> List[List[InlineKeyboardButton]]:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def kb_admin_sections() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📚 Экзамен", callback_data="admsec__exam")],
        [InlineKeyboardButton(text="📂 Материалы", callback_data="admsec__mat")],
        [InlineKeyboardButton(text="📋 Итог", callback_data="admsec__sum")],
    ])


def kb_admin_courses(section: str) -> InlineKeyboardMarkup:
    courses = range(1, MAX_COURSE_FOR_SUMMARY + 1) if section == "sum" else COURSES
    buttons = [InlineKeyboardButton(text=f"{c}-й курс", callback_data=f"admcourse__{section}__{c}") for c in courses]
    rows = _inline_rows(buttons, 3)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admmenu_root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_admin_tags(section: str, course: int) -> InlineKeyboardMarkup:
    items: List[Tuple[str, str]] = []
    if section == "exam":
        items = [(t, ALL_SUBJECTS_MAP[t]) for t in COURSE_SUBJECTS.get(course, [])]
    elif section == "mat":
        for type_key, type_name in MATERIAL_TYPES.items():
            emoji = type_name.split()[0]
            for mtag in MATERIALS_COURSE_SUBJECTS.get(course, []):
                items.append((f"{type_key}_{mtag}", f"{emoji} {MATERIALS_SUBJECTS_MAP[mtag]}"))
    elif section == "sum":
        items = [(f"summary{course}.{i}", f"Итог {course}.{i}") for i in range(1, SUMMARY_SECTIONS + 1)]
    buttons = [InlineKeyboardButton(text=name, callback_data=f"select_admin_tag__{tag}") for tag, name in items]
    rows = _inline_rows(buttons, 2)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admsec__{section}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# -------------------- 9. ОТПРАВКА КОНТЕНТА --------------------
async def send_material(chat_id: int, material, tag: str):
    """Отправляет один материал. Подписи длиннее лимита уходят отдельным сообщением."""
    mat_type = material['type']
    file_id = material['file_id']
    caption = clean_tags(material['caption'] or "", tag)
    protect = PROTECT_CONTENT and not is_admin(chat_id)

    if mat_type == "text":
        if caption:
            await with_retry(lambda: bot.send_message(chat_id, caption, protect_content=protect))
        return

    sender = MEDIA_SENDERS.get(mat_type)
    if not sender or not file_id:
        return
    method_name, arg_name = sender
    method = getattr(bot, method_name)

    final_caption = caption + BOT_SIGNATURE
    if len(final_caption) > 1000:
        await with_retry(lambda: method(chat_id, **{arg_name: file_id}, protect_content=protect))
        await with_retry(lambda: bot.send_message(chat_id, final_caption, protect_content=protect))
    else:
        await with_retry(lambda: method(chat_id, **{arg_name: file_id}, caption=final_caption,
                                        protect_content=protect))


async def send_content_by_tag(chat_id: int, tag: str, course_filter_value: int = None, user_group: str = None):
    """Отправляет контент по тегу. Курс-фильтр применяется только к не-админам."""
    logger.info(f"Sending #{tag} to {chat_id}, course={course_filter_value}, group={user_group}")
    admin = is_admin(chat_id)
    materials = await db.get_materials_by_tag(tag)

    if not materials:
        await bot.send_message(chat_id, "❌ Материалы не найдены.")
        return

    filtered = []
    for m in materials:
        course_ok = admin or m['course'] is None or m['course'] == course_filter_value
        group_ok = m['group_lang'] is None or m['group_lang'] == user_group
        if course_ok and group_ok:
            filtered.append(m)

    if not filtered:
        await bot.send_message(chat_id, f"❌ Нет доступных материалов по тегу <b>#{tag}</b> для вашего курса и группы.")
        return

    await bot.send_message(chat_id, f"📦 <b>Материалы по запросу ({len(filtered)} шт.):</b>")

    failed = 0
    for material in filtered:
        try:
            await send_material(chat_id, material, tag)
        except Exception as e:
            failed += 1
            logger.error(f"Ошибка отправки {material['type']} (id={material['id']}) для #{tag}: {e}")
        await asyncio.sleep(0.05)

    if failed:
        await bot.send_message(chat_id, f"⚠️ Не удалось отправить файлов: {failed}.")
    await bot.send_message(chat_id, "✅ Все доступные материалы отправлены.")


# ==================== 10. ХЕНДЛЕРЫ ====================
# ВАЖНО: порядок регистрации имеет значение — навигация и меню идут РАНЬШЕ обработчиков состояний,
# чтобы нажатие кнопки меню всегда имело приоритет над «ожиданием контента».

# -------------------- /start и /cancel --------------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    logger.info(f"User {user_id} started the bot")
    await state.clear()
    user_data = await db.get_user(user_id)

    if is_admin(user_id):
        if not user_data:
            await db.save_user(user_id, message.from_user.username or 'NoUsername',
                               message.from_user.full_name, max(COURSES), "ru")
            user_course = max(COURSES)
        else:
            user_course = user_data['course']
        await message.answer(
            "👑 <b>Админ-меню.</b>\n💡 Для справки используйте /admin_help",
            reply_markup=get_reply_main_menu_keyboard(user_course, user_id))
        return

    if user_data:
        await message.answer(
            f"✅ С возвращением! Вы учитесь на {user_data['course']}-м курсе.\nВыберите раздел:",
            reply_markup=get_reply_main_menu_keyboard(user_data['course'], user_id))
    else:
        await message.answer(
            "🎓 <b>Добро пожаловать!</b>\n\nДля персонализации материалов, пожалуйста, укажите ваш курс:",
            reply_markup=get_course_selection_keyboard())
        await state.set_state(RegistrationStates.waiting_for_course)


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    user_data = await db.get_user(message.from_user.id)
    if user_data:
        await message.answer("Действие отменено. Вы в главном меню.",
                             reply_markup=get_reply_main_menu_keyboard(user_data['course'], message.from_user.id))
    else:
        await message.answer("Действие отменено. Для начала работы отправьте /start",
                             reply_markup=types.ReplyKeyboardRemove())


# -------------------- Регистрация --------------------
@dp.message(RegistrationStates.waiting_for_course)
async def process_course_selection(message: types.Message, state: FSMContext):
    if not message.text:
        return await message.answer("Пожалуйста, выберите курс, используя кнопки ниже:")
    try:
        course = int(message.text.split("-")[0])
        if course not in COURSES:
            raise ValueError
    except ValueError:
        return await message.answer("Пожалуйста, выберите корректный курс, используя кнопки:")

    await state.update_data(course=course)
    await message.answer("📝 Теперь выберите вашу группу:", reply_markup=get_group_selection_keyboard())
    await state.set_state(RegistrationStates.waiting_for_group)


@dp.message(RegistrationStates.waiting_for_group)
async def process_group_selection(message: types.Message, state: FSMContext):
    group_lang = next((k for k, v in GROUPS.items() if message.text == v), None)
    if not group_lang:
        return await message.answer("Пожалуйста, выберите группу, используя кнопки ниже:")

    data = await state.get_data()
    course = data.get('course')
    if not course:
        await state.set_state(RegistrationStates.waiting_for_course)
        return await message.answer("Начнём сначала. Укажите ваш курс:", reply_markup=get_course_selection_keyboard())

    await state.update_data(group_lang=group_lang)
    await message.answer(
        f"⚠️ <b>Прежде чем сделать выбор — важное условие!</b>\n\n"
        f"Вы выбрали:\n"
        f"• Курс: <b>{course}-й</b>\n"
        f"• Группа: <b>{GROUPS[group_lang]}</b>\n\n"
        f"Ваше решение будет <b>окончательным</b>. После подтверждения курс <b>нельзя будет изменить</b>.\n\n"
        f"Пожалуйста, подойдите к выбору осознанно.",
        reply_markup=get_confirmation_keyboard())
    await state.set_state(RegistrationStates.waiting_for_confirmation)


@dp.message(RegistrationStates.waiting_for_confirmation)
async def process_confirmation(message: types.Message, state: FSMContext):
    user_id = message.from_user.id

    if message.text == "✅ Подтвердить выбор":
        data = await state.get_data()
        course, group_lang = data.get('course'), data.get('group_lang')
        if not course or not group_lang:
            await state.set_state(RegistrationStates.waiting_for_course)
            return await message.answer("Данные утеряны. Укажите ваш курс:",
                                        reply_markup=get_course_selection_keyboard())

        await db.save_user(user_id, message.from_user.username or 'NoUsername',
                           message.from_user.full_name, course, group_lang)
        await message.answer(
            "✅ <b>Регистрация завершена!</b>\n\nТеперь вам доступны материалы для вашего курса.",
            reply_markup=get_reply_main_menu_keyboard(course, user_id))
        await state.clear()

    elif message.text == "🔙 Назад к выбору курса":
        await state.clear()
        await message.answer("Начнем сначала. Пожалуйста, укажите ваш курс:",
                             reply_markup=get_course_selection_keyboard())
        await state.set_state(RegistrationStates.waiting_for_course)
    else:
        await message.answer("Пожалуйста, используйте кнопки для подтверждения или отмены.")


# -------------------- Проверка подписки (кнопка «Проверить») --------------------
@dp.callback_query(F.data == "check_subscription")
async def check_subscription_callback_handler(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id

    if await db.is_user_blocked(user_id):
        return await callback.answer("🚫 Ваш аккаунт заблокирован.", show_alert=True)

    if not await check_subscription(user_id, force=True):
        return await callback.answer("❌ Подписка не найдена. Попробуйте снова.", show_alert=True)

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await callback.answer("✅ Подписка подтверждена!", show_alert=False)

    user_data = await db.get_user(user_id)
    if user_data:
        await callback.message.answer(
            f"✅ С возвращением! Вы учитесь на {user_data['course']}-м курсе.\nВыберите раздел:",
            reply_markup=get_reply_main_menu_keyboard(user_data['course'], user_id))
    else:
        await callback.message.answer(
            "🎓 <b>Добро пожаловать!</b>\n\nДля персонализации материалов, пожалуйста, укажите ваш курс:",
            reply_markup=get_course_selection_keyboard())
        await state.set_state(RegistrationStates.waiting_for_course)


# -------------------- Навигация --------------------
@dp.message(F.text == "🔙 Главное меню")
async def back_to_main_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Вы вернулись в главное меню.", reply_markup=await main_menu_kb(message.from_user.id))


@dp.message(F.text == "🔙 К выбору курса")
async def back_to_exam_courses(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return await back_to_main_menu(message, state)
    user_data = await db.get_user(message.from_user.id)
    limit = user_data['course'] if user_data else max(COURSES)
    await state.update_data(exam_course=None)
    await state.set_state(AdminStates.waiting_for_exam_course)
    await message.answer("Выберите курс:", reply_markup=get_reply_exam_course_keyboard(limit))


@dp.message(F.text == "🔙 К типам материалов")
async def back_to_material_types(message: types.Message, state: FSMContext):
    if is_admin(message.from_user.id):
        data = await state.get_data()
        if data.get("admin_group_filter"):
            await state.update_data(material_type=None, materials_course=None)
            await state.set_state(AdminStates.waiting_for_materials_type)
            return await message.answer("Выберите тип материала:", reply_markup=get_reply_materials_menu_keyboard())
    await materials_menu(message, state)


@dp.message(F.text == "🔙 К выбору курса (Материалы)")
async def back_to_materials_course_admin(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return await back_to_main_menu(message, state)
    user_data = await db.get_user(message.from_user.id)
    limit = user_data['course'] if user_data else max(COURSES)
    data = await state.get_data()
    type_name = MATERIAL_TYPES.get(data.get('material_type'), "—")
    await state.update_data(materials_course=None)
    await state.set_state(AdminStates.waiting_for_materials_course)
    await message.answer(f"✅ Выбран тип: <b>{type_name}</b>.\n\nТеперь выберите курс:",
                         reply_markup=get_reply_materials_course_keyboard(limit))


# -------------------- Раздел «Экзамен» --------------------
@dp.message(F.text == "📚 Экзамен")
async def exam_menu(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user_data = await db.get_user(user_id)
    if not user_data:
        return await message.answer("Пожалуйста, завершите регистрацию через /start")

    await state.clear()
    user_course = user_data['course']

    if not is_admin(user_id):
        await state.update_data(exam_course=user_course)
        await message.answer(
            f"Теперь выберите предмет <b>{user_course}-го курса</b>:",
            reply_markup=get_reply_exam_subject_keyboard(user_course, user_id))
        await state.set_state(ExamStates.waiting_for_subject)
        return

    await state.update_data(target_section="exam")
    await message.answer("👑 <b>Админ-меню Экзамен:</b> Выберите язык материалов:",
                         reply_markup=get_group_selection_keyboard())
    await state.set_state(AdminStates.waiting_for_admin_group)


@dp.message(AdminStates.waiting_for_exam_course)
async def process_exam_course_selection(message: types.Message, state: FSMContext):
    match = re.match(r"^(\d+)-й курс$", message.text or "")
    if not match or int(match.group(1)) not in COURSES:
        return await message.answer("Пожалуйста, выберите курс, используя кнопки.")

    course_num = int(match.group(1))
    await state.update_data(exam_course=course_num)
    await message.answer(
        f"✅ Выбран <b>{course_num}-й курс</b>. Теперь выберите предмет:",
        reply_markup=get_reply_exam_subject_keyboard(course_num, message.from_user.id))
    await state.set_state(ExamStates.waiting_for_subject)


@dp.message(ExamStates.waiting_for_subject, F.text.in_(ALL_SUBJECT_NAMES))
async def exam_subject_handler(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user_data = await db.get_user(user_id)
    if not user_data:
        return await message.answer("Пожалуйста, завершите регистрацию через /start")

    admin = is_admin(user_id)
    data = await state.get_data()
    exam_course = data.get('exam_course')

    if not exam_course:
        await state.clear()
        return await message.answer("⚠️ Курс не выбран. Начните с «📚 Экзамен».",
                                    reply_markup=get_reply_main_menu_keyboard(user_data['course'], user_id))

    if not admin and exam_course > user_data['course']:
        return await message.answer(f"❌ У вас есть доступ только к материалам {user_data['course']}-го курса и ниже.")

    subject_tag = EXAM_NAME_TO_TAG.get(exam_course, {}).get(message.text)
    if not subject_tag:
        return await message.answer(f"Предмет не соответствует {exam_course}-му курсу.")

    course_filter = exam_course if admin else user_data['course']
    group_filter = (data.get('admin_group_filter') if admin else None) or user_data['group_lang']

    await send_content_by_tag(message.chat.id, subject_tag, course_filter, group_filter)

    if admin:
        await message.answer(f"<b>АДМИН-ПАНЕЛЬ:</b> Загрузка для <b>#{subject_tag}</b>",
                             reply_markup=get_admin_menu(subject_tag))

    await message.answer(
        f"Курс <b>{exam_course}-й</b> - Материалы отправлены. Выберите следующий предмет:",
        reply_markup=get_reply_exam_subject_keyboard(exam_course, user_id))
    await state.set_state(ExamStates.waiting_for_subject)


# -------------------- Раздел «Итог» --------------------
@dp.message(F.text == "📋 Итог")
async def summary_menu(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user_data = await db.get_user(user_id)
    if not user_data:
        return await message.answer("Пожалуйста, завершите регистрацию через /start")

    await state.clear()
    user_course = user_data['course']

    if not is_admin(user_id):
        if user_course > MAX_COURSE_FOR_SUMMARY:
            return await message.answer(
                f"Раздел «Итог» доступен только для 1–{MAX_COURSE_FOR_SUMMARY} курсов.",
                reply_markup=get_reply_main_menu_keyboard(user_course, user_id))
        return await message.answer(
            f"✅ Выбран <b>{user_course}-й курс</b>. Теперь выберите раздел итогов (1-{SUMMARY_SECTIONS}):",
            reply_markup=get_reply_final_summary_keyboard(user_course))

    await state.update_data(target_section="summary")
    await message.answer("👑 <b>Админ-меню Итог:</b> Выберите язык материалов:",
                         reply_markup=get_group_selection_keyboard())
    await state.set_state(AdminStates.waiting_for_admin_group)


@dp.message(F.text.regexp(r"^Итог - \d+ курс$"))
async def process_summary_course_selection(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user_data = await db.get_user(user_id)
    if not user_data:
        return await message.answer("Пожалуйста, завершите регистрацию через /start")

    course_num = int(re.search(r"\d+", message.text).group())
    if not is_admin(user_id) and (course_num > user_data['course'] or course_num > MAX_COURSE_FOR_SUMMARY):
        return await message.answer("❌ У вас нет доступа к этому курсу итоговых материалов.")
    if course_num > MAX_COURSE_FOR_SUMMARY:
        return await message.answer(f"❌ Доступны только курсы 1-{MAX_COURSE_FOR_SUMMARY} для раздела Итог.")

    if await state.get_state() == AdminStates.waiting_for_summary_course.state:
        await state.set_state(None)  # данные (admin_group_filter) сохраняются

    await message.answer(
        f"✅ Выбран <b>{course_num}-й курс</b>. Теперь выберите раздел итогов (1-{SUMMARY_SECTIONS}):",
        reply_markup=get_reply_final_summary_keyboard(course_num))


@dp.message(F.text.regexp(r"^Итог \d+\.\d+$"))
async def final_summary_handler(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user_data = await db.get_user(user_id)
    if not user_data:
        return await message.answer("Пожалуйста, завершите регистрацию через /start")

    admin = is_admin(user_id)
    course, section = (int(x) for x in message.text.split(" ")[1].split("."))

    if course > MAX_COURSE_FOR_SUMMARY or not 1 <= section <= SUMMARY_SECTIONS:
        return await message.answer("❌ Этот раздел итогов не существует или неактивен.")
    if not admin and course > user_data['course']:
        return await message.answer("❌ У вас нет доступа к этому курсу итоговых материалов.")

    data = await state.get_data()
    final_tag = f"summary{course}.{section}"
    group_filter = (data.get('admin_group_filter') if admin else None) or user_data['group_lang']

    await send_content_by_tag(message.chat.id, final_tag, user_data['course'], group_filter)

    if admin:
        await message.answer(f"<b>АДМИН-ПАНЕЛЬ:</b> Загрузка для <b>#{final_tag}</b>",
                             reply_markup=get_admin_menu(final_tag))

    await message.answer("Материалы отправлены. Выберите следующий раздел:",
                         reply_markup=get_reply_final_summary_keyboard(course))


# -------------------- Личный кабинет --------------------
@dp.message(F.text == "🚪 Личный кабинет")
async def personal_account(message: types.Message):
    user_data = await db.get_user(message.from_user.id)
    if not user_data:
        return await message.answer("Пожалуйста, завершите регистрацию через /start")

    account_info = (
        f"👤 <b>Личный кабинет</b>\n\n"
        f"• <b>Имя:</b> {html.escape(user_data['full_name'] or '—')}\n"
        f"• <b>Курс:</b> {user_data['course']}-й\n"
        f"• <b>Группа:</b> {GROUPS.get(user_data['group_lang'], user_data['group_lang'])}\n"
        f"• <b>Зарегистрирован:</b> {str(user_data['registered_at'])[:10]}\n"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="Открыть study.tj", web_app=WebAppInfo(url=WEBAPP_URL))
    await message.answer(account_info, reply_markup=builder.as_markup())


# -------------------- Раздел «Материалы» --------------------
@dp.message(F.text == "📂 Материалы")
async def materials_menu(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user_data = await db.get_user(user_id)
    if not user_data:
        return await message.answer("Пожалуйста, завершите регистрацию через /start")

    await state.clear()

    if is_admin(user_id):
        await state.update_data(target_section="materials")
        await message.answer("👑 <b>Админ-меню Материалы:</b> Выберите язык материалов:",
                             reply_markup=get_group_selection_keyboard())
        await state.set_state(AdminStates.waiting_for_admin_group)
        return

    await message.answer("Выберите тип материалов:", reply_markup=get_reply_materials_menu_keyboard())
    await state.set_state(MaterialsStates.waiting_for_material_type)


@dp.message(MaterialsStates.waiting_for_material_type, F.text.in_(list(MATERIAL_TYPES.values())))
async def handle_material_type_selection(message: types.Message, state: FSMContext):
    user_data = await db.get_user(message.from_user.id)
    if not user_data:
        return await message.answer("Пожалуйста, завершите регистрацию через /start")

    user_course = user_data['course']
    type_key = next((k for k, v in MATERIAL_TYPES.items() if v == message.text), None)
    if not type_key:
        return

    await state.update_data(material_type=type_key, materials_course=user_course)
    await message.answer(
        f"✅ Выбрано: <b>{message.text}</b>.\nТеперь выберите предмет для <b>{user_course}</b>-го курса:",
        reply_markup=get_reply_materials_subject_keyboard(user_course, message.from_user.id))
    await state.set_state(MaterialsStates.waiting_for_subject)


@dp.message(AdminStates.waiting_for_materials_type, F.text.in_(list(MATERIAL_TYPES.values())))
async def admin_select_material_type(message: types.Message, state: FSMContext):
    user_data = await db.get_user(message.from_user.id)
    limit = user_data['course'] if user_data else max(COURSES)
    type_key = next((k for k, v in MATERIAL_TYPES.items() if v == message.text), None)
    if not type_key:
        return

    await state.update_data(material_type=type_key)
    await message.answer(f"✅ Выбран тип: <b>{message.text}</b>.\n\nТеперь выберите курс:",
                         reply_markup=get_reply_materials_course_keyboard(limit))
    await state.set_state(AdminStates.waiting_for_materials_course)


@dp.message(AdminStates.waiting_for_materials_course, F.text.regexp(r"^\d+-й курс$"))
async def admin_select_course_for_materials(message: types.Message, state: FSMContext):
    data = await state.get_data()
    material_type = data.get('material_type')
    course_num = int(message.text.split("-")[0])

    if not material_type or course_num not in COURSES:
        await state.clear()
        return await message.answer(
            "⚠️ Ошибка состояния. Пожалуйста, начните сначала.",
            reply_markup=await main_menu_kb(message.from_user.id))

    await state.update_data(materials_course=course_num)
    await message.answer(f"✅ Выбран <b>{course_num}-й курс</b>. Теперь выберите предмет:",
                         reply_markup=get_reply_materials_subject_keyboard(course_num, message.from_user.id))
    await state.set_state(MaterialsStates.waiting_for_subject)


@dp.message(MaterialsStates.waiting_for_subject, F.text.in_(ALL_SUBJECT_NAMES))
async def handle_materials_subject_selection(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user_data = await db.get_user(user_id)
    if not user_data:
        return await message.answer("Пожалуйста, завершите регистрацию через /start")

    admin = is_admin(user_id)
    data = await state.get_data()
    material_type = data.get('material_type')
    materials_course = data.get('materials_course')

    if not material_type or not materials_course:
        await state.clear()
        return await message.answer("⚠️ Ошибка состояния. Начните с «📂 Материалы».",
                                    reply_markup=get_reply_main_menu_keyboard(user_data['course'], user_id))

    if not admin and materials_course > user_data['course']:
        return await message.answer("❌ У вас нет доступа к материалам этого курса.")

    subject_tag = MATERIALS_NAME_TO_TAG.get(materials_course, {}).get(message.text)
    if not subject_tag:
        return await message.answer(f"Предмет не соответствует {materials_course}-му курсу.")

    final_tag = f"{material_type}_{subject_tag}"
    course_filter = materials_course if admin else user_data['course']
    group_filter = (data.get('admin_group_filter') if admin else None) or user_data['group_lang']

    await send_content_by_tag(message.chat.id, final_tag, course_filter, group_filter)

    if admin:
        await message.answer(f"<b>АДМИН-ПАНЕЛЬ:</b> Загрузка для <b>#{final_tag}</b>",
                             reply_markup=get_admin_menu(final_tag))

    type_name = MATERIAL_TYPES.get(material_type, "Материалы")
    await message.answer(
        f"{type_name} - Курс <b>{materials_course}-й</b> - Материалы отправлены.\n"
        f"Выберите следующий предмет или вернитесь назад.",
        reply_markup=get_reply_materials_subject_keyboard(materials_course, user_id))
    await state.set_state(MaterialsStates.waiting_for_subject)


# -------------------- Выбор языка для админа (общий для Экзамен / Итог / Материалы) --------------------
@dp.message(AdminStates.waiting_for_admin_group)
async def process_admin_group_selection(message: types.Message, state: FSMContext):
    data = await state.get_data()
    target_section = data.get('target_section')

    group_lang = next((k for k, v in GROUPS.items() if message.text == v), None)
    if not group_lang:
        return await message.answer("Пожалуйста, выберите группу, используя кнопки ниже.")

    await state.update_data(admin_group_filter=group_lang)
    user_id = message.from_user.id
    user_data = await db.get_user(user_id)
    limit = user_data['course'] if user_data else max(COURSES)

    if target_section == "exam":
        await message.answer(f"✅ Выбран язык: {GROUPS[group_lang]}. Выберите курс:",
                             reply_markup=get_reply_exam_course_keyboard(limit))
        await state.set_state(AdminStates.waiting_for_exam_course)

    elif target_section == "summary":
        await message.answer(f"✅ Выбран язык: {GROUPS[group_lang]}. Выберите курс для итога:",
                             reply_markup=get_reply_summary_course_keyboard(limit, user_id))
        await state.set_state(AdminStates.waiting_for_summary_course)

    elif target_section == "materials":
        await message.answer(f"✅ Выбран язык: {GROUPS[group_lang]}. Выберите тип материала:",
                             reply_markup=get_reply_materials_menu_keyboard())
        await state.set_state(AdminStates.waiting_for_materials_type)

    else:
        await state.clear()
        await message.answer("Ошибка секции. Возврат в главное меню.",
                             reply_markup=get_reply_main_menu_keyboard(limit, user_id))


# -------------------- РАССЫЛКА --------------------
def _broadcast_group_kb():
    builder = ReplyKeyboardBuilder()
    for value in GROUPS.values():
        builder.button(text=value)
    builder.button(text="ВСЕМ ГРУППАМ")
    builder.adjust(3)
    return builder.as_markup(resize_keyboard=True)


@dp.message(Command("broadcast"), ADMIN_ONLY)
async def cmd_start_broadcast(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("👑 <b>РАССЫЛКА:</b> Выберите целевую группу:", reply_markup=_broadcast_group_kb())
    await state.set_state(AdminStates.waiting_for_broadcast_filter)


@dp.message(F.text == "📢 Рассылка", ADMIN_ONLY)
async def admin_broadcast_button(message: types.Message, state: FSMContext):
    await cmd_start_broadcast(message, state)


@dp.message(AdminStates.waiting_for_broadcast_filter)
async def process_broadcast_group_filter(message: types.Message, state: FSMContext):
    text = message.text or ""
    if text == "ВСЕМ ГРУППАМ":
        group_key = 'all'
    else:
        group_key = next((k for k, v in GROUPS.items() if v == text), None)
    if not group_key:
        return await message.answer("Пожалуйста, выберите группу из предложенных кнопок.")

    await state.update_data(broadcast_group=group_key)

    builder = ReplyKeyboardBuilder()
    for course in COURSES:
        builder.button(text=f"Курс {course}")
    builder.button(text="ВСЕМ КУРСАМ")
    builder.adjust(3)
    await message.answer(f"✅ Группа выбрана ({text}). Теперь выберите курс:",
                         reply_markup=builder.as_markup(resize_keyboard=True))
    await state.set_state(AdminStates.waiting_for_broadcast_course)


@dp.message(AdminStates.waiting_for_broadcast_course)
async def process_broadcast_course_filter(message: types.Message, state: FSMContext):
    text = message.text or ""
    course_num = 0  # 0 = все курсы
    if text != "ВСЕМ КУРСАМ":
        match = re.match(r"^Курс (\d+)$", text)
        if not match or int(match.group(1)) not in COURSES:
            return await message.answer("Пожалуйста, выберите курс из предложенных кнопок.")
        course_num = int(match.group(1))

    await state.update_data(broadcast_course=course_num)
    await message.answer(
        "📝 <b>Отправьте сообщение для рассылки</b> (текст, фото, видео, файл…).\n\n"
        "<i>Форматирование Telegram (жирный, ссылки и т.д.) сохранится — сообщение копируется как есть.</i>\n"
        "Для отмены — /cancel.",
        reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(AdminStates.waiting_for_broadcast_content)


@dp.message(AdminStates.waiting_for_broadcast_content,
            F.text | F.photo | F.document | F.video | F.audio | F.voice | F.animation)
async def process_broadcast_content(message: types.Message, state: FSMContext):
    if is_command(message):
        return await message.answer("Это команда. Отправьте сообщение для рассылки или /cancel.")

    data = await state.get_data()
    course = data.get('broadcast_course', 0)
    group_lang = data.get('broadcast_group', 'all')

    user_ids = await db.get_users_for_broadcast(course, group_lang)
    if not user_ids:
        await state.clear()
        return await message.answer(
            f"❌ Не найдено пользователей по фильтрам (Курс: {course or 'Все'}, "
            f"Группа: {GROUPS.get(group_lang, 'Все')}).",
            reply_markup=await main_menu_kb(message.from_user.id))

    await state.update_data(bc_chat_id=message.chat.id, bc_message_id=message.message_id)

    await message.answer("👁 <b>Предпросмотр рассылки:</b>")
    await message.copy_to(message.chat.id)

    builder = InlineKeyboardBuilder()
    builder.button(text=f"✅ Отправить ({len(user_ids)})", callback_data="bc_send")
    builder.button(text="❌ Отмена", callback_data="bc_cancel")
    builder.adjust(2)
    await message.answer(
        f"Получателей: <b>{len(user_ids)}</b>\n"
        f"Фильтры: курс — {course or 'все'}, группа — {GROUPS.get(group_lang, 'все')}.\n\n"
        f"Отправить это сообщение?",
        reply_markup=builder.as_markup())
    await state.set_state(AdminStates.waiting_for_broadcast_confirm)


@dp.message(AdminStates.waiting_for_broadcast_content)
async def process_broadcast_unsupported(message: types.Message):
    await message.answer("⚠️ Неподдерживаемый тип контента. Отправьте текст, фото, видео, файл или аудио.")


async def _copy_with_retry(target_id: int, src_chat_id: int, src_message_id: int):
    await with_retry(lambda: bot.copy_message(chat_id=target_id, from_chat_id=src_chat_id,
                                              message_id=src_message_id))


async def run_broadcast(admin_id: int, src_chat_id: int, src_message_id: int, course: int, group_lang: str):
    global _broadcast_running
    _broadcast_running = True
    sent = blocked = failed = 0
    try:
        user_ids = await db.get_users_for_broadcast(course, group_lang)
        total = len(user_ids)
        await bot.send_message(admin_id, f"⏳ <b>Начинаю рассылку</b> ({total} получателей). Ожидайте...")

        for uid in user_ids:
            try:
                await _copy_with_retry(uid, src_chat_id, src_message_id)
                sent += 1
            except TelegramForbiddenError:
                blocked += 1
                await db.mark_inactive(uid)
            except Exception as e:
                failed += 1
                logger.warning(f"Рассылка: не удалось отправить {uid}: {e}")
            await asyncio.sleep(0.05)  # ~20 сообщений/сек — в пределах лимитов Telegram

        await db.log_admin_action(admin_id, "broadcast",
                                  f"course={course} group={group_lang} sent={sent} blocked={blocked} failed={failed}")
        await bot.send_message(
            admin_id,
            f"✅ <b>Рассылка завершена!</b>\n"
            f"Доставлено: <b>{sent}</b> из {total}\n"
            f"Бот заблокирован пользователями: {blocked}\n"
            f"Ошибки: {failed}\n"
            f"Фильтры: курс {course or 'все'}, группа {GROUPS.get(group_lang, 'все')}.")
    except Exception as e:
        logger.exception("Рассылка прервана")
        try:
            await bot.send_message(admin_id, f"❌ Рассылка прервана ошибкой: <code>{html.escape(str(e))[:300]}</code>\n"
                                             f"Отправлено до сбоя: {sent}")
        except Exception:
            pass
    finally:
        _broadcast_running = False


@dp.callback_query(F.data == "bc_send", ADMIN_ONLY, AdminStates.waiting_for_broadcast_confirm)
async def broadcast_confirm(callback: types.CallbackQuery, state: FSMContext):
    if _broadcast_running:
        return await callback.answer("⏳ Другая рассылка ещё выполняется.", show_alert=True)

    data = await state.get_data()
    src_chat_id, src_message_id = data.get('bc_chat_id'), data.get('bc_message_id')
    course, group_lang = data.get('broadcast_course', 0), data.get('broadcast_group', 'all')
    await state.clear()

    if not src_chat_id or not src_message_id:
        return await callback.answer("Данные рассылки утеряны. Начните заново.", show_alert=True)

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await callback.answer("Рассылка запущена")

    task = asyncio.create_task(run_broadcast(callback.from_user.id, src_chat_id, src_message_id, course, group_lang))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


@dp.callback_query(F.data == "bc_cancel", ADMIN_ONLY)
async def broadcast_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.edit_text("❌ Рассылка отменена.")
    except TelegramBadRequest:
        pass
    await callback.message.answer("Возврат в главное меню.", reply_markup=await main_menu_kb(callback.from_user.id))
    await callback.answer()


# -------------------- ЗАГРУЗКА МАТЕРИАЛОВ (админ) --------------------
@dp.callback_query(F.data.startswith("upload__"), ADMIN_ONLY)
async def start_upload_fsm(callback: types.CallbackQuery, state: FSMContext):
    target_tag = callback.data.split("__", 1)[1]
    if target_tag not in VALID_TAGS:
        return await callback.answer("❌ Неизвестный тег.", show_alert=True)

    data = await state.get_data()
    group_filter = data.get('admin_group_filter')

    await state.update_data(target_tag=target_tag, group_filter=group_filter, uploaded_count=0)
    await state.set_state(AdminStates.waiting_for_content)

    filter_info = f"только для группы {GROUPS.get(group_filter, 'N/A')}" if group_filter else "для ВСЕХ групп"
    await callback.message.answer(
        f"✅ Режим загрузки активирован для раздела <b>#{target_tag}</b> ({filter_info}).\n"
        f"Отправляйте файлы, фото, видео, аудио или текст — можно <b>несколько подряд</b>.\n"
        f"Когда закончите — /done. Для отмены — /cancel.")
    await callback.answer()


@dp.message(AdminStates.waiting_for_content, Command("done"))
async def finish_upload(message: types.Message, state: FSMContext):
    data = await state.get_data()
    count = data.get('uploaded_count', 0)
    tag = data.get('target_tag', '—')
    await state.clear()
    await message.answer(f"✅ Загрузка завершена. Сохранено в <b>#{tag}</b>: {count} шт.",
                         reply_markup=await main_menu_kb(message.from_user.id))


@dp.message(AdminStates.waiting_for_content)
async def process_content_upload(message: types.Message, state: FSMContext):
    user_id = message.from_user.id

    if is_command(message):
        return await message.answer("Это команда, а не материал. Отправьте файл/текст, /done — завершить, /cancel — отмена.")

    data = await state.get_data()
    target_tag = data.get('target_tag')
    group_filter = data.get('group_filter')

    if not target_tag:
        await state.clear()
        return await message.answer("Ошибка: не удалось определить целевой раздел. Попробуйте снова.")

    entry = {"type": "text", "file_id": None, "file_name": None,
             "caption": message.html_text if (message.text or message.caption) else None}

    if message.animation:
        entry.update(type="animation", file_id=message.animation.file_id,
                     file_name=message.animation.file_name or "GIF")
    elif message.photo:
        entry.update(type="photo", file_id=message.photo[-1].file_id, file_name="Изображение")
    elif message.video:
        entry.update(type="video", file_id=message.video.file_id,
                     file_name=message.video.file_name or "Видеозапись")
    elif message.document:
        entry.update(type="document", file_id=message.document.file_id, file_name=message.document.file_name)
    elif message.audio:
        entry.update(type="audio", file_id=message.audio.file_id,
                     file_name=message.audio.file_name or message.audio.title or "Аудио")
    elif message.voice:
        entry.update(type="voice", file_id=message.voice.file_id, file_name="Голосовое сообщение")
    elif not message.text:
        return await message.answer("⚠️ Неподдерживаемый тип контента (нужен файл, фото, видео, аудио или текст).")

    material_id = await db.save_material(
        tag=target_tag, type_=entry["type"], file_id=entry["file_id"], file_name=entry["file_name"],
        caption=entry["caption"], course=None, group_lang=group_filter, uploaded_by=user_id)
    await db.log_admin_action(user_id, "upload", f"tag={target_tag} id={material_id} type={entry['type']}")

    count = data.get('uploaded_count', 0) + 1
    await state.update_data(uploaded_count=count)

    filter_text = f" (для группы {GROUPS.get(group_filter, 'N/A')})" if group_filter else " (доступно всем)"
    await message.answer(
        f"✅ Сохранено в <b>#{target_tag}</b>{filter_text}. ID: {material_id}, тип: {entry['type']}.\n"
        f"За эту сессию: <b>{count}</b>. Отправьте ещё или /done.")


# -------------------- УДАЛЕНИЕ МАТЕРИАЛОВ (админ) --------------------
MAX_DELETE_LIST = 40


async def render_delete_list(tag: str) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    materials = await db.get_materials_by_tag(tag)
    if not materials:
        return f"❌ В разделе <b>#{tag}</b> нет материалов.", None

    shown = materials[:MAX_DELETE_LIST]
    text = f"🗑️ <b>УДАЛЕНИЕ:</b> раздел <b>#{tag}</b> — всего {len(materials)}\n\n"
    for m in shown:
        if m['type'] != "text" and m['file_name']:
            display = html.escape(str(m['file_name'])[:40])
        else:
            display = plain_preview(m['caption']) or "Текст"
        filters = []
        if m['course']:
            filters.append(f"к{m['course']}")
        if m['group_lang']:
            filters.append(f"г{m['group_lang']}")
        flt = f" [{', '.join(filters)}]" if filters else " [всем]"
        text += f"<b>ID {m['id']}:</b> <code>{m['type'].upper()}</code> {display}{flt}\n"
    if len(materials) > len(shown):
        text += f"\n… и ещё {len(materials) - len(shown)} (удалите часть, чтобы увидеть остальные)\n"
    text += "\nНажмите кнопку с ID, чтобы удалить материал."

    buttons = [InlineKeyboardButton(text=f"🗑 {m['id']}", callback_data=f"delmat__{m['id']}__{tag}") for m in shown]
    rows = _inline_rows(buttons, 4)
    rows.append([InlineKeyboardButton(text="🗑 Удалить ВСЕ", callback_data=f"delall__{tag}")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data.startswith("delete_indexed__"), ADMIN_ONLY)
async def start_indexed_delete(callback: types.CallbackQuery):
    tag = callback.data.split("__", 1)[1]
    if tag not in VALID_TAGS:
        return await callback.answer("❌ Неизвестный тег.", show_alert=True)
    text, markup = await render_delete_list(tag)
    await callback.message.answer(text, reply_markup=markup)
    await callback.answer()


@dp.callback_query(F.data.startswith("delmat__"), ADMIN_ONLY)
async def delete_single_material(callback: types.CallbackQuery):
    _, mat_id, tag = callback.data.split("__", 2)
    deleted = await db.delete_material(int(mat_id))
    if deleted:
        await db.log_admin_action(callback.from_user.id, "delete_material", f"tag={tag} id={mat_id}")
    await callback.answer("✅ Удалено" if deleted else "Материал уже удалён")
    text, markup = await render_delete_list(tag)
    await safe_edit_text(callback.message, text, markup)


@dp.callback_query(F.data.startswith("delall__"), ADMIN_ONLY)
async def delete_all_ask(callback: types.CallbackQuery):
    tag = callback.data.split("__", 1)[1]
    count = len(await db.get_materials_by_tag(tag))
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, удалить всё", callback_data=f"delall_yes__{tag}")
    builder.button(text="↩️ Отмена", callback_data=f"delall_no__{tag}")
    builder.adjust(1)
    await safe_edit_text(
        callback.message,
        f"⚠️ <b>Удалить ВСЕ материалы ({count} шт.) из раздела #{tag}?</b>\nЭто действие необратимо.",
        builder.as_markup())
    await callback.answer()


@dp.callback_query(F.data.startswith("delall_yes__"), ADMIN_ONLY)
async def delete_all_confirm(callback: types.CallbackQuery):
    tag = callback.data.split("__", 1)[1]
    deleted = await db.delete_materials_by_tag(tag)
    await db.log_admin_action(callback.from_user.id, "delete_all", f"tag={tag} count={deleted}")
    await safe_edit_text(callback.message, f"✅ Удалено материалов из <b>#{tag}</b>: {deleted}.")
    await callback.answer()


@dp.callback_query(F.data.startswith("delall_no__"), ADMIN_ONLY)
async def delete_all_cancel(callback: types.CallbackQuery):
    tag = callback.data.split("__", 1)[1]
    text, markup = await render_delete_list(tag)
    await safe_edit_text(callback.message, text, markup)
    await callback.answer("Отменено")


# -------------------- АДМИН: МЕНЮ ТЕГОВ (раздел -> курс -> тег) --------------------
@dp.message(Command("admin_menu"), ADMIN_ONLY)
async def cmd_admin_menu(message: types.Message):
    await message.answer("🛠️ <b>АДМИН-ПАНЕЛЬ:</b> Выберите раздел.", reply_markup=kb_admin_sections())


@dp.callback_query(F.data == "admmenu_root", ADMIN_ONLY)
async def admin_menu_root(callback: types.CallbackQuery):
    await safe_edit_text(callback.message, "🛠️ <b>АДМИН-ПАНЕЛЬ:</b> Выберите раздел.", kb_admin_sections())
    await callback.answer()


@dp.callback_query(F.data.startswith("admsec__"), ADMIN_ONLY)
async def admin_menu_section(callback: types.CallbackQuery):
    section = callback.data.split("__", 1)[1]
    if section not in ("exam", "mat", "sum"):
        return await callback.answer()
    await safe_edit_text(callback.message, "🛠️ <b>АДМИН-ПАНЕЛЬ:</b> Выберите курс.", kb_admin_courses(section))
    await callback.answer()


@dp.callback_query(F.data.startswith("admcourse__"), ADMIN_ONLY)
async def admin_menu_course(callback: types.CallbackQuery):
    _, section, course = callback.data.split("__")
    if section not in ("exam", "mat", "sum") or not course.isdigit():
        return await callback.answer()
    await safe_edit_text(callback.message, "🛠️ <b>АДМИН-ПАНЕЛЬ:</b> Выберите раздел (тег).",
                         kb_admin_tags(section, int(course)))
    await callback.answer()


@dp.callback_query(F.data.startswith("select_admin_tag__"), ADMIN_ONLY)
async def show_admin_menu_for_tag(callback: types.CallbackQuery):
    tag = callback.data.split("__", 1)[1]
    if tag not in VALID_TAGS:
        return await callback.answer("❌ Неизвестный тег.", show_alert=True)
    await safe_edit_text(callback.message, f"<b>АДМИН-ПАНЕЛЬ:</b> Действия для раздела <b>#{tag}</b>",
                         get_admin_menu(tag))
    await callback.answer()


# -------------------- АДМИН: БЛОКИРОВКА --------------------
async def send_blocklist(chat_id: int):
    blocked_users = await db.get_blocked_users()
    if not blocked_users:
        return await bot.send_message(
            chat_id,
            "📋 Список заблокированных пользователей пуст.\n\n"
            "💡 <b>Команды:</b>\n"
            "<code>/block USER_ID [причина]</code> - Заблокировать\n"
            "<code>/unblock USER_ID</code> - Разблокировать")

    response = "🚫 <b>ЗАБЛОКИРОВАННЫЕ ПОЛЬЗОВАТЕЛИ</b>\n\n"
    rows = []
    for i, u in enumerate(blocked_users, 1):
        username = f"@{html.escape(u['username'])}" if u['username'] else "—"
        full_name = html.escape(u['full_name'] or "Неизвестно")
        course = f"{u['course']}-й курс" if u['course'] else "—"
        group = GROUPS.get(u['group_lang'], "—")
        response += (
            f"<b>{i}.</b> {full_name}\n"
            f"   📱 {username}\n"
            f"   🆔 <code>{u['user_id']}</code>\n"
            f"   📚 {course} | 🌐 {group}\n"
            f"   📅 {str(u['blocked_at'])[:16]}\n"
            f"   📝 {html.escape(u['reason'] or 'Не указана')}\n\n"
        )
        rows.append([InlineKeyboardButton(text=f"✅ Разблокировать {(u['full_name'] or str(u['user_id']))[:15]}",
                                          callback_data=f"unblock_user__{u['user_id']}")])

    response += "💡 <code>/block USER_ID [причина]</code> — заблокировать, или нажмите кнопку ниже ⬇️"
    await send_long(chat_id, response, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.message(Command("block"), ADMIN_ONLY)
async def cmd_block_user(message: types.Message):
    args = message.text.split(maxsplit=2)
    if len(args) < 2:
        return await message.answer(
            "📝 <b>Использование:</b>\n<code>/block USER_ID [причина]</code>\n\n"
            "<b>Пример:</b>\n<code>/block 123456789 Спам</code>")
    try:
        target_id = int(args[1])
    except ValueError:
        return await message.answer("❌ Неверный формат ID. Введите числовой ID пользователя.")

    if target_id in ADMIN_IDS:
        return await message.answer("❌ Невозможно заблокировать администратора.")

    reason = args[2] if len(args) > 2 else None
    await db.block_user(target_id, message.from_user.id, reason)
    await db.log_admin_action(message.from_user.id, "block", f"user={target_id} reason={reason}")

    user_data = await db.get_user(target_id)
    if user_data:
        username = f"@{html.escape(user_data['username'])}" if user_data['username'] else "Нет username"
        user_info = (
            f"👤 <b>Имя:</b> {html.escape(user_data['full_name'] or 'Неизвестно')}\n"
            f"📱 <b>Username:</b> {username}\n"
            f"🆔 <b>ID:</b> <code>{target_id}</code>\n"
            f"📚 <b>Курс:</b> {user_data['course']}-й\n"
            f"🌐 <b>Группа:</b> {GROUPS.get(user_data['group_lang'], user_data['group_lang'])}")
    else:
        user_info = f"👤 <b>Имя:</b> Не зарегистрирован\n🆔 <b>ID:</b> <code>{target_id}</code>"

    await message.answer(f"🚫 <b>Пользователь заблокирован</b>\n\n{user_info}\n"
                         f"📝 Причина: {html.escape(reason or 'Не указана')}")


@dp.message(Command("unblock"), ADMIN_ONLY)
async def cmd_unblock_user(message: types.Message):
    args = message.text.split()
    if len(args) < 2:
        return await message.answer(
            "📝 <b>Использование:</b>\n<code>/unblock USER_ID</code>\n\n"
            "<b>Пример:</b>\n<code>/unblock 123456789</code>")
    try:
        target_id = int(args[1])
    except ValueError:
        return await message.answer("❌ Неверный формат ID. Введите числовой ID пользователя.")

    if await db.unblock_user(target_id) > 0:
        await db.log_admin_action(message.from_user.id, "unblock", f"user={target_id}")
        await message.answer(f"✅ <b>Пользователь разблокирован</b>\n\n🆔 <code>{target_id}</code>")
    else:
        await message.answer(f"ℹ️ Пользователь <code>{target_id}</code> не был в списке заблокированных.")


@dp.message(Command("blocklist"), ADMIN_ONLY)
async def cmd_blocklist(message: types.Message):
    await send_blocklist(message.chat.id)


@dp.message(F.text == "🚫 Заблокированные", ADMIN_ONLY)
async def admin_blocklist_button(message: types.Message):
    await send_blocklist(message.chat.id)


@dp.callback_query(F.data.startswith("unblock_user__"), ADMIN_ONLY)
async def callback_unblock_user(callback: types.CallbackQuery):
    target_id = int(callback.data.split("__")[1])
    if await db.unblock_user(target_id) > 0:
        await db.log_admin_action(callback.from_user.id, "unblock", f"user={target_id}")
        await callback.answer("✅ Пользователь разблокирован!", show_alert=True)
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass
        await send_blocklist(callback.message.chat.id)
    else:
        await callback.answer("ℹ️ Пользователь не был в списке заблокированных.", show_alert=True)


# -------------------- АДМИН: УПРАВЛЕНИЕ КУРСАМИ --------------------
@dp.message(Command("setcourse"), ADMIN_ONLY)
async def cmd_set_course(message: types.Message):
    args = message.text.split()
    usage = ("📝 <b>Использование:</b>\n<code>/setcourse USER_ID КУРС [ru|tj]</code>\n\n"
             "<b>Пример:</b>\n<code>/setcourse 123456789 4</code>")
    if len(args) < 3:
        return await message.answer(usage)
    try:
        target_id, course = int(args[1]), int(args[2])
    except ValueError:
        return await message.answer(usage)
    if course not in COURSES:
        return await message.answer(f"❌ Курс должен быть от {min(COURSES)} до {max(COURSES)}.")
    group_lang = args[3].lower() if len(args) > 3 else None
    if group_lang and group_lang not in GROUPS:
        return await message.answer("❌ Группа должна быть <code>ru</code> или <code>tj</code>.")

    if await db.set_user_course(target_id, course, group_lang) == 0:
        return await message.answer("❌ Пользователь не найден в базе.")

    await db.log_admin_action(message.from_user.id, "setcourse", f"user={target_id} course={course} group={group_lang}")
    await message.answer(f"✅ Пользователю <code>{target_id}</code> установлен {course}-й курс"
                         f"{' и группа ' + GROUPS[group_lang] if group_lang else ''}.")
    try:
        await bot.send_message(target_id, f"ℹ️ Администратор изменил ваш курс на <b>{course}-й</b>.\n"
                                          f"Нажмите /start, чтобы обновить меню.")
    except Exception:
        pass


@dp.message(Command("promote_all"), ADMIN_ONLY)
async def cmd_promote_all(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, перевести всех", callback_data="promote_yes")
    builder.button(text="❌ Отмена", callback_data="promote_no")
    builder.adjust(1)
    await message.answer(
        f"⚠️ <b>Перевести ВСЕХ пользователей на курс выше?</b>\n"
        f"Пользователи {max(COURSES)}-го курса останутся на месте. Действие нельзя отменить автоматически.",
        reply_markup=builder.as_markup())


@dp.callback_query(F.data == "promote_yes", ADMIN_ONLY)
async def promote_confirm(callback: types.CallbackQuery):
    count = await db.promote_all()
    await db.log_admin_action(callback.from_user.id, "promote_all", f"count={count}")
    await safe_edit_text(callback.message, f"✅ Переведено на курс выше: <b>{count}</b> пользователей.")
    await callback.answer()


@dp.callback_query(F.data == "promote_no", ADMIN_ONLY)
async def promote_cancel(callback: types.CallbackQuery):
    await safe_edit_text(callback.message, "❌ Перевод отменён.")
    await callback.answer()


# -------------------- АДМИН: СТАТИСТИКА И СПРАВКА --------------------
@dp.message(Command("admin_stats"), ADMIN_ONLY)
async def cmd_admin_stats(message: types.Message):
    s = await db.get_stats()
    text = "📊 <b>СТАТИСТИКА БОТА</b>\n\n"
    text += f"👥 <b>Пользователи:</b> {s['total_users']}\n"
    text += f"  • активны за 7 дней: {s['active_7d']}\n"
    text += f"  • активны за 30 дней: {s['active_30d']}\n"
    text += f"  • заблокировали бота: {s['inactive']}\n"
    text += f"  • в бане: {s['blocked']}\n\n"
    text += "🎓 <b>По курсам:</b>\n"
    for row in s['by_course']:
        text += f"  • {row['course']}-й курс: {row['cnt']}\n"
    text += "\n🌍 <b>По группам:</b>\n"
    for row in s['by_group']:
        text += f"  • {GROUPS.get(row['group_lang'], row['group_lang'])}: {row['cnt']}\n"
    text += f"\n📁 <b>Материалов всего:</b> {s['total_materials']}\n"
    if s['top_tags']:
        text += "\n🏆 <b>Больше всего материалов:</b>\n"
        for row in s['top_tags']:
            text += f"  • #{row['tag']}: {row['cnt']}\n"
    await send_long(message.chat.id, text)


@dp.message(F.text == "📊 Статистика", ADMIN_ONLY)
async def admin_stats_button(message: types.Message):
    await cmd_admin_stats(message)


@dp.message(Command("admin_materials"), ADMIN_ONLY)
async def cmd_admin_materials(message: types.Message):
    rows = await db.get_materials_summary()
    if not rows:
        return await message.answer("📭 В базе нет материалов.")

    grouped: Dict[str, List[str]] = {}
    for r in rows:
        flt = ""
        if r['course']:
            flt += f"к{r['course']}"
        if r['group_lang']:
            flt += f"г{r['group_lang']}"
        grouped.setdefault(r['tag'], []).append(f"{r['type']} [{flt or 'всем'}]: {r['cnt']} шт.")

    text = "📁 <b>МАТЕРИАЛЫ В БАЗЕ</b>\n"
    for tag in sorted(grouped):
        text += f"\n<b>#{tag.upper()}:</b>\n"
        for line in grouped[tag]:
            text += f"  • {line}\n"
    await send_long(message.chat.id, text)


@dp.message(Command("admin_help"), ADMIN_ONLY)
async def cmd_admin_help(message: types.Message):
    await message.answer("""🛠️ <b>АДМИН-КОМАНДЫ</b>

<b>📢 Рассылка:</b>
- Кнопка "📢 Рассылка" или <code>/broadcast</code> (с предпросмотром и подтверждением)

<b>📊 Статистика:</b>
- Кнопка "📊 Статистика" или <code>/admin_stats</code>
- <code>/admin_materials</code> - Материалы в базе

<b>🚫 Блокировка:</b>
- Кнопка "🚫 Заблокированные" или <code>/blocklist</code>
- <code>/block USER_ID [причина]</code> - Заблокировать
- <code>/unblock USER_ID</code> - Разблокировать

<b>🎓 Курсы:</b>
- <code>/setcourse USER_ID КУРС [ru|tj]</code> - Сменить курс пользователя
- <code>/promote_all</code> - Перевести всех на курс выше (раз в год)

<b>📁 Материалы:</b>
- <code>/admin_menu</code> - Меню разделов (раздел → курс → тег)
- Или через кнопки "📚 Экзамен", "📂 Материалы", "📋 Итог"
- В режиме загрузки можно отправлять много файлов подряд, завершить — <code>/done</code>

<b>💬 Обратная связь:</b>
- Ответ на сообщения пользователей — кнопкой под сообщением

<b>↩️ Общее:</b> <code>/cancel</code> - отмена текущего действия""")


# -------------------- ОБРАТНАЯ СВЯЗЬ --------------------
@dp.message(F.text == "✉️ Обратная связь")
async def feedback_handler(message: types.Message, state: FSMContext):
    await message.answer(
        "📝 <b>Обратная связь</b>\n\n"
        "Отправьте ваше сообщение, вопрос, предложение или жалобу.\n"
        "Можно отправить текст, фото, документ, видео, аудио или голосовое.\n\n"
        "Для отмены используйте /cancel",
        reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(FeedbackStates.waiting_for_feedback_message)


@dp.message(FeedbackStates.waiting_for_feedback_message, F.text | F.photo | F.document | F.video | F.audio | F.voice)
async def process_feedback_message(message: types.Message, state: FSMContext):
    user_id = message.from_user.id

    if is_command(message):
        return await message.answer("Это команда. Отправьте обычное сообщение или /cancel для отмены.")

    now = time.monotonic()
    if not is_admin(user_id) and now - _feedback_last.get(user_id, -FEEDBACK_COOLDOWN - 1) < FEEDBACK_COOLDOWN:
        return await message.answer(f"⏳ Подождите {FEEDBACK_COOLDOWN} секунд между сообщениями.")
    _feedback_last[user_id] = now

    user_data = await db.get_user(user_id)
    username = message.from_user.username
    course = f"{user_data['course']}-й" if user_data else "Не указан"
    group = GROUPS.get(user_data['group_lang'], "Не указана") if user_data else "Не указана"

    user_info = (
        f"📩 <b>НОВОЕ СООБЩЕНИЕ ОТ ПОЛЬЗОВАТЕЛЯ</b>\n\n"
        f"👤 <b>Имя:</b> {html.escape(message.from_user.full_name)}\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
        f"📱 <b>Username:</b> {'@' + html.escape(username) if username else 'Нет username'}\n"
        f"📚 <b>Курс:</b> {course}\n"
        f"🌍 <b>Группа:</b> {group}"
    )

    reply_kb = InlineKeyboardBuilder()
    reply_kb.button(text="✉️ Ответить пользователю", callback_data=f"reply_to_user__{user_id}")

    success = 0
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, user_info)
            # copy_to (а не forward): работает и у пользователей со скрытым профилем
            await message.copy_to(admin_id, reply_markup=reply_kb.as_markup())
            success += 1
        except Exception as e:
            logger.error(f"Не удалось отправить feedback админу {admin_id}: {e}")

    await state.clear()
    menu = get_reply_main_menu_keyboard(user_data['course'] if user_data else None, user_id)
    if success:
        await message.answer(
            "✅ <b>Ваше сообщение отправлено!</b>\n\nАдминистратор получил ваше обращение и свяжется с вами.",
            reply_markup=menu)
    else:
        await message.answer("❌ Произошла ошибка при отправке сообщения. Попробуйте позже.", reply_markup=menu)


@dp.callback_query(F.data.startswith("reply_to_user__"), ADMIN_ONLY)
async def start_admin_reply(callback: types.CallbackQuery, state: FSMContext):
    target_user_id = int(callback.data.split("__")[1])
    target = await db.get_user(target_user_id)

    await state.update_data(reply_target_user_id=target_user_id)
    await state.set_state(AdminReplyStates.waiting_for_reply_message)

    name = html.escape(target['full_name'] or "—") if target else "не в базе"
    await callback.message.answer(
        f"✉️ <b>Ответ пользователю</b>\n\n👤 {name}\n🆔 <code>{target_user_id}</code>\n\n"
        f"Отправьте ваш ответ (текст, фото, документ и т.д.)\nДля отмены — /cancel")
    await callback.answer()


@dp.message(AdminReplyStates.waiting_for_reply_message, F.text | F.photo | F.document | F.video | F.audio | F.voice)
async def process_admin_reply(message: types.Message, state: FSMContext):
    admin_id = message.from_user.id

    if is_command(message):
        return await message.answer("Это команда. Отправьте ответ пользователю или /cancel.")

    data = await state.get_data()
    target_user_id = data.get('reply_target_user_id')
    if not target_user_id:
        await state.clear()
        return await message.answer("❌ Ошибка: не удалось определить получателя.")

    try:
        await bot.send_message(target_user_id, "📬 <b>Ответ от администратора:</b>")
        await message.copy_to(target_user_id)
        await db.log_admin_action(admin_id, "feedback_reply", f"user={target_user_id}")
        await message.answer("✅ <b>Ваш ответ успешно отправлен пользователю!</b>")
    except Exception as e:
        logger.error(f"Не удалось отправить ответ пользователю {target_user_id}: {e}")
        await message.answer(f"❌ <b>Ошибка отправки:</b>\n<code>{html.escape(str(e))}</code>\n\n"
                             f"Возможно, пользователь заблокировал бота.")

    await state.clear()
    await message.answer("Готово.", reply_markup=await main_menu_kb(admin_id))


# -------------------- ЗАПАСНЫЕ ОБРАБОТЧИКИ (регистрируются ПОСЛЕДНИМИ) --------------------
@dp.message(StateFilter(
    ExamStates.waiting_for_subject,
    MaterialsStates.waiting_for_material_type,
    MaterialsStates.waiting_for_subject,
    AdminStates.waiting_for_materials_type,
    AdminStates.waiting_for_materials_course,
    AdminStates.waiting_for_summary_course,
    AdminStates.waiting_for_broadcast_confirm,
    AdminReplyStates.waiting_for_reply_message,
    FeedbackStates.waiting_for_feedback_message,
))
async def state_fallback(message: types.Message):
    await message.answer("Пожалуйста, используйте кнопки меню, отправьте корректное сообщение или /cancel для отмены.")


# -------------------- ОБРАБОТЧИК ОШИБОК --------------------
@dp.errors()
async def global_error_handler(event: ErrorEvent):
    logger.error(f"Unhandled error: {event.exception!r}", exc_info=event.exception)

    update = event.update
    target = update.message or (update.callback_query.message if update.callback_query else None)
    if target:
        try:
            await target.answer("⚠️ Произошла внутренняя ошибка. Попробуйте позже или нажмите /start.")
        except Exception:
            pass

    await alert_admins(
        "unhandled_error",
        f"⚠️ <b>Ошибка в боте:</b>\n<code>{html.escape(repr(event.exception))[:500]}</code>",
        interval=300)
    return True


# -------------------- ЗАПУСК --------------------
async def main():
    await db.init_pool()

    dp.message.middleware(SubscriptionMiddleware())
    dp.callback_query.middleware(CallbackSubscriptionMiddleware())

    logger.info("Бот запущен (админы: %s)...", sorted(ADMIN_IDS))
    try:
        await dp.start_polling(bot)
    finally:
        for task in list(_background_tasks):
            task.cancel()
        await dp.storage.close()
        await bot.session.close()
        await db.close()
        logger.info("Бот остановлен.")


if __name__ == "__main__":
    asyncio.run(main())
