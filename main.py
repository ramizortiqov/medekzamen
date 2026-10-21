import asyncio
import hashlib
import html
import io
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import asyncpg
from dotenv import load_dotenv

from aiogram import BaseMiddleware, Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

# Опциональный импорт библиотек Google Drive API
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build as gdrive_build
    from googleapiclient.http import MediaIoBaseDownload
    GDRIVE_AVAILABLE = True
except ImportError:
    GDRIVE_AVAILABLE = False

# -------------------- 1. ЛОГИРОВАНИЕ --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
#Hi HIHIHIIHIHIIH
# ------------------- 2. НАСТРОЙКИ --------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("REDIS_URL")  # необязательно: если задан — состояния FSM переживут перезапуск

# Настройки Google Drive (по умолчанию прописан ваш ID корневой папки medekzamenbot)
DEFAULT_GDRIVE_ROOT_ID = "1JFm2VPx4SXCB7XS3HRC68_IA2Ef708zv"
GDRIVE_CREDENTIALS_FILE = os.getenv("GDRIVE_CREDENTIALS_FILE", "credentials.json")
GDRIVE_ROOT_FOLDER_ID = os.getenv("GDRIVE_ROOT_FOLDER_ID", DEFAULT_GDRIVE_ROOT_ID).strip()

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


def create_bot(token: str) -> Bot:
    """Поддержка как старых (3.0–3.6), так и новых (3.7+) версий aiogram."""
    try:
        from aiogram.client.default import DefaultBotProperties
        return Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    except ImportError:
        return Bot(token=token, parse_mode=ParseMode.HTML)


storage = build_storage()
bot = create_bot(BOT_TOKEN)
dp = Dispatcher(storage=storage)

# -------------------- 3. ДАННЫЕ О ФАКУЛЬТЕТАХ, КУРСАХ И ПРЕДМЕТАХ --------------------
FACULTIES: Dict[str, str] = {
    "tibbi": "🏥 Тибби",
    "stom": "🦷 Стоматологи",
    "ped": "👶 Педиатри",
    "pharm": "💊 Фармацевти",
    "ntch": "🛡️ НТЧ",
}
FACULTY_DRIVE_NAMES: Dict[str, str] = {
    "tibbi": "Тибби",
    "stom": "Стоматологи",
    "ped": "Педиатри",
    "pharm": "Фармацевти",
    "ntch": "НТЧ",
}
# Короткие коды факультетов в названиях папок вида 1-k-farm-ru-okt, 1-k-ntj-tj-okt и т.д.
FACULTY_SHORT_CODES: Dict[str, str] = {
    "pharm": "farm",
    "ntch": "ntj",
    "ped": "ped",
    "stom": "stom",
    "tibbi": "tibbi",
}
FACULTY_FOLDER_ALIASES: Dict[str, set] = {
    "tibbi": {"tibbi", "tib", "тибби", "тиббӣ", "леч", "лечебный"},
    "stom": {"stom", "стоматологи", "стом", "стоматология"},
    "ped": {"ped", "педиатри", "пед", "педиатрия"},
    "pharm": {"farm", "pharm", "фармацевти", "фарм", "фармация"},
    "ntch": {"ntj", "ntch", "нтч", "нтҷ"},
}

GROUPS: Dict[str, str] = {"ru": "🇷🇺 Русский", "tj": "🇹🇯 Таджикский"}
GROUP_DRIVE_NAMES: Dict[str, str] = {"ru": "Русский", "tj": "Таджикский"}
GROUP_FOLDER_ALIASES: Dict[str, set] = {
    "ru": {"ru", "rus", "рус", "русский"},
    "tj": {"tj", "taj", "тдж", "тадж", "таджикский", "точики", "тоҷикӣ"},
}

COURSES = [1, 2, 3, 4, 5, 6]
MAX_COURSE_FOR_SUMMARY = 3
SUMMARY_SECTIONS = 4
MAT_PREFIX = "mat"

# Базовый список предметов по курсам (используется по умолчанию, если в БД ещё нет предметов с Google Диска).
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

# --- Производные структуры ---
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

MATERIAL_TYPES = {
    "lecture": "📖 Лекции+темплан",
    "practice": "🔬 Практика",
    "video": "🎥 Видео",
}
MATERIAL_TYPE_DRIVE_NAMES = {
    "lecture": "Лекции+темплан",
    "practice": "Практика",
    "video": "Видео",
}
MATERIAL_TYPE_ALIASES = {
    "lecture": ["Лекции+темплан", "📖 Лекции+темплан", "Лекции", "лекции", "lecture", "lektsii"],
    "practice": ["Практика", "🔬 Практика", "практика", "practice", "praktika"],
    "video": ["Видео", "🎥 Видео", "видео", "video"],
}

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


def make_dynamic_tag(prefix: str, course: int, subject_name: str) -> str:
    """Генерирует короткий стабильный тег для любой папки или файла предмета с Google Диска."""
    norm_subj = subject_name.strip().lower()
    if prefix == "exam":
        for _, subs in [(course, SUBJECTS_BY_COURSE.get(course, []))]:
            for t, n in subs:
                clean_n = re.sub(r"^[^\w\dА-Яа-яЁё]+", "", n).strip().lower()
                if norm_subj == n.lower() or norm_subj == clean_n:
                    return t
        h = hashlib.md5(f"{course}:{ norm_subj}".encode("utf-8")).hexdigest()[:8]
        return f"ex{course}_{h}"
    else:
        for t, n in SUBJECTS_BY_COURSE.get(course, []):
            clean_n = re.sub(r"^[^\w\dА-Яа-яЁё]+", "", n).strip().lower()
            if norm_subj == n.lower() or norm_subj == clean_n:
                return f"{prefix}_{MAT_PREFIX}{t}"
        h = hashlib.md5(f"{course}:{norm_subj}".encode("utf-8")).hexdigest()[:8]
        return f"{prefix}_mat{course}_{h}"


# -------------------- 4. БАЗА ДАННЫХ (PostgreSQL) --------------------
USER_CACHE_TTL = 60
BLOCK_CACHE_TTL = 30
TOUCH_INTERVAL = 600


class PostgresDB:
    """
    Работа с PostgreSQL через asyncpg.
    Все пользователи, предметы и Telegram file_id хранятся в PostgreSQL.
    При обычных запросах пользователей бот берёт данные ТОЛЬКО из PostgreSQL (мгновенно!).
    """

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
                    faculty TEXT,
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
                    faculty TEXT,
                    group_lang TEXT,
                    source TEXT DEFAULT 'telegram',
                    drive_file_id TEXT,
                    drive_md5 TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS drive_subjects (
                    id SERIAL PRIMARY KEY,
                    section TEXT NOT NULL,
                    faculty TEXT NOT NULL,
                    course INTEGER NOT NULL,
                    group_lang TEXT NOT NULL,
                    subject_name TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    folder_id TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (section, faculty, course, group_lang, subject_name)
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

                -- Миграции для существующих таблиц
                ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active TIMESTAMP;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS faculty TEXT;

                ALTER TABLE materials ADD COLUMN IF NOT EXISTS uploaded_by BIGINT;
                ALTER TABLE materials ADD COLUMN IF NOT EXISTS faculty TEXT;
                ALTER TABLE materials ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'telegram';
                ALTER TABLE materials ADD COLUMN IF NOT EXISTS drive_file_id TEXT;
                ALTER TABLE materials ADD COLUMN IF NOT EXISTS drive_md5 TEXT;

                CREATE INDEX IF NOT EXISTS idx_materials_drive_file_id ON materials (drive_file_id);
                CREATE INDEX IF NOT EXISTS idx_materials_lookup ON materials (tag, faculty, group_lang);
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

    async def save_user(self, user_id: int, username: str, full_name: str,
                        course: int, faculty: str, group_lang: str):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO users (user_id, username, full_name, course, faculty, group_lang)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    full_name = EXCLUDED.full_name,
                    faculty = COALESCE(users.faculty, EXCLUDED.faculty)
            ''', user_id, username, full_name, course, faculty, group_lang)
        self._user_cache.pop(user_id, None)

    async def set_user_faculty(self, user_id: int, faculty: str):
        async with self.pool.acquire() as conn:
            await conn.execute('UPDATE users SET faculty = $2 WHERE user_id = $1', user_id, faculty)
        self._user_cache.pop(user_id, None)

    async def set_user_profile(self, user_id: int, course: int,
                               group_lang: Optional[str] = None,
                               faculty: Optional[str] = None) -> int:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow('SELECT course, group_lang, faculty FROM users WHERE user_id = $1', user_id)
            if not row:
                return 0
            new_group = group_lang or row['group_lang']
            new_faculty = faculty or row['faculty']
            result = await conn.execute(
                'UPDATE users SET course = $2, group_lang = $3, faculty = $4 WHERE user_id = $1',
                user_id, course, new_group, new_faculty
            )
        self._user_cache.pop(user_id, None)
        return int(result.split()[-1])

    async def promote_all(self) -> int:
        async with self.pool.acquire() as conn:
            count = await conn.fetchval('''
                WITH u AS (
                    UPDATE users SET course = course + 1 WHERE course < $1 RETURNING 1
                ) SELECT COUNT(*) FROM u
            ''', max(COURSES))
        self._user_cache.clear()
        return count

    async def touch_user(self, user_id: int):
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
                       u.username, u.full_name, u.course, u.faculty, u.group_lang
                FROM blocked_users b
                LEFT JOIN users u ON b.user_id = u.user_id
                ORDER BY b.blocked_at DESC
                LIMIT $1
            ''', limit)

    # ---------- материалы и синхронизация с Google Drive ----------
    async def get_materials_by_tag(self, tag: str):
        async with self.pool.acquire() as conn:
            return await conn.fetch('SELECT * FROM materials WHERE tag = $1 ORDER BY created_at, id', tag)

    async def get_material_by_drive_id(self, drive_file_id: str, tag: str,
                                       faculty: Optional[str] = None, group_lang: Optional[str] = None):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow('''
                SELECT * FROM materials
                WHERE drive_file_id = $1 AND tag = $2
                  AND COALESCE(faculty, '') = COALESCE($3, '')
                  AND COALESCE(group_lang, '') = COALESCE($4, '')
                LIMIT 1
            ''', drive_file_id, tag, faculty, group_lang)

    async def get_gdrive_cache(self) -> Dict[Tuple[str, str, str, str], Tuple[Optional[str], Optional[str]]]:
        """Все файлы с Диска, уже лежащие в БД, одним запросом: ключ -> (file_id, drive_md5)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch('''
                SELECT drive_file_id, tag, COALESCE(faculty, '') AS fac,
                       COALESCE(group_lang, '') AS grp, file_id, drive_md5
                FROM materials
                WHERE drive_file_id IS NOT NULL
            ''')
        return {
            (r['drive_file_id'], r['tag'], r['fac'], r['grp']): (r['file_id'], r['drive_md5'])
            for r in rows
        }

    async def save_material(self, tag: str, type_: str, file_id: str = None, file_name: str = None,
                            caption: str = None, course: int = None, faculty: str = None,
                            group_lang: str = None, uploaded_by: int = None,
                            source: str = "telegram", drive_file_id: str = None, drive_md5: str = None):
        async with self.pool.acquire() as conn:
            if drive_file_id:
                existing = await conn.fetchval('''
                    SELECT id FROM materials
                    WHERE drive_file_id = $1 AND tag = $2
                      AND COALESCE(faculty, '') = COALESCE($3, '')
                      AND COALESCE(group_lang, '') = COALESCE($4, '')
                ''', drive_file_id, tag, faculty, group_lang)
                if existing:
                    await conn.execute('''
                        UPDATE materials
                        SET file_id = $1, file_name = $2, caption = $3, drive_md5 = $4,
                            course = $5, faculty = $6, group_lang = $7
                        WHERE id = $8
                    ''', file_id, file_name, caption, drive_md5, course, faculty, group_lang, existing)
                    return existing

            return await conn.fetchval('''
                INSERT INTO materials (
                    tag, type, file_id, file_name, caption, course, faculty, group_lang,
                    uploaded_by, source, drive_file_id, drive_md5
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                RETURNING id
            ''', tag, type_, file_id, file_name, caption, course, faculty, group_lang,
                                       uploaded_by, source, drive_file_id, drive_md5)

    async def cleanup_deleted_drive_materials(self, tag: str, faculty: Optional[str],
                                              group_lang: Optional[str], active_drive_ids: List[str]) -> int:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch('''
                SELECT id, drive_file_id FROM materials
                WHERE tag = $1 AND source = 'gdrive'
                  AND COALESCE(faculty, '') = COALESCE($2, '')
                  AND COALESCE(group_lang, '') = COALESCE($3, '')
            ''', tag, faculty, group_lang)
            active_set = set(active_drive_ids)
            to_delete = [r['id'] for r in rows if r['drive_file_id'] not in active_set]
            if to_delete:
                await conn.execute('DELETE FROM materials WHERE id = ANY($1::int[])', to_delete)
                logger.info(f"Удалено {len(to_delete)} устаревших файлов GDrive из БД для #{tag}")
            return len(to_delete)

    async def save_drive_subjects(self, section: str, faculty: str, course: int, group_lang: str,
                                  subjects: List[Tuple[str, str, str]]):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute('''
                    DELETE FROM drive_subjects
                    WHERE section = $1 AND faculty = $2 AND course = $3 AND group_lang = $4
                ''', section, faculty, course, group_lang)
                for subj_name, tag, folder_id in subjects:
                    await conn.execute('''
                        INSERT INTO drive_subjects (section, faculty, course, group_lang, subject_name, tag, folder_id)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ''', section, faculty, course, group_lang, subj_name, tag, folder_id)

    async def save_drive_subjects_bulk(self, subject_menus) -> Tuple[int, int]:
        """
        Сохраняет предметы всех групп за 1-2 обращения к БД (раньше — по несколько запросов
        на каждую группу). Перезаписываются только группы, где данные реально изменились.
        Возвращает (всего_групп, изменено_групп).
        """
        # Объединяем одинаковые группы (разные папки Диска → один ключ), дубли имён отбрасываем
        desired: Dict[Tuple[str, str, int, str], Dict[str, Tuple[str, str]]] = {}
        for section, faculty, course, group_lang, subjects in subject_menus:
            group = desired.setdefault((section, faculty, course, group_lang), {})
            for subj_name, tag, folder_id in subjects:
                group.setdefault(subj_name, (tag, folder_id))
        if not desired:
            return 0, 0

        async with self.pool.acquire() as conn:
            rows = await conn.fetch('''
                SELECT section, faculty, course, group_lang, subject_name, tag, folder_id
                FROM drive_subjects
            ''')
            existing: Dict[Tuple[str, str, int, str], Dict[str, Tuple[str, str]]] = {}
            for r in rows:
                key = (r['section'], r['faculty'], r['course'], r['group_lang'])
                existing.setdefault(key, {})[r['subject_name']] = (r['tag'], r['folder_id'])

            changed = [k for k, v in desired.items() if existing.get(k) != v]
            if not changed:
                return len(desired), 0

            async with conn.transaction():
                await conn.executemany('''
                    DELETE FROM drive_subjects
                    WHERE section = $1 AND faculty = $2 AND course = $3 AND group_lang = $4
                ''', changed)
                await conn.executemany('''
                    INSERT INTO drive_subjects (section, faculty, course, group_lang, subject_name, tag, folder_id)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                ''', [
                    (k[0], k[1], k[2], k[3], name, tag, folder_id)
                    for k in changed
                    for name, (tag, folder_id) in desired[k].items()
                ])
            return len(desired), len(changed)

    async def cleanup_deleted_drive_materials_bulk(self, active: Dict[Tuple[str, str, str], set]) -> int:
        """
        Удаляет из БД файлы Диска, которых там больше нет, одним SELECT и одним DELETE.
        active: {(tag, faculty или '', group_lang или ''): {drive_file_id, ...}}
        """
        if not active:
            return 0
        async with self.pool.acquire() as conn:
            rows = await conn.fetch('''
                SELECT id, tag, COALESCE(faculty, '') AS fac, COALESCE(group_lang, '') AS grp, drive_file_id
                FROM materials
                WHERE source = 'gdrive'
            ''')
            to_delete = [
                r['id'] for r in rows
                if (r['tag'], r['fac'], r['grp']) in active
                and r['drive_file_id'] not in active[(r['tag'], r['fac'], r['grp'])]
            ]
            if to_delete:
                await conn.execute('DELETE FROM materials WHERE id = ANY($1::int[])', to_delete)
                logger.info(f"Удалено {len(to_delete)} устаревших файлов GDrive из БД")
            return len(to_delete)

    async def get_drive_subjects(self, section: str, faculty: str, course: int, group_lang: str):
        async with self.pool.acquire() as conn:
            return await conn.fetch('''
                SELECT subject_name, tag, folder_id
                FROM drive_subjects
                WHERE section = $1 AND faculty = $2 AND course = $3 AND group_lang = $4
                ORDER BY subject_name
            ''', section, faculty, course, group_lang)

    async def delete_material(self, material_id: int) -> int:
        async with self.pool.acquire() as conn:
            result = await conn.execute('DELETE FROM materials WHERE id = $1', material_id)
        return int(result.split()[-1])

    async def delete_materials_by_tag(self, tag: str, faculty: Optional[str] = None,
                                      group_lang: Optional[str] = None) -> int:
        async with self.pool.acquire() as conn:
            if faculty and group_lang:
                result = await conn.execute(
                    'DELETE FROM materials WHERE tag = $1 AND (faculty = $2 OR faculty IS NULL) AND (group_lang = $3 OR group_lang IS NULL)',
                    tag, faculty, group_lang
                )
            else:
                result = await conn.execute('DELETE FROM materials WHERE tag = $1', tag)
        return int(result.split()[-1])

    async def get_materials_summary(self):
        async with self.pool.acquire() as conn:
            return await conn.fetch('''
                SELECT tag, type, course, faculty, group_lang, source, COUNT(*) AS cnt
                FROM materials
                GROUP BY tag, type, course, faculty, group_lang, source
                ORDER BY tag, type
            ''')

    # ---------- экспорт и импорт резервной копии БД ----------
    async def export_backup_json(self) -> Dict[str, Any]:
        """Формирует JSON-бэкап напрямую из таблиц PostgreSQL (users, materials, drive_subjects)."""
        async with self.pool.acquire() as conn:
            users = await conn.fetch(
                'SELECT user_id, username, full_name, course, faculty, group_lang, is_active FROM users'
            )
            materials = await conn.fetch('''
                SELECT tag, type, file_id, file_name, caption, course, faculty, group_lang,
                       source, drive_file_id, drive_md5
                FROM materials
            ''')
            subjects = await conn.fetch('''
                SELECT section, faculty, course, group_lang, subject_name, tag, folder_id
                FROM drive_subjects
            ''')
        return {
            "exported_at": datetime.utcnow().isoformat(),
            "users": [dict(u) for u in users],
            "materials": [dict(m) for m in materials],
            "drive_subjects": [dict(s) for s in subjects],
        }

    async def import_backup_json(self, data: Dict[str, Any]) -> Tuple[int, int]:
        users_list = data.get("users", [])
        materials_list = data.get("materials", [])
        subjects_list = data.get("drive_subjects", [])
        u_cnt = m_cnt = 0
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for u in users_list:
                    await conn.execute('''
                        INSERT INTO users (user_id, username, full_name, course, faculty, group_lang, is_active)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        ON CONFLICT (user_id) DO UPDATE SET
                            course = COALESCE(users.course, EXCLUDED.course),
                            faculty = COALESCE(users.faculty, EXCLUDED.faculty),
                            group_lang = COALESCE(users.group_lang, EXCLUDED.group_lang)
                    ''', u['user_id'], u.get('username'), u.get('full_name'),
                                       u.get('course'), u.get('faculty'), u.get('group_lang'),
                                       u.get('is_active', True))
                    u_cnt += 1
                for m in materials_list:
                    exists = await conn.fetchval('''
                        SELECT id FROM materials
                        WHERE tag = $1 AND COALESCE(file_id, '') = COALESCE($2, '')
                          AND COALESCE(caption, '') = COALESCE($3, '')
                          AND COALESCE(faculty, '') = COALESCE($4, '')
                          AND COALESCE(group_lang, '') = COALESCE($5, '')
                        LIMIT 1
                    ''', m['tag'], m.get('file_id'), m.get('caption'), m.get('faculty'), m.get('group_lang'))
                    if not exists:
                        await conn.execute('''
                            INSERT INTO materials (
                                tag, type, file_id, file_name, caption, course, faculty, group_lang,
                                source, drive_file_id, drive_md5
                            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                        ''', m['tag'], m['type'], m.get('file_id'), m.get('file_name'),
                                           m.get('caption'), m.get('course'), m.get('faculty'),
                                           m.get('group_lang'), m.get('source', 'telegram'),
                                           m.get('drive_file_id'), m.get('drive_md5'))
                        m_cnt += 1
                for s in subjects_list:
                    await conn.execute('''
                        INSERT INTO drive_subjects (section, faculty, course, group_lang, subject_name, tag, folder_id)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        ON CONFLICT (section, faculty, course, group_lang, subject_name)
                        DO UPDATE SET tag = EXCLUDED.tag, folder_id = EXCLUDED.folder_id
                    ''', s['section'], s['faculty'], s['course'], s['group_lang'],
                                       s['subject_name'], s['tag'], s['folder_id'])
        self._user_cache.clear()
        return u_cnt, m_cnt

    # ---------- рассылка / статистика / журнал ----------
    async def get_users_for_broadcast(self, course: int, group_lang: str, faculty: str = 'all') -> List[int]:
        conditions = ["is_active IS NOT FALSE", "user_id NOT IN (SELECT user_id FROM blocked_users)"]
        args: List[Any] = []
        if course != 0:
            args.append(course)
            conditions.append(f"course = ${len(args)}")
        if group_lang != 'all':
            args.append(group_lang)
            conditions.append(f"group_lang = ${len(args)}")
        if faculty != 'all':
            args.append(faculty)
            conditions.append(f"faculty = ${len(args)}")
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
                "by_faculty": await conn.fetch(
                    'SELECT faculty, COUNT(*) AS cnt FROM users GROUP BY faculty ORDER BY cnt DESC'),
                "by_group": await conn.fetch(
                    'SELECT group_lang, COUNT(*) AS cnt FROM users GROUP BY group_lang ORDER BY group_lang'),
                "total_materials": await conn.fetchval('SELECT COUNT(*) FROM materials'),
                "gdrive_materials": await conn.fetchval(
                    "SELECT COUNT(*) FROM materials WHERE source = 'gdrive'"),
                "tg_materials": await conn.fetchval(
                    "SELECT COUNT(*) FROM materials WHERE source = 'telegram' OR source IS NULL"),
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


# -------------------- 4.1 ИНТЕГРАЦИЯ С GOOGLE ДИСКОМ (СИНХРОНИЗАЦИЯ В БД) --------------------
class GoogleDriveManager:
    """
    Отвечает за синхронизацию папок и файлов с Google Диска в PostgreSQL по команде администратора.
    Во время обычной работы пользователей к Google Диску НЕТ никаких запросов —
    бот берёт предметы и Telegram file_id напрямую из PostgreSQL!
    """

    SCOPES = ["https://www.googleapis.com/auth/drive"]

    def __init__(self, credentials_path: str, root_folder_id: str):
        self.credentials_path = credentials_path
        self.root_folder_id = root_folder_id
        self._tl = threading.local()  # индекс дерева Диска на время одного сканирования

    @property
    def is_enabled(self) -> bool:
        return bool(GDRIVE_AVAILABLE and self.root_folder_id and os.path.exists(self.credentials_path))

    def _get_service(self):
        creds = service_account.Credentials.from_service_account_file(
            self.credentials_path, scopes=self.SCOPES
        )
        return gdrive_build("drive", "v3", credentials=creds, cache_discovery=False)

    @staticmethod
    def _normalize_name(name: str) -> str:
        cleaned = re.sub(r"^[^\w\dА-Яа-яЁёҶҷҚқҒғҲҳӢӣӮӯ]+", "", name.strip())
        return cleaned.strip().lower()

    @staticmethod
    def _tokenize(name: str) -> set:
        return {t for t in re.split(r"[^a-zA-Zа-яА-ЯёЁҷҶқҚғҒҳҲӣӢӯӮ0-9]+", name.lower()) if t}

    @classmethod
    def _matches_course_folder(cls, folder_name: str, course: int) -> bool:
        norm = cls._normalize_name(folder_name)
        if norm in {
            f"{course}-kurs", f"{course} kurs", f"{course}_kurs",
            f"{course}-курс", f"{course} курс", f"{course}-й курс",
            f"{course}k", f"{course}-k", str(course),
        }:
            return True
        return bool(re.match(rf"^0?{course}\s*[-_]?\s*(?:kurs|курс|k|к|-й\s*курс)$", norm, re.IGNORECASE))

    @classmethod
    def _detect_course(cls, folder_name: str) -> Optional[int]:
        for c in COURSES:
            if cls._matches_course_folder(folder_name, c):
                return c
        return None

    @classmethod
    def _matches_rating_folder(cls, folder_name: str, course: int, section_num: int) -> bool:
        norm = cls._normalize_name(folder_name)
        if norm in {
            f"итог {course}.{section_num}",
            f"{course}.{section_num}",
            f"раздел {section_num}",
            f"рейтинг {section_num}",
            f"итог {section_num}",
            str(section_num),
        }:
            return True
        if re.search(rf"(?:рейтинг|итог|rating|раздел|рубеж)\s*{course}\.{section_num}(?!\d)", norm):
            return True
        if re.search(rf"(?:рейтинг|итог|rating|раздел|рубеж)\s*[-_№#]?\s*0?{section_num}(?![.\d])", norm):
            return True
        return False

    @classmethod
    def _detect_rating_num(cls, folder_name: str) -> Optional[int]:
        for sec in range(1, SUMMARY_SECTIONS + 1):
            if cls._matches_rating_folder(folder_name, 1, sec):
                return sec
        return None

    @classmethod
    def _detect_fac_and_lang(cls, folder_name: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Из имени папки вроде '1-k-farm-ru-okt' или '1-k-ntj-tj-okt' определяет (faculty_key, group_lang).
        """
        tokens = cls._tokenize(folder_name)
        fac_key = None
        for fk, aliases in FACULTY_FOLDER_ALIASES.items():
            if tokens & aliases:
                fac_key = fk
                break
        lang_key = None
        for lk, aliases in GROUP_FOLDER_ALIASES.items():
            if tokens & aliases:
                lang_key = lk
                break
        return fac_key, lang_key

    @classmethod
    def _matches_fac_lang_folder(cls, folder_name: str, faculty_key: Optional[str],
                                 group_lang: str, course: Optional[int] = None) -> bool:
        tokens = cls._tokenize(folder_name)
        lang_aliases = GROUP_FOLDER_ALIASES.get(group_lang, {group_lang.lower()})
        if not (tokens & lang_aliases):
            return False

        if faculty_key and faculty_key != "all":
            fac_aliases = FACULTY_FOLDER_ALIASES.get(faculty_key, {faculty_key.lower()})
            if not (tokens & fac_aliases):
                return False
        else:
            all_fac_tokens = set().union(*FACULTY_FOLDER_ALIASES.values())
            if tokens & all_fac_tokens:
                return False

        if course is not None:
            m = re.match(r"^\s*(\d+)\s*[-_]", folder_name)
            if m and int(m.group(1)) != course:
                return False
        return True

    def _list_children_sync(self, parent_id: str, folders_only: Optional[bool] = False,
                            service=None) -> List[Dict[str, Any]]:
        index = getattr(self._tl, "index", None)
        if index is not None:
            FOLDER = "application/vnd.google-apps.folder"
            items = index.get(parent_id, [])
            if folders_only is True:
                return [i for i in items if i.get("mimeType") == FOLDER]
            if folders_only is False:
                return [i for i in items if i.get("mimeType") != FOLDER]
            return list(items)
        if service is None:
            service = self._get_service()
        if folders_only is True:
            mime_cond = "and mimeType = 'application/vnd.google-apps.folder'"
        elif folders_only is False:
            mime_cond = "and mimeType != 'application/vnd.google-apps.folder'"
        else:
            mime_cond = ""
        query = f"'{parent_id}' in parents and trashed = false {mime_cond}"
        items: List[Dict[str, Any]] = []
        page_token = None
        while True:
            resp = service.files().list(
                q=query,
                spaces="drive",
                fields="nextPageToken, files(id, name, mimeType, md5Checksum, modifiedTime, size)",
                pageToken=page_token,
                pageSize=200,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            items.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        items.sort(key=lambda x: x.get("name", "").lower())
        return items

    def _find_child_folder_sync(self, parent_id: str, candidates: List[str], service=None) -> Optional[str]:
        folders = self._list_children_sync(parent_id, folders_only=True, service=service)
        norm_candidates = {self._normalize_name(c) for c in candidates if c}
        lower_candidates = {c.lower() for c in candidates if c}
        for f in folders:
            fname = f.get("name", "").strip()
            if fname.lower() in lower_candidates or self._normalize_name(fname) in norm_candidates:
                return f["id"]
        return None

    def _find_course_folder_in_sync(self, parent_id: str, course: int, service=None) -> Optional[str]:
        folders = self._list_children_sync(parent_id, folders_only=True, service=service)
        for f in folders:
            if self._matches_course_folder(f.get("name", ""), course):
                return f["id"]
        return None

    def _find_fac_lang_folder_in_sync(self, parent_id: str, faculty_key: Optional[str],
                                      group_lang: str, course: Optional[int] = None, service=None) -> Optional[str]:
        folders = self._list_children_sync(parent_id, folders_only=True, service=service)
        for f in folders:
            if self._matches_fac_lang_folder(f.get("name", ""), faculty_key, group_lang, course):
                return f["id"]
        return None

    def _ensure_folder_sync(self, service, parent_id: str, folder_name: str,
                            aliases: Optional[List[str]] = None) -> str:
        search_names = [folder_name] + (aliases or [])
        existing_id = self._find_child_folder_sync(parent_id, search_names, service=service)
        if existing_id:
            return existing_id
        meta = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        created = service.files().create(body=meta, fields="id", supportsAllDrives=True).execute()
        return created["id"]

    def _list_files_in_leaf_sync(self, folder_id: str, service=None) -> List[Dict[str, Any]]:
        children = self._list_children_sync(folder_id, folders_only=None, service=service)
        files = [c for c in children if c.get("mimeType") != "application/vnd.google-apps.folder"]
        subfolders = [c for c in children if c.get("mimeType") == "application/vnd.google-apps.folder"]
        for sf in subfolders:
            files.extend(self._list_children_sync(sf["id"], folders_only=False, service=service))
        return files

    def _collect_subjects_and_files_from_group_folder_sync(
        self,
        service,
        section: str,
        faculty: Optional[str],
        course: int,
        group_lang: str,
        group_folder_id: str,
    ) -> Tuple[List[Tuple[str, str, str]], List[Tuple[str, int, Optional[str], str, List[Dict[str, Any]]]]]:
        """
        Для папки группы в Экзамене или Материалах собирает:
        1) список предметов для таблицы drive_subjects
        2) список файлов по каждому предмету для синхронизации в таблицу materials
        """
        children = self._list_children_sync(group_folder_id, folders_only=None, service=service)
        subfolders = [c for c in children if c.get("mimeType") == "application/vnd.google-apps.folder"]
        direct_files = [c for c in children if c.get("mimeType") != "application/vnd.google-apps.folder"]

        subjects_meta: List[Tuple[str, str, str]] = []
        file_targets: List[Tuple[str, int, Optional[str], str, List[Dict[str, Any]]]] = []
        seen_names: set = set()
        tag_prefix = "exam" if section == "exam" else section

        # 1. Папки предметов
        for sf in subfolders:
            sname = sf["name"].strip()
            if not sname:
                continue
            seen_names.add(sname.lower())
            tag = make_dynamic_tag(tag_prefix, course, sname)
            subjects_meta.append((sname, tag, sf["id"]))
            s_files = self._list_files_in_leaf_sync(sf["id"], service=service)
            file_targets.append((tag, course, faculty, group_lang, s_files))

        # 2. Файлы предметов, лежащие напрямую в папке группы
        grouped_files: Dict[str, List[Dict[str, Any]]] = {}
        display_names: Dict[str, str] = {}
        for df in direct_files:
            raw_name = df.get("name", "").strip()
            stem = os.path.splitext(raw_name)[0].strip() or raw_name
            key = stem.lower()
            if key in seen_names:
                continue
            display_names.setdefault(key, stem)
            grouped_files.setdefault(key, []).append(df)

        for key, dfiles in grouped_files.items():
            sname = display_names[key]
            tag = make_dynamic_tag(tag_prefix, course, sname)
            spec = "files:" + ",".join(x["id"] for x in dfiles)
            subjects_meta.append((sname, tag, spec))
            file_targets.append((tag, course, faculty, group_lang, dfiles))

        return subjects_meta, file_targets

    def _build_index_sync(self, service) -> Dict[str, List[Dict[str, Any]]]:
        """
        Скачивает список ВСЕХ файлов и папок Диска несколькими большими запросами
        (по 1000 штук) и строит в памяти дерево {parent_id: [дети]}.
        Вместо сотен запросов «дети каждой папки» — несколько запросов.
        """
        index: Dict[str, List[Dict[str, Any]]] = {}
        page_token = None
        while True:
            resp = service.files().list(
                q="trashed = false",
                spaces="drive",
                fields="nextPageToken, files(id, name, mimeType, md5Checksum, modifiedTime, size, parents)",
                pageToken=page_token,
                pageSize=1000,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            for item in resp.get("files", []):
                for parent in item.get("parents", []) or []:
                    index.setdefault(parent, []).append(item)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        for children in index.values():
            children.sort(key=lambda x: x.get("name", "").lower())
        return index

    def _scan_entire_drive_sync(self):
        started = time.time()
        try:
            self._tl.index = self._build_index_sync(self._get_service())
        except Exception as e:
            logger.warning(f"Не удалось построить индекс Диска, сканирую по папкам (медленнее): {e}")
            self._tl.index = None
        try:
            return self._scan_entire_drive_impl()
        finally:
            self._tl.index = None
            logger.info(f"Сканирование Google Диска заняло {time.time() - started:.1f} с")

    def _scan_entire_drive_impl(self) -> Tuple[
        List[Tuple[str, str, int, str, List[Tuple[str, str, str]]]],
        List[Tuple[str, int, Optional[str], str, List[Dict[str, Any]]]]
    ]:
        """
        Обходит существующие папки на Google Диске (Итог, Экзамен, Материалы)
        и возвращает все найденные предметы и списки файлов для синхронизации с PostgreSQL.
        """
        service = self._get_service()
        all_subject_menus: List[Tuple[str, str, int, str, List[Tuple[str, str, str]]]] = []
        all_file_targets: List[Tuple[str, int, Optional[str], str, List[Dict[str, Any]]]] = []

        # --- 1. ИТОГ ---
        sum_id = self._find_child_folder_sync(
            self.root_folder_id, ["итог", "Итог", "Итоги", "📋 Итог"], service=service
        )
        if sum_id:
            rating_folders = self._list_children_sync(sum_id, folders_only=True, service=service)
            for rf in rating_folders:
                sec_num = self._detect_rating_num(rf.get("name", ""))
                if not sec_num:
                    continue
                course_folders = self._list_children_sync(rf["id"], folders_only=True, service=service)
                for cf in course_folders:
                    course = self._detect_course(cf.get("name", ""))
                    if course and course <= MAX_COURSE_FOR_SUMMARY:
                        group_folders = self._list_children_sync(cf["id"], folders_only=True, service=service)
                        for gf in group_folders:
                            fac_key, lang_key = self._detect_fac_and_lang(gf.get("name", ""))
                            if fac_key and lang_key:
                                tag = f"summary{course}.{sec_num}"
                                files = self._list_files_in_leaf_sync(gf["id"], service=service)
                                all_file_targets.append((tag, course, fac_key, lang_key, files))

        # --- 2. ЭКЗАМЕН ---
        exam_id = self._find_child_folder_sync(
            self.root_folder_id, ["экзамен", "Экзамен", "Экзамены", "📚 Экзамен"], service=service
        )
        if exam_id:
            top_exam_folders = self._list_children_sync(exam_id, folders_only=True, service=service)
            course_containers: List[Tuple[int, str]] = []
            for tf in top_exam_folders:
                c = self._detect_course(tf.get("name", ""))
                if c:
                    course_containers.append((c, tf["id"]))
                else:
                    # Возможно, это папка сессии (например, 'Сессия 2026-10'), внутри которой лежат 1-kurs..6-kurs
                    sub_cfs = self._list_children_sync(tf["id"], folders_only=True, service=service)
                    for scf in sub_cfs:
                        sc = self._detect_course(scf.get("name", ""))
                        if sc:
                            course_containers.append((sc, scf["id"]))

            for course, c_folder_id in course_containers:
                group_folders = self._list_children_sync(c_folder_id, folders_only=True, service=service)
                for gf in group_folders:
                    fac_key, lang_key = self._detect_fac_and_lang(gf.get("name", ""))
                    if fac_key and lang_key:
                        s_meta, f_targets = self._collect_subjects_and_files_from_group_folder_sync(
                            service, "exam", fac_key, course, lang_key, gf["id"]
                        )
                        all_subject_menus.append(("exam", fac_key, course, lang_key, s_meta))
                        all_file_targets.extend(f_targets)

        # --- 3. МАТЕРИАЛЫ (общие для всех факультетов) ---
        mat_id = self._find_child_folder_sync(
            self.root_folder_id, ["материалы", "Материалы", "📂 Материалы"], service=service
        )
        if mat_id:
            for m_key, aliases in MATERIAL_TYPE_ALIASES.items():
                t_id = self._find_child_folder_sync(mat_id, aliases, service=service)
                if not t_id:
                    continue
                course_folders = self._list_children_sync(t_id, folders_only=True, service=service)
                for cf in course_folders:
                    course = self._detect_course(cf.get("name", ""))
                    if not course:
                        continue
                    group_folders = self._list_children_sync(cf["id"], folders_only=True, service=service)
                    for gf in group_folders:
                        _, lang_key = self._detect_fac_and_lang(gf.get("name", ""))
                        if lang_key:
                            s_meta, f_targets = self._collect_subjects_and_files_from_group_folder_sync(
                                service, m_key, None, course, lang_key, gf["id"]
                            )
                            all_subject_menus.append((m_key, "all", course, lang_key, s_meta))
                            all_file_targets.extend(f_targets)

        return all_subject_menus, all_file_targets

    async def scan_entire_drive(self):
        return await asyncio.to_thread(self._scan_entire_drive_sync)

    def _download_file_sync(self, file_id: str, mime_type: str) -> Tuple[bytes, str]:
        service = self._get_service()
        buf = io.BytesIO()
        if mime_type == "application/vnd.google-apps.document":
            request = service.files().export_media(fileId=file_id, mimeType="text/plain")
            ext = ".txt"
        elif mime_type == "application/vnd.google-apps.spreadsheet":
            request = service.files().export_media(
                fileId=file_id,
                mimeType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            ext = ".xlsx"
        elif mime_type == "application/vnd.google-apps.presentation":
            request = service.files().export_media(fileId=file_id, mimeType="application/pdf")
            ext = ".pdf"
        else:
            request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
            ext = ""

        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue(), ext

    async def download_file(self, file_id: str, mime_type: str) -> Tuple[bytes, str]:
        return await asyncio.to_thread(self._download_file_sync, file_id, mime_type)

    def _init_base_structure_sync(self, create_default_subjects: bool = False) -> int:
        """Создаёт недостающие папки в стиле 1-kurs -> 1-k-farm-ru на Google Диске."""
        service = self._get_service()
        created_ops = 0

        # 1. Итог -> Рейтинг 1..4 -> 1..3-kurs -> 1-k-farm-ru и т.д.
        sum_id = self._ensure_folder_sync(service, self.root_folder_id, "итог", ["Итог", "📋 Итог"])
        existing_ratings = self._list_children_sync(sum_id, folders_only=True, service=service)
        for sec in range(1, SUMMARY_SECTIONS + 1):
            r_id = None
            for rf in existing_ratings:
                if self._matches_rating_folder(rf.get("name", ""), 1, sec):
                    r_id = rf["id"]
                    break
            if not r_id:
                r_id = self._ensure_folder_sync(service, sum_id, f"Рейтинг {sec}")
            for course in range(1, MAX_COURSE_FOR_SUMMARY + 1):
                c_id = self._find_course_folder_in_sync(r_id, course, service=service)
                if not c_id:
                    c_id = self._ensure_folder_sync(service, r_id, f"{course}-kurs")
                for fac_key, fac_short in sorted(FACULTY_SHORT_CODES.items(), key=lambda x: x[1]):
                    for g_key in ("ru", "tj"):
                        leaf_id = self._find_fac_lang_folder_in_sync(c_id, fac_key, g_key, course, service=service)
                        if not leaf_id:
                            self._ensure_folder_sync(service, c_id, f"{course}-k-{fac_short}-{g_key}")
                        created_ops += 1

        # 2. Экзамен -> 1..6-kurs -> 1-k-farm-ru (-> предметы)
        exam_id = self._ensure_folder_sync(service, self.root_folder_id, "экзамен", ["Экзамен", "📚 Экзамен"])
        for course in COURSES:
            c_id = self._find_course_folder_in_sync(exam_id, course, service=service)
            if not c_id:
                c_id = self._ensure_folder_sync(service, exam_id, f"{course}-kurs")
            for fac_key, fac_short in sorted(FACULTY_SHORT_CODES.items(), key=lambda x: x[1]):
                for g_key in ("ru", "tj"):
                    leaf_id = self._find_fac_lang_folder_in_sync(c_id, fac_key, g_key, course, service=service)
                    if not leaf_id:
                        leaf_id = self._ensure_folder_sync(service, c_id, f"{course}-k-{fac_short}-{g_key}")
                    created_ops += 1
                    if create_default_subjects:
                        for _, subj_title in SUBJECTS_BY_COURSE.get(course, []):
                            self._ensure_folder_sync(service, leaf_id, subj_title)

        # 3. Материалы (общие для всех факультетов) -> Тип -> 1..6-kurs -> 1-k-ru / 1-k-tj
        mat_id = self._ensure_folder_sync(service, self.root_folder_id, "материалы", ["Материалы", "📂 Материалы"])
        for m_key, m_name in MATERIAL_TYPE_DRIVE_NAMES.items():
            t_id = self._ensure_folder_sync(service, mat_id, m_name, MATERIAL_TYPE_ALIASES.get(m_key, []))
            for course in COURSES:
                c_id = self._find_course_folder_in_sync(t_id, course, service=service)
                if not c_id:
                    c_id = self._ensure_folder_sync(service, t_id, f"{course}-kurs")
                for g_key in ("ru", "tj"):
                    leaf_id = self._find_fac_lang_folder_in_sync(c_id, None, g_key, course, service=service)
                    if not leaf_id:
                        leaf_id = self._ensure_folder_sync(service, c_id, f"{course}-k-{g_key}")
                    created_ops += 1
                    if create_default_subjects:
                        for _, subj_title in SUBJECTS_BY_COURSE.get(course, []):
                            self._ensure_folder_sync(service, leaf_id, subj_title)

        return created_ops

    async def init_base_structure(self, create_default_subjects: bool = False) -> int:
        return await asyncio.to_thread(self._init_base_structure_sync, create_default_subjects)


gdrive = GoogleDriveManager(GDRIVE_CREDENTIALS_FILE, GDRIVE_ROOT_FOLDER_ID)


# -------------------- 5. FSM СОСТОЯНИЯ --------------------
class RegistrationStates(StatesGroup):
    waiting_for_course = State()
    waiting_for_faculty = State()
    waiting_for_group = State()
    waiting_for_confirmation = State()
    waiting_for_missing_faculty = State()


class AdminStates(StatesGroup):
    waiting_for_content = State()
    waiting_for_admin_faculty = State()
    waiting_for_admin_group = State()
    waiting_for_exam_course = State()
    waiting_for_summary_course = State()
    waiting_for_materials_type = State()
    waiting_for_materials_course = State()
    waiting_for_broadcast_faculty = State()
    waiting_for_broadcast_filter = State()
    waiting_for_broadcast_course = State()
    waiting_for_broadcast_content = State()
    waiting_for_broadcast_confirm = State()
    waiting_for_restore_file = State()


class FeedbackStates(StatesGroup):
    waiting_for_feedback_message = State()


class AdminReplyStates(StatesGroup):
    waiting_for_reply_message = State()


class MaterialsStates(StatesGroup):
    waiting_for_material_type = State()
    waiting_for_subject = State()


class ExamStates(StatesGroup):
    waiting_for_subject = State()


class SummaryStates(StatesGroup):
    waiting_for_section = State()


# -------------------- 6. СЕРВИСНЫЕ ФУНКЦИИ --------------------
SUB_CACHE_TTL = 120
_sub_cache: Dict[int, float] = {}
_alert_times: Dict[str, float] = {}
_feedback_last: Dict[int, float] = {}
FEEDBACK_COOLDOWN = 30
_background_tasks: set = set()
_broadcast_running = False
_gdrive_sync_running = False


def get_subscription_keyboard() -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔔 Подписка", url=CHANNEL_URL)
    builder.button(text="✅ Проверить", callback_data="check_subscription")
    builder.adjust(2)
    return builder.as_markup()


def get_no_access_message() -> str:
    return "🚫 <b>Доступ ограничен.</b> Для использования бота, пожалуйста, <b>подпишитесь</b> на наш канал."


async def alert_admins(key: str, text: str, interval: int = 3600):
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
    if not html_text:
        return ""
    text = html.unescape(re.sub(r"<[^>]+>", "", html_text))
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit] + "…"
    return html.escape(text)


async def send_long(chat_id: int, text: str, reply_markup=None):
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
    try:
        return await factory()
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        return await factory()


async def resolve_subjects_for_menu(section: str, faculty: str, course: int,
                                    group_lang: str) -> Dict[str, Tuple[str, Optional[str]]]:
    """
    Читает список предметов ТОЛЬКО из PostgreSQL (таблица drive_subjects) без обращения к Google Диску!
    Если в PostgreSQL для этого курса ещё нет синхронизированных предметов с Диска,
    возвращает стандартный список предметов из SUBJECTS_BY_COURSE.
    """
    rows = await db.get_drive_subjects(section, faculty, course, group_lang)
    if rows:
        return {r["subject_name"]: (r["tag"], r["folder_id"]) for r in rows}

    result: Dict[str, Tuple[str, Optional[str]]] = {}
    for base_tag, name in SUBJECTS_BY_COURSE.get(course, []):
        if section == "exam":
            result[name] = (base_tag, None)
        else:
            result[name] = (f"{section}_{MAT_PREFIX}{base_tag}", None)
    return result


# -------------------- 7. MIDDLEWARE --------------------
class SubscriptionMiddleware(BaseMiddleware):
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
    builder.button(text="☁️ Google Диск")
    builder.button(text="🚪 Личный кабинет")
    builder.button(text="✉️ Обратная связь")
    builder.adjust(3, 3, 3)
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


def get_faculty_selection_keyboard(with_back_to_main: bool = False):
    items = list(FACULTIES.values())
    nav = [["🔙 Главное меню"]] if with_back_to_main else []
    return build_rows_kb(items, nav, per_row=2)


def get_group_selection_keyboard(with_back_to_main: bool = False):
    items = list(GROUPS.values())
    nav = [["🔙 Главное меню"]] if with_back_to_main else []
    return build_rows_kb(items, nav, per_row=2)


def get_confirmation_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.button(text="✅ Подтвердить выбор")
    builder.button(text="🔙 Назад к выбору курса")
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)


def get_reply_exam_course_keyboard(max_course: int):
    items = [f"{c}-й курс" for c in range(1, max_course + 1)]
    return build_rows_kb(items, [["🔙 Главное меню"]], per_row=3)


def get_reply_dynamic_subject_keyboard(subject_names: List[str], user_id: int, section: str = "exam"):
    if is_admin(user_id):
        if section == "exam":
            nav = [["🔄 Обновить с Google Диска", "🔙 К выбору курса"], ["🔙 Главное меню"]]
        else:
            nav = [
                ["🔄 Обновить с Google Диска", "🔙 К типам материалов"],
                ["🔙 К выбору курса (Материалы)", "🔙 Главное меню"],
            ]
    else:
        if section == "exam":
            nav = [["🔙 Главное меню"]]
        else:
            nav = [["🔙 К типам материалов", "🔙 Главное меню"]]
    return build_rows_kb(subject_names, nav, per_row=2)


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


# --- Inline-клавиатуры админа ---
def get_admin_menu(tag: str) -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=f"➕ Загрузить через ТГ в #{tag}", callback_data=f"upload__{tag}")
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


def kb_admin_faculties(section: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=title, callback_data=f"admfac__{section}__{fkey}")
        for fkey, title in FACULTIES.items()
    ]
    rows = _inline_rows(buttons, 2)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admmenu_root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_admin_langs(section: str, faculty: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=title, callback_data=f"admlang__{section}__{faculty}__{gkey}")
        for gkey, title in GROUPS.items()
    ]
    rows = _inline_rows(buttons, 2)
    back_cb = "admmenu_root" if section == "mat" else f"admsec__{section}"
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_admin_courses(section: str, faculty: str, group_lang: str) -> InlineKeyboardMarkup:
    courses = range(1, MAX_COURSE_FOR_SUMMARY + 1) if section == "sum" else COURSES
    buttons = [
        InlineKeyboardButton(text=f"{c}-й курс", callback_data=f"admcourse__{section}__{faculty}__{group_lang}__{c}")
        for c in courses
    ]
    rows = _inline_rows(buttons, 3)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admfac__{section}__{faculty}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def kb_admin_tags(section: str, faculty: str, group_lang: str, course: int) -> InlineKeyboardMarkup:
    items: List[Tuple[str, str]] = []
    if section == "exam":
        smap = await resolve_subjects_for_menu("exam", faculty, course, group_lang)
        items = [(tag, name) for name, (tag, _) in smap.items()]
    elif section == "mat":
        for type_key, type_name in MATERIAL_TYPES.items():
            emoji = type_name.split()[0]
            smap = await resolve_subjects_for_menu(type_key, "all", course, group_lang)
            for name, (tag, _) in smap.items():
                items.append((tag, f"{emoji} {name}"))
    elif section == "sum":
        items = [(f"summary{course}.{i}", f"Итог {course}.{i}") for i in range(1, SUMMARY_SECTIONS + 1)]
    buttons = [
        InlineKeyboardButton(text=name, callback_data=f"select_admin_tag__{faculty}__{group_lang}__{course}__{tag}")
        for tag, name in items
    ]
    rows = _inline_rows(buttons, 2)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admlang__{section}__{faculty}__{group_lang}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# -------------------- 9. МГНОВЕННАЯ ОТПРАВКА ИЗ POSTGRESQL И СИНХРОНИЗАЦИЯ С GOOGLE ДИСКОМ --------------------
def _detect_telegram_media_type(file_name: str, mime_type: str) -> str:
    lower = (file_name or "").lower()
    if lower.endswith((".jpg", ".jpeg", ".png", ".webp")):
        return "photo"
    if lower.endswith((".mp4", ".mov", ".mkv")) or mime_type.startswith("video/"):
        return "video"
    if lower.endswith((".mp3", ".m4a", ".wav", ".ogg")) or mime_type.startswith("audio/"):
        return "audio"
    return "document"


def _extract_sent_file_id(sent_msg: types.Message, media_type: str) -> Optional[str]:
    if media_type == "photo" and sent_msg.photo:
        return sent_msg.photo[-1].file_id
    if media_type == "video" and sent_msg.video:
        return sent_msg.video.file_id
    if media_type == "audio" and sent_msg.audio:
        return sent_msg.audio.file_id
    if sent_msg.document:
        return sent_msg.document.file_id
    return None


async def send_material(chat_id: int, material, tag: str):
    """Отправляет один материал из PostgreSQL по Telegram file_id (мгновенно!)."""
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

    final_caption = (caption + "\n" if caption else "") + BOT_SIGNATURE.strip()
    if len(final_caption) > 1000:
        await with_retry(lambda: method(chat_id, **{arg_name: file_id}, protect_content=protect))
        await with_retry(lambda: bot.send_message(chat_id, final_caption, protect_content=protect))
    else:
        await with_retry(lambda: method(chat_id, **{arg_name: file_id}, caption=final_caption,
                                        protect_content=protect))


async def send_content_by_tag(
    chat_id: int,
    tag: str,
    course_filter_value: int = None,
    user_group: str = None,
    user_faculty: str = None,
    drive_folder_id: str = None,
):
    """
    Отправляет материалы пользователю НАПРЯМУЮ из PostgreSQL по сохранённым Telegram file_id!
    Никаких запросов к Google Диску при нажатии кнопок студентами не выполняется — всё работает мгновенно.
    """
    logger.info(
        f"Sending #{tag} from PostgreSQL to {chat_id}, course={course_filter_value}, "
        f"faculty={user_faculty}, group={user_group}"
    )
    admin = is_admin(chat_id)

    all_db_materials = await db.get_materials_by_tag(tag)
    filtered = []
    for m in all_db_materials:
        course_ok = admin or m['course'] is None or m['course'] == course_filter_value
        group_ok = m['group_lang'] is None or m['group_lang'] == user_group
        faculty_ok = user_faculty is None or m['faculty'] is None or m['faculty'] == user_faculty
        if course_ok and group_ok and faculty_ok:
            filtered.append(m)

    if not filtered:
        await bot.send_message(
            chat_id,
            f"❌ Нет доступных материалов в разделе <b>#{tag}</b> для выбранных параметров."
        )
        return

    sent_count = 0
    failed_count = 0
    for material in filtered:
        try:
            await send_material(chat_id, material, tag)
            sent_count += 1
        except Exception as e:
            failed_count += 1
            logger.error(f"Ошибка отправки {material['type']} (id={material['id']}) для #{tag}: {e}")
        await asyncio.sleep(0.05)

    if failed_count:
        await bot.send_message(chat_id, f"⚠️ Не удалось отправить файлов: {failed_count}.")
    await bot.send_message(chat_id, f"✅ Все доступные материалы отправлены ({sent_count} шт.).")


async def sync_drive_targets_to_db(
    admin_chat_id: int,
    subject_menus: List[Tuple[str, str, int, str, List[Tuple[str, str, str]]]],
    file_targets: List[Tuple[str, int, Optional[str], str, List[Dict[str, Any]]]],
) -> Tuple[int, int, int, int]:
    """
    Синхронизирует найденные на Google Диске предметы и файлы в базу PostgreSQL:
    - Новые или изменённые файлы скачивает с Google Диска, загружает в Telegram (получая file_id)
      и сохраняет file_id в таблицу materials в PostgreSQL.
    - Уже имеющиеся в PostgreSQL неизменённые файлы пропускает (не качает заново).
    - Удалённые с Google Диска файлы удаляет из PostgreSQL.
    Возвращает: (добавлено_новых, уже_в_базе, удалено_старых, ошибок)
    """
    _t0 = time.time()
    total_groups, changed_groups = await db.save_drive_subjects_bulk(subject_menus)
    logger.info(f"[sync] предметы: групп {total_groups}, изменено {changed_groups}, {time.time() - _t0:.1f} с")

    added_count = 0
    skipped_count = 0
    removed_count = 0
    failed_count = 0

    # ВАЖНО: разные папки Диска могут давать один и тот же (tag, faculty, group_lang)
    # (например, две папки сессий с одним курсом). Раньше каждая такая цель при очистке
    # удаляла файлы соседней, и при следующей синхронизации всё скачивалось заново.
    # Поэтому сначала объединяем цели с одинаковым ключом и убираем дубли файлов.
    merged: Dict[Tuple[str, Optional[str], str], Dict[str, Any]] = {}
    for tag, course, faculty, group_lang, drive_files in file_targets:
        key = (tag, faculty, group_lang)
        entry = merged.setdefault(key, {"course": course, "files": {}})
        for f in drive_files:
            entry["files"].setdefault(f["id"], f)

    # Весь кэш из БД одним запросом вместо запроса на каждый файл
    cache = await db.get_gdrive_cache()
    total_files = sum(len(e["files"]) for e in merged.values())
    logger.info(f"[sync] Найдено на Диске: {total_files} файлов, в кэше БД: {len(cache)}")

    _tc = time.time()
    removed_count += await db.cleanup_deleted_drive_materials_bulk({
        (tag, faculty or "", group_lang or ""): set(entry["files"].keys())
        for (tag, faculty, group_lang), entry in merged.items()
    })
    _cleanup_time = time.time() - _tc

    for (tag, faculty, group_lang), entry in merged.items():
        course = entry["course"]
        drive_files = list(entry["files"].values())

        for df in drive_files:
            d_id = df["id"]
            d_name = df.get("name", "document.txt")
            d_mime = df.get("mimeType", "application/octet-stream")
            d_ver = df.get("md5Checksum") or df.get("modifiedTime") or ""

            cached = cache.get((d_id, tag, faculty or "", group_lang or ""))
            if cached and cached[0] and cached[1] == d_ver:
                skipped_count += 1
                continue

            if not cached:
                why = "нового файла нет в БД"
            elif not cached[0]:
                why = "в БД нет file_id"
            else:
                why = f"версия изменилась (БД: {cached[1]!r} → Диск: {d_ver!r})"
            logger.info(f"[sync] #{tag} {faculty}/{group_lang}: загружаю '{d_name}' — {why}")

            try:
                _td = time.time()
                raw_bytes, added_ext = await gdrive.download_file(d_id, d_mime)
                logger.info(f"[sync] скачано с Диска '{d_name}' ({len(raw_bytes) / 1024 / 1024:.1f} МБ) за {time.time() - _td:.1f} с")
                _tu = time.time()
                if added_ext and not d_name.lower().endswith(added_ext):
                    d_name = d_name + added_ext

                media_type = _detect_telegram_media_type(d_name, d_mime)
                input_file = BufferedInputFile(raw_bytes, filename=d_name)
                fac_label = FACULTIES.get(faculty, "Общие") if faculty else "Общие"
                sync_caption = f"☁️➡️🗄 Синхронизация в БД: <b>{html.escape(d_name)}</b> (#{tag}, {course} к., {fac_label}, {group_lang})"

                sender_info = MEDIA_SENDERS.get(media_type, ("send_document", "document"))
                method = getattr(bot, sender_info[0])
                arg_name = sender_info[1]

                try:
                    sent_msg = await with_retry(
                        lambda: method(
                            admin_chat_id,
                            **{arg_name: input_file},
                            caption=sync_caption[:1024],
                            disable_notification=True,
                        )
                    )
                except TelegramBadRequest:
                    media_type = "document"
                    input_file = BufferedInputFile(raw_bytes, filename=d_name)
                    sent_msg = await with_retry(
                        lambda: bot.send_document(
                            admin_chat_id,
                            document=input_file,
                            caption=sync_caption[:1024],
                            disable_notification=True,
                        )
                    )

                logger.info(f"[sync] загружено в Telegram за {time.time() - _tu:.1f} с")
                new_file_id = _extract_sent_file_id(sent_msg, media_type)
                if new_file_id:
                    await db.save_material(
                        tag=tag,
                        type_=media_type,
                        file_id=new_file_id,
                        file_name=d_name,
                        caption=f"📄 {html.escape(d_name)}",
                        course=course,
                        faculty=faculty,
                        group_lang=group_lang,
                        uploaded_by=admin_chat_id,
                        source="gdrive",
                        drive_file_id=d_id,
                        drive_md5=d_ver,
                    )
                    added_count += 1
            except Exception as e:
                failed_count += 1
                logger.error(f"Ошибка синхронизации файла {d_name} ({d_id}) с Google Drive: {e}")

            await asyncio.sleep(0.25)

    logger.info(f"[sync] очистка удалённых: {_cleanup_time:.1f} с")
    logger.info(f"[sync] Готово: добавлено={added_count}, пропущено={skipped_count}, "
                f"удалено={removed_count}, ошибок={failed_count}")

    # Резервную копию делаем только если что-то реально изменилось
    if added_count or removed_count:
        try:
            _tb = time.time()
            backup_data = await db.export_backup_json()
            with open("backup_auto.json", "w", encoding="utf-8") as f:
                json.dump(backup_data, f, ensure_ascii=False, indent=2)
            logger.info(f"[sync] backup_auto.json сохранён за {time.time() - _tb:.1f} с")
        except Exception as e:
            logger.warning(f"Не удалось сохранить локальный backup_auto.json: {e}")

    return added_count, skipped_count, removed_count, failed_count


async def run_full_gdrive_sync(admin_chat_id: int):
    global _gdrive_sync_running
    _gdrive_sync_running = True
    try:
        await bot.send_message(
            admin_chat_id,
            "⏳ <b>Начинаю синхронизацию Google Диска с базой PostgreSQL...</b>\n"
            "Сканирую папки <code>итог</code>, <code>экзамен</code> и <code>материалы</code>. Пожалуйста, подождите."
        )
        subject_menus, file_targets = await gdrive.scan_entire_drive()
        added, skipped, removed, failed = await sync_drive_targets_to_db(
            admin_chat_id, subject_menus, file_targets
        )
        await db.log_admin_action(
            admin_chat_id, "gdrive_sync",
            f"added={added} skipped={skipped} removed={removed} failed={failed}"
        )
        await bot.send_message(
            admin_chat_id,
            f"✅ <b>Синхронизация с Google Диском завершена!</b>\n\n"
            f"• 📥 Новых/обновлённых файлов добавлено в PostgreSQL: <b>{added}</b>\n"
            f"• ⚡ Уже были в базе (пропущены): <b>{skipped}</b>\n"
            f"• 🗑️ Удалено устаревших файлов из базы: <b>{removed}</b>\n"
            f"• ⚠️ Ошибок: <b>{failed}</b>\n\n"
            f"🚀 Теперь все эти файлы раздаются студентам <b>мгновенно напрямую из PostgreSQL по Telegram file_id</b>!"
        )
    except Exception as e:
        logger.exception("Ошибка при полной синхронизации с Google Drive")
        await bot.send_message(
            admin_chat_id,
            f"❌ <b>Ошибка синхронизации с Google Диском:</b>\n<code>{html.escape(str(e))[:400]}</code>"
        )
    finally:
        _gdrive_sync_running = False


# ==================== 10. ХЕНДЛЕРЫ ====================

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
                               message.from_user.full_name, max(COURSES), "tibbi", "ru")
            user_course = max(COURSES)
        else:
            user_course = user_data['course']
        await message.answer(
            "👑 <b>Админ-меню.</b>\n💡 Для справки используйте /admin_help",
            reply_markup=get_reply_main_menu_keyboard(user_course, user_id))
        return

    if user_data:
        if not user_data['faculty']:
            await message.answer(
                "🎓 Пожалуйста, выберите ваш <b>факультет</b> для обновления профиля:",
                reply_markup=get_faculty_selection_keyboard())
            await state.set_state(RegistrationStates.waiting_for_missing_faculty)
            return

        fac_title = FACULTIES.get(user_data['faculty'], user_data['faculty'])
        await message.answer(
            f"✅ С возвращением! Вы учитесь на <b>{user_data['course']}-м курсе</b> ({fac_title}).\nВыберите раздел:",
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


# -------------------- Дозаполнение факультета (для старых пользователей) --------------------
@dp.message(RegistrationStates.waiting_for_missing_faculty)
async def process_missing_faculty(message: types.Message, state: FSMContext):
    faculty_key = next((k for k, v in FACULTIES.items() if message.text == v), None)
    if not faculty_key:
        return await message.answer("Пожалуйста, выберите факультет, используя кнопки ниже:",
                                    reply_markup=get_faculty_selection_keyboard())

    user_id = message.from_user.id
    await db.set_user_faculty(user_id, faculty_key)
    await state.clear()
    user_data = await db.get_user(user_id)
    course = user_data['course'] if user_data else 1
    await message.answer(
        f"✅ Факультет <b>{FACULTIES[faculty_key]}</b> сохранён! Выберите раздел:",
        reply_markup=get_reply_main_menu_keyboard(course, user_id))


# -------------------- Регистрация (Курс -> Факультет -> Группа -> Подтверждение) --------------------
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
    await message.answer("🏛️ Теперь выберите ваш <b>факультет</b>:",
                         reply_markup=get_faculty_selection_keyboard())
    await state.set_state(RegistrationStates.waiting_for_faculty)


@dp.message(RegistrationStates.waiting_for_faculty)
async def process_faculty_selection(message: types.Message, state: FSMContext):
    faculty_key = next((k for k, v in FACULTIES.items() if message.text == v), None)
    if not faculty_key:
        return await message.answer("Пожалуйста, выберите факультет, используя кнопки ниже:",
                                    reply_markup=get_faculty_selection_keyboard())

    await state.update_data(faculty=faculty_key)
    await message.answer("📝 Теперь выберите вашу языковую группу:",
                         reply_markup=get_group_selection_keyboard())
    await state.set_state(RegistrationStates.waiting_for_group)


@dp.message(RegistrationStates.waiting_for_group)
async def process_group_selection(message: types.Message, state: FSMContext):
    group_lang = next((k for k, v in GROUPS.items() if message.text == v), None)
    if not group_lang:
        return await message.answer("Пожалуйста, выберите группу, используя кнопки ниже:")

    data = await state.get_data()
    course = data.get('course')
    faculty = data.get('faculty')
    if not course or not faculty:
        await state.set_state(RegistrationStates.waiting_for_course)
        return await message.answer("Начнём сначала. Укажите ваш курс:", reply_markup=get_course_selection_keyboard())

    await state.update_data(group_lang=group_lang)
    await message.answer(
        f"⚠️ <b>Прежде чем сделать выбор — важное условие!</b>\n\n"
        f"Вы выбрали:\n"
        f"• Курс: <b>{course}-й</b>\n"
        f"• Факультет: <b>{FACULTIES[faculty]}</b>\n"
        f"• Группа: <b>{GROUPS[group_lang]}</b>\n\n"
        f"Ваше решение будет <b>окончательным</b>. После подтверждения данные <b>нельзя будет изменить самостоятельно</b>.\n\n"
        f"Пожалуйста, подойдите к выбору осознанно.",
        reply_markup=get_confirmation_keyboard())
    await state.set_state(RegistrationStates.waiting_for_confirmation)


@dp.message(RegistrationStates.waiting_for_confirmation)
async def process_confirmation(message: types.Message, state: FSMContext):
    user_id = message.from_user.id

    if message.text == "✅ Подтвердить выбор":
        data = await state.get_data()
        course = data.get('course')
        faculty = data.get('faculty')
        group_lang = data.get('group_lang')
        if not course or not faculty or not group_lang:
            await state.set_state(RegistrationStates.waiting_for_course)
            return await message.answer("Данные утеряны. Укажите ваш курс:",
                                        reply_markup=get_course_selection_keyboard())

        await db.save_user(user_id, message.from_user.username or 'NoUsername',
                           message.from_user.full_name, course, faculty, group_lang)
        await message.answer(
            "✅ <b>Регистрация завершена!</b>\n\nТеперь вам доступны материалы для вашего курса и факультета.",
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
        if not user_data['faculty'] and not is_admin(user_id):
            await callback.message.answer(
                "🎓 Пожалуйста, выберите ваш <b>факультет</b>:",
                reply_markup=get_faculty_selection_keyboard())
            await state.set_state(RegistrationStates.waiting_for_missing_faculty)
            return
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
    await state.update_data(exam_course=None)
    await state.set_state(AdminStates.waiting_for_exam_course)
    await message.answer("Выберите курс:", reply_markup=get_reply_exam_course_keyboard(max(COURSES)))


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
    data = await state.get_data()
    type_name = MATERIAL_TYPES.get(data.get('material_type'), "—")
    await state.update_data(materials_course=None)
    await state.set_state(AdminStates.waiting_for_materials_course)
    await message.answer(f"✅ Выбран тип: <b>{type_name}</b>.\n\nТеперь выберите курс:",
                         reply_markup=get_reply_materials_course_keyboard(max(COURSES)))


# -------------------- Выбор факультета и языка для админа --------------------
@dp.message(AdminStates.waiting_for_admin_faculty)
async def process_admin_faculty_selection(message: types.Message, state: FSMContext):
    faculty_key = next((k for k, v in FACULTIES.items() if message.text == v), None)
    if not faculty_key:
        return await message.answer("Пожалуйста, выберите факультет, используя кнопки ниже:",
                                    reply_markup=get_faculty_selection_keyboard(with_back_to_main=True))

    await state.update_data(admin_faculty_filter=faculty_key)
    await message.answer(
        f"✅ Выбран факультет: <b>{FACULTIES[faculty_key]}</b>.\nТеперь выберите язык материалов:",
        reply_markup=get_group_selection_keyboard(with_back_to_main=True))
    await state.set_state(AdminStates.waiting_for_admin_group)


@dp.message(AdminStates.waiting_for_admin_group)
async def process_admin_group_selection(message: types.Message, state: FSMContext):
    data = await state.get_data()
    target_section = data.get('target_section')

    group_lang = next((k for k, v in GROUPS.items() if message.text == v), None)
    if not group_lang:
        return await message.answer("Пожалуйста, выберите группу, используя кнопки ниже.",
                                    reply_markup=get_group_selection_keyboard(with_back_to_main=True))

    await state.update_data(admin_group_filter=group_lang)
    user_id = message.from_user.id
    limit = max(COURSES)

    if target_section == "exam":
        fac = FACULTIES.get(data.get('admin_faculty_filter', ''), '—')
        await message.answer(
            f"✅ Факультет: <b>{fac}</b> | Язык: <b>{GROUPS[group_lang]}</b>.\nВыберите курс:",
            reply_markup=get_reply_exam_course_keyboard(limit))
        await state.set_state(AdminStates.waiting_for_exam_course)

    elif target_section == "summary":
        fac = FACULTIES.get(data.get('admin_faculty_filter', ''), '—')
        await message.answer(
            f"✅ Факультет: <b>{fac}</b> | Язык: <b>{GROUPS[group_lang]}</b>.\nВыберите курс для итога:",
            reply_markup=get_reply_summary_course_keyboard(limit, user_id))
        await state.set_state(AdminStates.waiting_for_summary_course)

    elif target_section == "materials":
        await message.answer(f"✅ Выбран язык: <b>{GROUPS[group_lang]}</b>. Выберите тип материала:",
                             reply_markup=get_reply_materials_menu_keyboard())
        await state.set_state(AdminStates.waiting_for_materials_type)

    else:
        await state.clear()
        await message.answer("Ошибка секции. Возврат в главное меню.",
                             reply_markup=get_reply_main_menu_keyboard(limit, user_id))


# -------------------- Раздел «Экзамен» --------------------
@dp.message(F.text == "📚 Экзамен")
async def exam_menu(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user_data = await db.get_user(user_id)
    if not user_data:
        return await message.answer("Пожалуйста, завершите регистрацию через /start")

    await state.clear()

    if not is_admin(user_id):
        if not user_data['faculty']:
            await message.answer("🎓 Пожалуйста, укажите ваш факультет:",
                                 reply_markup=get_faculty_selection_keyboard())
            await state.set_state(RegistrationStates.waiting_for_missing_faculty)
            return

        user_course = user_data['course']
        user_faculty = user_data['faculty']
        user_group = user_data['group_lang']

        subjects_map = await resolve_subjects_for_menu("exam", user_faculty, user_course, user_group)
        await state.update_data(
            exam_course=user_course,
            exam_faculty=user_faculty,
            exam_group=user_group,
            current_subjects_map=subjects_map,
        )
        await message.answer(
            f"📚 <b>Экзамены</b> ({FACULTIES.get(user_faculty, user_faculty)}, {user_course}-й курс).\n"
            f"Выберите предмет:",
            reply_markup=get_reply_dynamic_subject_keyboard(list(subjects_map.keys()), user_id, section="exam"))
        await state.set_state(ExamStates.waiting_for_subject)
        return

    # Для админа: сначала спрашиваем факультет -> затем язык -> затем курс
    await state.update_data(target_section="exam")
    await message.answer(
        "👑 <b>Админ-меню Экзамен:</b> Выберите факультет:",
        reply_markup=get_faculty_selection_keyboard(with_back_to_main=True))
    await state.set_state(AdminStates.waiting_for_admin_faculty)


@dp.message(AdminStates.waiting_for_exam_course)
async def process_exam_course_selection(message: types.Message, state: FSMContext):
    match = re.match(r"^(\d+)-й курс$", message.text or "")
    if not match or int(match.group(1)) not in COURSES:
        return await message.answer("Пожалуйста, выберите курс, используя кнопки.")

    course_num = int(match.group(1))
    data = await state.get_data()
    faculty = data.get("admin_faculty_filter", "tibbi")
    group_lang = data.get("admin_group_filter", "ru")

    subjects_map = await resolve_subjects_for_menu("exam", faculty, course_num, group_lang)
    await state.update_data(
        exam_course=course_num,
        exam_faculty=faculty,
        exam_group=group_lang,
        current_subjects_map=subjects_map,
    )
    await message.answer(
        f"✅ Выбран <b>{course_num}-й курс</b> ({FACULTIES.get(faculty, faculty)}, {GROUPS.get(group_lang, group_lang)}).\n"
        f"Теперь выберите предмет:",
        reply_markup=get_reply_dynamic_subject_keyboard(list(subjects_map.keys()), message.from_user.id, section="exam"))
    await state.set_state(ExamStates.waiting_for_subject)


@dp.message(ExamStates.waiting_for_subject, F.text == "🔄 Обновить с Google Диска", ADMIN_ONLY)
async def refresh_exam_subjects_from_drive(message: types.Message, state: FSMContext):
    if not gdrive.is_enabled:
        return await message.answer("⚠️ Google Диск не подключён. Проверьте GDRIVE_ROOT_FOLDER_ID и credentials.json.")
    if _gdrive_sync_running:
        return await message.answer("⏳ Синхронизация с Google Диском уже выполняется, подождите...")

    data = await state.get_data()
    course_num = data.get("exam_course")
    faculty = data.get("exam_faculty", "tibbi")
    group_lang = data.get("exam_group", "ru")

    await run_full_gdrive_sync(message.chat.id)
    subjects_map = await resolve_subjects_for_menu("exam", faculty, course_num, group_lang)
    await state.update_data(current_subjects_map=subjects_map)
    await message.answer(
        f"✅ Список предметов в базе обновлён ({len(subjects_map)} шт.):",
        reply_markup=get_reply_dynamic_subject_keyboard(list(subjects_map.keys()), message.from_user.id, section="exam"))


@dp.message(ExamStates.waiting_for_subject)
async def exam_subject_handler(message: types.Message, state: FSMContext):
    if not message.text or is_command(message):
        return await message.answer("Пожалуйста, выберите предмет кнопкой или нажмите /cancel.")

    user_id = message.from_user.id
    user_data = await db.get_user(user_id)
    if not user_data:
        return await message.answer("Пожалуйста, завершите регистрацию через /start")

    admin = is_admin(user_id)
    data = await state.get_data()
    exam_course = data.get('exam_course')
    exam_faculty = data.get('exam_faculty') or user_data['faculty']
    exam_group = data.get('exam_group') or user_data['group_lang']
    subjects_map: Dict[str, Tuple[str, Optional[str]]] = data.get('current_subjects_map') or {}

    if not exam_course:
        await state.clear()
        return await message.answer("⚠️ Курс не выбран. Начните с «📚 Экзамен».",
                                    reply_markup=get_reply_main_menu_keyboard(user_data['course'], user_id))

    if not subjects_map:
        subjects_map = await resolve_subjects_for_menu("exam", exam_faculty, exam_course, exam_group)
        await state.update_data(current_subjects_map=subjects_map)

    if message.text not in subjects_map:
        return await message.answer("Пожалуйста, выберите предмет из предложенных кнопок меню.")

    subject_tag, drive_folder_id = subjects_map[message.text]

    await send_content_by_tag(
        chat_id=message.chat.id,
        tag=subject_tag,
        course_filter_value=exam_course,
        user_group=exam_group,
        user_faculty=exam_faculty,
        drive_folder_id=drive_folder_id,
    )

    if admin:
        await state.update_data(
            admin_course_filter=exam_course,
            admin_faculty_filter=exam_faculty,
            admin_group_filter=exam_group,
        )
        await message.answer(
            f"<b>АДМИН-ПАНЕЛЬ:</b> Управление <b>#{subject_tag}</b>\n"
            f"({FACULTIES.get(exam_faculty, exam_faculty)}, {exam_course} курс, {GROUPS.get(exam_group, exam_group)})",
            reply_markup=get_admin_menu(subject_tag))

    await message.answer(
        f"Курс <b>{exam_course}-й</b> ({FACULTIES.get(exam_faculty, exam_faculty)}) — выберите следующий предмет:",
        reply_markup=get_reply_dynamic_subject_keyboard(list(subjects_map.keys()), user_id, section="exam"))
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
        if not user_data['faculty']:
            await message.answer("🎓 Пожалуйста, укажите ваш факультет:",
                                 reply_markup=get_faculty_selection_keyboard())
            await state.set_state(RegistrationStates.waiting_for_missing_faculty)
            return

        if user_course > MAX_COURSE_FOR_SUMMARY:
            return await message.answer(
                f"Раздел «Итог» доступен только для 1–{MAX_COURSE_FOR_SUMMARY} курсов.",
                reply_markup=get_reply_main_menu_keyboard(user_course, user_id))

        fac_title = FACULTIES.get(user_data['faculty'], user_data['faculty'])
        await state.set_state(SummaryStates.waiting_for_section)
        return await message.answer(
            f"📋 <b>Итог</b> ({fac_title}, <b>{user_course}-й курс</b>).\n"
            f"Выберите раздел итогов (1–{SUMMARY_SECTIONS}):",
            reply_markup=get_reply_final_summary_keyboard(user_course))

    # Для админа: спрашиваем факультет -> язык -> курс
    await state.update_data(target_section="summary")
    await message.answer(
        "👑 <b>Админ-меню Итог:</b> Выберите факультет:",
        reply_markup=get_faculty_selection_keyboard(with_back_to_main=True))
    await state.set_state(AdminStates.waiting_for_admin_faculty)


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

    await state.update_data(summary_course=course_num)
    await state.set_state(SummaryStates.waiting_for_section)

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
    faculty_filter = (data.get('admin_faculty_filter') if admin else None) or user_data['faculty']

    await send_content_by_tag(
        chat_id=message.chat.id,
        tag=final_tag,
        course_filter_value=course,
        user_group=group_filter,
        user_faculty=faculty_filter,
    )

    if admin:
        await state.update_data(
            admin_course_filter=course,
            admin_faculty_filter=faculty_filter,
            admin_group_filter=group_filter,
        )
        await message.answer(
            f"<b>АДМИН-ПАНЕЛЬ:</b> Управление <b>#{final_tag}</b>\n"
            f"({FACULTIES.get(faculty_filter, faculty_filter)}, {course} курс, {GROUPS.get(group_filter, group_filter)})",
            reply_markup=get_admin_menu(final_tag))

    await message.answer("Выберите следующий раздел:",
                         reply_markup=get_reply_final_summary_keyboard(course))


# -------------------- Личный кабинет --------------------
@dp.message(F.text == "🚪 Личный кабинет")
async def personal_account(message: types.Message):
    user_data = await db.get_user(message.from_user.id)
    if not user_data:
        return await message.answer("Пожалуйста, завершите регистрацию через /start")

    fac_display = FACULTIES.get(user_data['faculty'], user_data['faculty'] or 'Не указан')
    account_info = (
        f"👤 <b>Личный кабинет</b>\n\n"
        f"• <b>Имя:</b> {html.escape(user_data['full_name'] or '—')}\n"
        f"• <b>Курс:</b> {user_data['course']}-й\n"
        f"• <b>Факультет:</b> {fac_display}\n"
        f"• <b>Группа:</b> {GROUPS.get(user_data['group_lang'], user_data['group_lang'])}\n"
        f"• <b>Зарегистрирован:</b> {str(user_data['registered_at'])[:10]}\n"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="Открыть study.tj", web_app=WebAppInfo(url=WEBAPP_URL))
    await message.answer(account_info, reply_markup=builder.as_markup())


# -------------------- Раздел «Материалы» (общий для всех факультетов) --------------------
@dp.message(F.text == "📂 Материалы")
async def materials_menu(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user_data = await db.get_user(user_id)
    if not user_data:
        return await message.answer("Пожалуйста, завершите регистрацию через /start")

    await state.clear()

    if is_admin(user_id):
        await state.update_data(target_section="materials", admin_faculty_filter=None)
        await message.answer("👑 <b>Админ-меню Материалы (общие для всех факультетов):</b>\nВыберите язык материалов:",
                             reply_markup=get_group_selection_keyboard(with_back_to_main=True))
        await state.set_state(AdminStates.waiting_for_admin_group)
        return

    await message.answer("Выберите тип материалов:", reply_markup=get_reply_materials_menu_keyboard())
    await state.set_state(MaterialsStates.waiting_for_material_type)


@dp.message(MaterialsStates.waiting_for_material_type, F.text.in_(list(MATERIAL_TYPES.values())))
async def handle_material_type_selection(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user_data = await db.get_user(user_id)
    if not user_data:
        return await message.answer("Пожалуйста, завершите регистрацию через /start")

    user_course = user_data['course']
    user_group = user_data['group_lang']
    type_key = next((k for k, v in MATERIAL_TYPES.items() if v == message.text), None)
    if not type_key:
        return

    subjects_map = await resolve_subjects_for_menu(type_key, "all", user_course, user_group)
    await state.update_data(
        material_type=type_key,
        materials_course=user_course,
        materials_group=user_group,
        current_subjects_map=subjects_map,
    )
    await message.answer(
        f"✅ Выбрано: <b>{message.text}</b>.\nТеперь выберите предмет для <b>{user_course}</b>-го курса:",
        reply_markup=get_reply_dynamic_subject_keyboard(list(subjects_map.keys()), user_id, section="materials"))
    await state.set_state(MaterialsStates.waiting_for_subject)


@dp.message(AdminStates.waiting_for_materials_type, F.text.in_(list(MATERIAL_TYPES.values())))
async def admin_select_material_type(message: types.Message, state: FSMContext):
    type_key = next((k for k, v in MATERIAL_TYPES.items() if v == message.text), None)
    if not type_key:
        return

    await state.update_data(material_type=type_key)
    await message.answer(f"✅ Выбран тип: <b>{message.text}</b>.\n\nТеперь выберите курс:",
                         reply_markup=get_reply_materials_course_keyboard(max(COURSES)))
    await state.set_state(AdminStates.waiting_for_materials_course)


@dp.message(AdminStates.waiting_for_materials_course, F.text.regexp(r"^\d+-й курс$"))
async def admin_select_course_for_materials(message: types.Message, state: FSMContext):
    data = await state.get_data()
    material_type = data.get('material_type')
    group_lang = data.get('admin_group_filter', 'ru')
    course_num = int(message.text.split("-")[0])

    if not material_type or course_num not in COURSES:
        await state.clear()
        return await message.answer(
            "⚠️ Ошибка состояния. Пожалуйста, начните сначала.",
            reply_markup=await main_menu_kb(message.from_user.id))

    subjects_map = await resolve_subjects_for_menu(material_type, "all", course_num, group_lang)
    await state.update_data(
        materials_course=course_num,
        materials_group=group_lang,
        current_subjects_map=subjects_map,
    )
    await message.answer(
        f"✅ Выбран <b>{course_num}-й курс</b> ({GROUPS.get(group_lang, group_lang)}). Теперь выберите предмет:",
        reply_markup=get_reply_dynamic_subject_keyboard(list(subjects_map.keys()), message.from_user.id, section="materials"))
    await state.set_state(MaterialsStates.waiting_for_subject)


@dp.message(MaterialsStates.waiting_for_subject, F.text == "🔄 Обновить с Google Диска", ADMIN_ONLY)
async def refresh_materials_subjects_from_drive(message: types.Message, state: FSMContext):
    if not gdrive.is_enabled:
        return await message.answer("⚠️ Google Диск не подключён. Проверьте GDRIVE_ROOT_FOLDER_ID и credentials.json.")
    if _gdrive_sync_running:
        return await message.answer("⏳ Синхронизация с Google Диском уже выполняется, подождите...")

    data = await state.get_data()
    material_type = data.get("material_type")
    course_num = data.get("materials_course")
    group_lang = data.get("materials_group") or data.get("admin_group_filter", "ru")

    await run_full_gdrive_sync(message.chat.id)
    subjects_map = await resolve_subjects_for_menu(material_type, "all", course_num, group_lang)
    await state.update_data(current_subjects_map=subjects_map)
    await message.answer(
        f"✅ Список предметов в базе обновлён ({len(subjects_map)} шт.):",
        reply_markup=get_reply_dynamic_subject_keyboard(list(subjects_map.keys()), message.from_user.id, section="materials"))


@dp.message(MaterialsStates.waiting_for_subject)
async def handle_materials_subject_selection(message: types.Message, state: FSMContext):
    if not message.text or is_command(message):
        return await message.answer("Пожалуйста, выберите предмет кнопкой или нажмите /cancel.")

    user_id = message.from_user.id
    user_data = await db.get_user(user_id)
    if not user_data:
        return await message.answer("Пожалуйста, завершите регистрацию через /start")

    admin = is_admin(user_id)
    data = await state.get_data()
    material_type = data.get('material_type')
    materials_course = data.get('materials_course')
    group_filter = (data.get('admin_group_filter') if admin else None) or user_data['group_lang']
    subjects_map: Dict[str, Tuple[str, Optional[str]]] = data.get('current_subjects_map') or {}

    if not material_type or not materials_course:
        await state.clear()
        return await message.answer("⚠️ Ошибка состояния. Начните с «📂 Материалы».",
                                    reply_markup=get_reply_main_menu_keyboard(user_data['course'], user_id))

    if not admin and materials_course > user_data['course']:
        return await message.answer("❌ У вас нет доступа к материалам этого курса.")

    if not subjects_map:
        subjects_map = await resolve_subjects_for_menu(material_type, "all", materials_course, group_filter)
        await state.update_data(current_subjects_map=subjects_map)

    if message.text not in subjects_map:
        return await message.answer("Пожалуйста, выберите предмет из предложенных кнопок меню.")

    final_tag, drive_folder_id = subjects_map[message.text]

    await send_content_by_tag(
        chat_id=message.chat.id,
        tag=final_tag,
        course_filter_value=materials_course,
        user_group=group_filter,
        user_faculty=None,
        drive_folder_id=drive_folder_id,
    )

    if admin:
        await state.update_data(
            admin_course_filter=materials_course,
            admin_faculty_filter=None,
            admin_group_filter=group_filter,
        )
        await message.answer(
            f"<b>АДМИН-ПАНЕЛЬ:</b> Управление <b>#{final_tag}</b>\n"
            f"(Общие материалы, {materials_course} курс, {GROUPS.get(group_filter, group_filter)})",
            reply_markup=get_admin_menu(final_tag))

    type_name = MATERIAL_TYPES.get(material_type, "Материалы")
    await message.answer(
        f"{type_name} — Курс <b>{materials_course}-й</b>.\n"
        f"Выберите следующий предмет или вернитесь назад:",
        reply_markup=get_reply_dynamic_subject_keyboard(list(subjects_map.keys()), user_id, section="materials"))
    await state.set_state(MaterialsStates.waiting_for_subject)


# -------------------- УПРАВЛЕНИЕ GOOGLE ДИСКОМ И БЭКАПАМИ БД (АДМИН) --------------------
def _gdrive_admin_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Синхронизировать ВСЁ с Google Диска в БД", callback_data="gdrive_sync_all")
    builder.button(text="📁 Досоздать недостающие папки (1-kurs...)", callback_data="gdrive_init_basic")
    builder.button(text="📚 Создать папки + базовые предметы", callback_data="gdrive_init_full")
    builder.button(text="💾 Скачать бэкап базы PostgreSQL (JSON)", callback_data="db_backup_export")
    builder.button(text="📥 Восстановить базу из JSON", callback_data="db_backup_import")
    builder.adjust(1)
    return builder.as_markup()


@dp.message(Command("gdrive"), ADMIN_ONLY)
@dp.message(F.text == "☁️ Google Диск", ADMIN_ONLY)
async def cmd_gdrive_panel(message: types.Message):
    status_emoji = "✅ Подключён" if gdrive.is_enabled else "⚠️ Не настроен"
    creds_exists = "✅ Найден" if os.path.exists(GDRIVE_CREDENTIALS_FILE) else f"❌ Нет файла ({GDRIVE_CREDENTIALS_FILE})"
    folder_info = f"<code>{GDRIVE_ROOT_FOLDER_ID}</code>" if GDRIVE_ROOT_FOLDER_ID else "❌ Не указан в .env"

    text = (
        f"☁️ <b>Интеграция с Google Диском и Базой PostgreSQL</b>\n\n"
        f"• <b>Статус Google Диска:</b> {status_emoji}\n"
        f"• <b>Ключ сервисного аккаунта:</b> {creds_exists}\n"
        f"• <b>ID корневой папки:</b> {folder_info}\n\n"
        f"⚡ <b>Как это работает:</b>\n"
        f"1. Вы загружаете или обновляете файлы в папках на Google Диске.\n"
        f"2. Нажимаете <b>«🔄 Синхронизировать ВСЁ с Google Диска в БД»</b> (или <code>/sync_drive</code>).\n"
        f"3. Бот загружает новые файлы из Диска в Telegram, получает их <code>file_id</code> и сохраняет в <b>PostgreSQL</b>.\n"
        f"4. После этого студенты получают файлы <b>мгновенно напрямую из PostgreSQL</b> (без обращений к Google Диску)!"
    )
    await message.answer(text, reply_markup=_gdrive_admin_kb())


@dp.message(Command("sync_drive"), ADMIN_ONLY)
async def cmd_sync_drive(message: types.Message):
    if not gdrive.is_enabled:
        return await message.answer("⚠️ Google Диск не настроен (проверьте credentials.json и GDRIVE_ROOT_FOLDER_ID).")
    if _gdrive_sync_running:
        return await message.answer("⏳ Синхронизация уже выполняется, пожалуйста, подождите...")
    task = asyncio.create_task(run_full_gdrive_sync(message.chat.id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


@dp.callback_query(F.data == "gdrive_sync_all", ADMIN_ONLY)
async def cb_gdrive_sync_all(callback: types.CallbackQuery):
    if not gdrive.is_enabled:
        return await callback.answer("⚠️ Google Диск не настроен в .env", show_alert=True)
    if _gdrive_sync_running:
        return await callback.answer("⏳ Синхронизация уже идёт, подождите окончания.", show_alert=True)
    await callback.answer("🚀 Синхронизация запущена!")
    task = asyncio.create_task(run_full_gdrive_sync(callback.message.chat.id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


@dp.callback_query(F.data.in_({"gdrive_init_basic", "gdrive_init_full"}), ADMIN_ONLY)
async def cb_gdrive_init(callback: types.CallbackQuery):
    if not gdrive.is_enabled:
        return await callback.answer("⚠️ Google Диск не настроен в .env", show_alert=True)

    with_subjects = callback.data == "gdrive_init_full"
    await callback.answer("⏳ Проверяю и досоздаю папки на Google Диске...")
    await callback.message.answer(
        "⏳ <b>Проверка и создание недостающих папок на Google Диске запущены...</b>\n"
        "Уже существующие папки (например, <code>Рейтинг 1 (2026-10)</code>) не дублируются."
    )
    try:
        ops = await gdrive.init_base_structure(create_default_subjects=with_subjects)
        await callback.message.answer(
            f"✅ <b>Структура папок на Google Диске готова!</b>\n"
            f"Проверено/создано веток разделов: <b>{ops}</b>.\n"
            f"После загрузки файлов в папки нажмите «🔄 Синхронизировать ВСЁ с Google Диска в БД»."
        )
    except Exception as e:
        logger.exception("Ошибка инициализации папок Google Drive")
        await callback.message.answer(
            f"❌ <b>Ошибка при создании папок на Google Диске:</b>\n"
            f"<code>{html.escape(str(e))[:400]}</code>\n\n"
            f"Проверьте, что вы дали сервисному аккаунту доступ <b>«Редактор»</b> к корневой папке."
        )


@dp.callback_query(F.data == "db_backup_export", ADMIN_ONLY)
@dp.message(Command("backup_db"), ADMIN_ONLY)
async def handle_db_backup_export(event: types.Message | types.CallbackQuery):
    chat_id = event.message.chat.id if isinstance(event, types.CallbackQuery) else event.chat.id
    if isinstance(event, types.CallbackQuery):
        await event.answer("⏳ Выгружаю данные из PostgreSQL в JSON...")
    data = await db.export_backup_json()
    raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    fname = f"medbot_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    doc = BufferedInputFile(raw, filename=fname)
    await bot.send_document(
        chat_id,
        document=doc,
        caption=(
            f"💾 <b>Резервная копия базы PostgreSQL</b>\n"
            f"👥 Пользователей: <b>{len(data['users'])}</b>\n"
            f"📁 Материалов (file_id): <b>{len(data['materials'])}</b>\n"
            f"📚 Разделов предметов: <b>{len(data.get('drive_subjects', []))}</b>\n\n"
            f"Этот JSON выгружен напрямую из вашей базы PostgreSQL."
        ),
    )


@dp.callback_query(F.data == "db_backup_import", ADMIN_ONLY)
async def cb_db_backup_import(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_restore_file)
    await callback.message.answer(
        "📥 <b>Восстановление базы данных в PostgreSQL</b>\n\n"
        "Отправьте сюда JSON-файл резервной копии (`medbot_backup_....json` или `backup_auto.json`).\n"
        "Для отмены — /cancel."
    )
    await callback.answer()


@dp.message(AdminStates.waiting_for_restore_file, F.document, ADMIN_ONLY)
async def process_db_restore_file(message: types.Message, state: FSMContext):
    doc = message.document
    if not doc.file_name or not doc.file_name.endswith(".json"):
        return await message.answer("⚠️ Пожалуйста, отправьте файл в формате <code>.json</code> или /cancel.")

    buf = io.BytesIO()
    await bot.download(doc, destination=buf)
    try:
        payload = json.loads(buf.getvalue().decode("utf-8"))
        u_cnt, m_cnt = await db.import_backup_json(payload)
        await state.clear()
        await message.answer(
            f"✅ <b>База PostgreSQL успешно восстановлена!</b>\n"
            f"• Пользователей обработано: <b>{u_cnt}</b>\n"
            f"• Новых материалов добавлено: <b>{m_cnt}</b>",
            reply_markup=await main_menu_kb(message.from_user.id),
        )
    except Exception as e:
        logger.exception("Ошибка импорта JSON бэкапа")
        await message.answer(f"❌ Ошибка чтения файла бэкапа: <code>{html.escape(str(e))[:300]}</code>")


# -------------------- АДМИН: INLINE-МЕНЮ ТЕГОВ (/admin_menu) --------------------
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
    if section == "mat":
        await safe_edit_text(callback.message, "🛠️ <b>АДМИН-ПАНЕЛЬ (Материалы):</b> Выберите язык.",
                             kb_admin_langs("mat", "all"))
    else:
        await safe_edit_text(callback.message, "🛠️ <b>АДМИН-ПАНЕЛЬ:</b> Выберите факультет.",
                             kb_admin_faculties(section))
    await callback.answer()


@dp.callback_query(F.data.startswith("admfac__"), ADMIN_ONLY)
async def admin_menu_faculty(callback: types.CallbackQuery):
    _, section, faculty = callback.data.split("__")
    await safe_edit_text(callback.message, "🛠️ <b>АДМИН-ПАНЕЛЬ:</b> Выберите язык.",
                         kb_admin_langs(section, faculty))
    await callback.answer()


@dp.callback_query(F.data.startswith("admlang__"), ADMIN_ONLY)
async def admin_menu_lang(callback: types.CallbackQuery):
    _, section, faculty, group_lang = callback.data.split("__")
    await safe_edit_text(callback.message, "🛠️ <b>АДМИН-ПАНЕЛЬ:</b> Выберите курс.",
                         kb_admin_courses(section, faculty, group_lang))
    await callback.answer()


@dp.callback_query(F.data.startswith("admcourse__"), ADMIN_ONLY)
async def admin_menu_course(callback: types.CallbackQuery):
    _, section, faculty, group_lang, course = callback.data.split("__")
    if not course.isdigit():
        return await callback.answer()
    kb = await kb_admin_tags(section, faculty, group_lang, int(course))
    await safe_edit_text(callback.message, "🛠️ <b>АДМИН-ПАНЕЛЬ:</b> Выберите предмет (тег).", kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("select_admin_tag__"), ADMIN_ONLY)
async def show_admin_menu_for_tag(callback: types.CallbackQuery, state: FSMContext):
    _, faculty, group_lang, course_str, tag = callback.data.split("__", 4)
    fac_val = None if faculty == "all" else faculty
    await state.update_data(
        admin_faculty_filter=fac_val,
        admin_group_filter=group_lang,
        admin_course_filter=int(course_str),
    )
    await safe_edit_text(
        callback.message,
        f"<b>АДМИН-ПАНЕЛЬ:</b> Действия для раздела <b>#{tag}</b>\n"
        f"Факультет: {FACULTIES.get(fac_val, 'Общий')} | Курс: {course_str} | Язык: {GROUPS.get(group_lang, group_lang)}",
        get_admin_menu(tag),
    )
    await callback.answer()


# -------------------- РАССЫЛКА --------------------
def _broadcast_faculty_kb():
    builder = ReplyKeyboardBuilder()
    for value in FACULTIES.values():
        builder.button(text=value)
    builder.button(text="ВСЕМ ФАКУЛЬТЕТАМ")
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)


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
    await message.answer("👑 <b>РАССЫЛКА:</b> Выберите целевой факультет:",
                         reply_markup=_broadcast_faculty_kb())
    await state.set_state(AdminStates.waiting_for_broadcast_faculty)


@dp.message(F.text == "📢 Рассылка", ADMIN_ONLY)
async def admin_broadcast_button(message: types.Message, state: FSMContext):
    await cmd_start_broadcast(message, state)


@dp.message(AdminStates.waiting_for_broadcast_faculty)
async def process_broadcast_faculty_filter(message: types.Message, state: FSMContext):
    text = message.text or ""
    if text == "ВСЕМ ФАКУЛЬТЕТАМ":
        fac_key = 'all'
    else:
        fac_key = next((k for k, v in FACULTIES.items() if v == text), None)
    if not fac_key:
        return await message.answer("Пожалуйста, выберите факультет из предложенных кнопок.")

    await state.update_data(broadcast_faculty=fac_key)
    await message.answer(f"✅ Факультет выбран ({text}). Теперь выберите языковую группу:",
                         reply_markup=_broadcast_group_kb())
    await state.set_state(AdminStates.waiting_for_broadcast_filter)


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
    course_num = 0
    if text != "ВСЕМ КУРСАМ":
        match = re.match(r"^Курс (\d+)$", text)
        if not match or int(match.group(1)) not in COURSES:
            return await message.answer("Пожалуйста, выберите курс из предложенных кнопок.")
        course_num = int(match.group(1))

    await state.update_data(broadcast_course=course_num)
    await message.answer(
        "📝 <b>Отправьте сообщение для рассылки</b> (текст, фото, видео, файл…).\n\n"
        "<i>Форматирование Telegram сохранится — сообщение копируется как есть.</i>\n"
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
    faculty = data.get('broadcast_faculty', 'all')

    user_ids = await db.get_users_for_broadcast(course, group_lang, faculty)
    if not user_ids:
        await state.clear()
        return await message.answer(
            f"❌ Не найдено пользователей по фильтрам (Факультет: {FACULTIES.get(faculty, 'Все')}, "
            f"Курс: {course or 'Все'}, Группа: {GROUPS.get(group_lang, 'Все')}).",
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
        f"Фильтры: факультет — {FACULTIES.get(faculty, 'все')}, "
        f"курс — {course or 'все'}, группа — {GROUPS.get(group_lang, 'все')}.\n\n"
        f"Отправить это сообщение?",
        reply_markup=builder.as_markup())
    await state.set_state(AdminStates.waiting_for_broadcast_confirm)


@dp.message(AdminStates.waiting_for_broadcast_content)
async def process_broadcast_unsupported(message: types.Message):
    await message.answer("⚠️ Неподдерживаемый тип контента. Отправьте текст, фото, видео, файл или аудио.")


async def _copy_with_retry(target_id: int, src_chat_id: int, src_message_id: int):
    await with_retry(lambda: bot.copy_message(chat_id=target_id, from_chat_id=src_chat_id,
                                              message_id=src_message_id))


async def run_broadcast(admin_id: int, src_chat_id: int, src_message_id: int,
                        course: int, group_lang: str, faculty: str = 'all'):
    global _broadcast_running
    _broadcast_running = True
    sent = blocked = failed = 0
    try:
        user_ids = await db.get_users_for_broadcast(course, group_lang, faculty)
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
            await asyncio.sleep(0.05)

        await db.log_admin_action(
            admin_id, "broadcast",
            f"faculty={faculty} course={course} group={group_lang} sent={sent} blocked={blocked} failed={failed}"
        )
        await bot.send_message(
            admin_id,
            f"✅ <b>Рассылка завершена!</b>\n"
            f"Доставлено: <b>{sent}</b> из {total}\n"
            f"Бот заблокирован пользователями: {blocked}\n"
            f"Ошибки: {failed}\n"
            f"Фильтры: факультет {FACULTIES.get(faculty, 'все')}, курс {course or 'все'}, "
            f"группа {GROUPS.get(group_lang, 'все')}.")
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
    course = data.get('broadcast_course', 0)
    group_lang = data.get('broadcast_group', 'all')
    faculty = data.get('broadcast_faculty', 'all')
    await state.clear()

    if not src_chat_id or not src_message_id:
        return await callback.answer("Данные рассылки утеряны. Начните заново.", show_alert=True)

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await callback.answer("Рассылка запущена")

    task = asyncio.create_task(run_broadcast(callback.from_user.id, src_chat_id, src_message_id, course, group_lang, faculty))
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


# -------------------- ЗАГРУЗКА МАТЕРИАЛОВ ЧЕРЕЗ TELEGRAM (АДМИН) --------------------
@dp.callback_query(F.data.startswith("upload__"), ADMIN_ONLY)
async def start_upload_fsm(callback: types.CallbackQuery, state: FSMContext):
    target_tag = callback.data.split("__", 1)[1]
    data = await state.get_data()
    group_filter = data.get('admin_group_filter')
    faculty_filter = data.get('admin_faculty_filter')
    course_filter = data.get('admin_course_filter')

    await state.update_data(
        target_tag=target_tag,
        group_filter=group_filter,
        faculty_filter=faculty_filter,
        course_filter=course_filter,
        uploaded_count=0,
    )
    await state.set_state(AdminStates.waiting_for_content)

    parts = []
    if faculty_filter:
        parts.append(f"факультет {FACULTIES.get(faculty_filter, faculty_filter)}")
    else:
        parts.append("все факультеты")
    if course_filter:
        parts.append(f"{course_filter} курс")
    if group_filter:
        parts.append(f"группа {GROUPS.get(group_filter, group_filter)}")
    filter_info = ", ".join(parts)

    await callback.message.answer(
        f"✅ Режим загрузки через Telegram активирован для <b>#{target_tag}</b> ({filter_info}).\n"
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
    faculty_filter = data.get('faculty_filter')
    course_filter = data.get('course_filter')

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
        tag=target_tag,
        type_=entry["type"],
        file_id=entry["file_id"],
        file_name=entry["file_name"],
        caption=entry["caption"],
        course=course_filter,
        faculty=faculty_filter,
        group_lang=group_filter,
        uploaded_by=user_id,
        source="telegram",
    )
    await db.log_admin_action(
        user_id, "upload",
        f"tag={target_tag} id={material_id} type={entry['type']} fac={faculty_filter} grp={group_filter}"
    )

    count = data.get('uploaded_count', 0) + 1
    await state.update_data(uploaded_count=count)

    await message.answer(
        f"✅ Сохранено в <b>#{target_tag}</b>. ID: {material_id}, тип: {entry['type']}.\n"
        f"За эту сессию: <b>{count}</b>. Отправьте ещё или /done.")


# -------------------- УДАЛЕНИЕ МАТЕРИАЛОВ (АДМИН) --------------------
MAX_DELETE_LIST = 40


async def render_delete_list(tag: str, faculty: Optional[str] = None,
                             group_lang: Optional[str] = None) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    all_materials = await db.get_materials_by_tag(tag)
    materials = []
    for m in all_materials:
        fac_ok = faculty is None or m['faculty'] is None or m['faculty'] == faculty
        grp_ok = group_lang is None or m['group_lang'] is None or m['group_lang'] == group_lang
        if fac_ok and grp_ok:
            materials.append(m)

    if not materials:
        return f"❌ В разделе <b>#{tag}</b> нет сохранённых в БД материалов.", None

    shown = materials[:MAX_DELETE_LIST]
    text = f"🗑️ <b>УДАЛЕНИЕ:</b> раздел <b>#{tag}</b> — всего {len(materials)}\n\n"
    for m in shown:
        if m['type'] != "text" and m['file_name']:
            display = html.escape(str(m['file_name'])[:40])
        else:
            display = plain_preview(m['caption']) or "Текст"
        filters = []
        if m['source'] == 'gdrive':
            filters.append("☁️ GDrive→БД")
        else:
            filters.append("📱 TG")
        if m['faculty']:
            filters.append(FACULTY_DRIVE_NAMES.get(m['faculty'], m['faculty']))
        if m['course']:
            filters.append(f"к{m['course']}")
        if m['group_lang']:
            filters.append(f"г{m['group_lang']}")
        flt = f" [{', '.join(filters)}]"
        text += f"<b>ID {m['id']}:</b> <code>{m['type'].upper()}</code> {display}{flt}\n"
    if len(materials) > len(shown):
        text += f"\n… и ещё {len(materials) - len(shown)}\n"
    text += "\nНажмите кнопку с ID, чтобы удалить материал из базы PostgreSQL."

    buttons = [InlineKeyboardButton(text=f"🗑 {m['id']}", callback_data=f"delmat__{m['id']}__{tag}") for m in shown]
    rows = _inline_rows(buttons, 4)
    rows.append([InlineKeyboardButton(text="🗑 Очистить ВСЁ в этом разделе", callback_data=f"delall__{tag}")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data.startswith("delete_indexed__"), ADMIN_ONLY)
async def start_indexed_delete(callback: types.CallbackQuery, state: FSMContext):
    tag = callback.data.split("__", 1)[1]
    data = await state.get_data()
    text, markup = await render_delete_list(tag, data.get('admin_faculty_filter'), data.get('admin_group_filter'))
    await callback.message.answer(text, reply_markup=markup)
    await callback.answer()


@dp.callback_query(F.data.startswith("delmat__"), ADMIN_ONLY)
async def delete_single_material(callback: types.CallbackQuery, state: FSMContext):
    _, mat_id, tag = callback.data.split("__", 2)
    deleted = await db.delete_material(int(mat_id))
    if deleted:
        await db.log_admin_action(callback.from_user.id, "delete_material", f"tag={tag} id={mat_id}")
    await callback.answer("✅ Удалено" if deleted else "Материал уже удалён")
    data = await state.get_data()
    text, markup = await render_delete_list(tag, data.get('admin_faculty_filter'), data.get('admin_group_filter'))
    await safe_edit_text(callback.message, text, markup)


@dp.callback_query(F.data.startswith("delall__"), ADMIN_ONLY)
async def delete_all_ask(callback: types.CallbackQuery):
    tag = callback.data.split("__", 1)[1]
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, удалить всё", callback_data=f"delall_yes__{tag}")
    builder.button(text="↩️ Отмена", callback_data=f"delall_no__{tag}")
    builder.adjust(1)
    await safe_edit_text(
        callback.message,
        f"⚠️ <b>Удалить все материалы из БД для раздела #{tag}?</b>\nЭто действие необратимо.",
        builder.as_markup())
    await callback.answer()


@dp.callback_query(F.data.startswith("delall_yes__"), ADMIN_ONLY)
async def delete_all_confirm(callback: types.CallbackQuery, state: FSMContext):
    tag = callback.data.split("__", 1)[1]
    data = await state.get_data()
    deleted = await db.delete_materials_by_tag(tag, data.get('admin_faculty_filter'), data.get('admin_group_filter'))
    await db.log_admin_action(callback.from_user.id, "delete_all", f"tag={tag} count={deleted}")
    await safe_edit_text(callback.message, f"✅ Удалено материалов из <b>#{tag}</b>: {deleted}.")
    await callback.answer()


@dp.callback_query(F.data.startswith("delall_no__"), ADMIN_ONLY)
async def delete_all_cancel(callback: types.CallbackQuery, state: FSMContext):
    tag = callback.data.split("__", 1)[1]
    data = await state.get_data()
    text, markup = await render_delete_list(tag, data.get('admin_faculty_filter'), data.get('admin_group_filter'))
    await safe_edit_text(callback.message, text, markup)
    await callback.answer("Отменено")


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
        fac = FACULTIES.get(u['faculty'], "—")
        group = GROUPS.get(u['group_lang'], "—")
        response += (
            f"<b>{i}.</b> {full_name}\n"
            f"   📱 {username}\n"
            f"   🆔 <code>{u['user_id']}</code>\n"
            f"   📚 {course} | 🏛️ {fac} | 🌐 {group}\n"
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
            f"🏛️ <b>Факультет:</b> {FACULTIES.get(user_data['faculty'], '—')}\n"
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


# -------------------- АДМИН: УПРАВЛЕНИЕ ПРОФИЛЕМ ПОЛЬЗОВАТЕЛЯ --------------------
@dp.message(Command("setcourse"), ADMIN_ONLY)
async def cmd_set_course(message: types.Message):
    args = message.text.split()
    fac_codes = "|".join(FACULTIES.keys())
    usage = (
        f"📝 <b>Использование:</b>\n"
        f"<code>/setcourse USER_ID КУРС [ru|tj] [{fac_codes}]</code>\n\n"
        f"<b>Пример:</b>\n<code>/setcourse 123456789 4 ru tibbi</code>"
    )
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

    faculty = args[4].lower() if len(args) > 4 else None
    if faculty and faculty not in FACULTIES:
        return await message.answer(f"❌ Факультет должен быть одним из: <code>{fac_codes}</code>.")

    if await db.set_user_profile(target_id, course, group_lang, faculty) == 0:
        return await message.answer("❌ Пользователь не найден в базе.")

    await db.log_admin_action(
        message.from_user.id, "setcourse",
        f"user={target_id} course={course} group={group_lang} faculty={faculty}"
    )
    details = [f"{course}-й курс"]
    if group_lang:
        details.append(f"группа {GROUPS[group_lang]}")
    if faculty:
        details.append(f"факультет {FACULTIES[faculty]}")

    await message.answer(f"✅ Пользователю <code>{target_id}</code> установлено: {', '.join(details)}.")
    try:
        await bot.send_message(
            target_id,
            f"ℹ️ Администратор обновил ваш профиль: <b>{', '.join(details)}</b>.\n"
            f"Нажмите /start, чтобы обновить меню."
        )
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

    text += "🏛️ <b>По факультетам:</b>\n"
    for row in s['by_faculty']:
        fname = FACULTIES.get(row['faculty'], row['faculty'] or "Не указан")
        text += f"  • {fname}: {row['cnt']}\n"

    text += "\n🎓 <b>По курсам:</b>\n"
    for row in s['by_course']:
        text += f"  • {row['course']}-й курс: {row['cnt']}\n"

    text += "\n🌍 <b>По группам:</b>\n"
    for row in s['by_group']:
        text += f"  • {GROUPS.get(row['group_lang'], row['group_lang'])}: {row['cnt']}\n"

    text += (
        f"\n📁 <b>Материалов в PostgreSQL всего:</b> {s['total_materials']}\n"
        f"  • ☁️ синхронизировано с Google Диска: {s['gdrive_materials']}\n"
        f"  • 📱 загружено через Telegram: {s['tg_materials']}\n"
    )
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
        return await message.answer("📭 В базе PostgreSQL нет сохранённых материалов.")

    grouped: Dict[str, List[str]] = {}
    for r in rows:
        flt = []
        flt.append("☁️GDrive→БД" if r['source'] == 'gdrive' else "📱TG")
        if r['faculty']:
            flt.append(FACULTY_DRIVE_NAMES.get(r['faculty'], r['faculty']))
        if r['course']:
            flt.append(f"к{r['course']}")
        if r['group_lang']:
            flt.append(f"г{r['group_lang']}")
        grouped.setdefault(r['tag'], []).append(f"{r['type']} [{', '.join(flt)}]: {r['cnt']} шт.")

    text = "📁 <b>МАТЕРИАЛЫ В БАЗЕ POSTGRESQL</b>\n"
    for tag in sorted(grouped):
        text += f"\n<b>#{tag.upper()}:</b>\n"
        for line in grouped[tag]:
            text += f"  • {line}\n"
    await send_long(message.chat.id, text)


@dp.message(Command("admin_help"), ADMIN_ONLY)
async def cmd_admin_help(message: types.Message):
    await message.answer("""🛠️ <b>АДМИН-КОМАНДЫ</b>

<b>☁️ Google Диск и Синхронизация с PostgreSQL:</b>
- Кнопка "☁️ Google Диск" или <code>/gdrive</code> — меню Google Диска и бэкапов.
- <code>/sync_drive</code> — синхронизировать новые/изменённые файлы с Google Диска в PostgreSQL (получить Telegram <code>file_id</code>).
- <code>/backup_db</code> — выгрузить резервную копию всей базы PostgreSQL в JSON-файл.

<b>📁 Загрузка через Telegram:</b>
- Перейдите в "📚 Экзамен", "📋 Итог" или "📂 Материалы" (или <code>/admin_menu</code>) → выберите факультет, язык, курс и предмет → нажмите <b>➕ Загрузить через ТГ</b>.

<b>📢 Рассылка:</b>
- Кнопка "📢 Рассылка" или <code>/broadcast</code> (по факультетам, языкам и курсам)

<b>📊 Статистика:</b>
- Кнопка "📊 Статистика" или <code>/admin_stats</code>
- <code>/admin_materials</code> — список всех материалов в PostgreSQL

<b>🚫 Блокировка:</b>
- Кнопка "🚫 Заблокированные" или <code>/blocklist</code>
- <code>/block USER_ID [причина]</code> — Заблокировать
- <code>/unblock USER_ID</code> — Разблокировать

<b>🎓 Профиль и курсы:</b>
- <code>/setcourse USER_ID КУРС [ru|tj] [tibbi|stom|ped|pharm|ntch]</code> — Изменить курс/группу/факультет
- <code>/promote_all</code> — Перевести всех на курс выше

<b>↩️ Общее:</b> <code>/cancel</code> — отмена текущего действия""")


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
    faculty = FACULTIES.get(user_data['faculty'], "Не указан") if user_data else "Не указан"
    group = GROUPS.get(user_data['group_lang'], "Не указана") if user_data else "Не указана"

    user_info = (
        f"📩 <b>НОВОЕ СООБЩЕНИЕ ОТ ПОЛЬЗОВАТЕЛЯ</b>\n\n"
        f"👤 <b>Имя:</b> {html.escape(message.from_user.full_name)}\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
        f"📱 <b>Username:</b> {'@' + html.escape(username) if username else 'Нет username'}\n"
        f"📚 <b>Курс:</b> {course}\n"
        f"🏛️ <b>Факультет:</b> {faculty}\n"
        f"🌍 <b>Группа:</b> {group}"
    )

    reply_kb = InlineKeyboardBuilder()
    reply_kb.button(text="✉️ Ответить пользователю", callback_data=f"reply_to_user__{user_id}")

    success = 0
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, user_info)
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


# -------------------- ЗАПАСНЫЕ ОБРАБОТЧИКИ --------------------
@dp.message(StateFilter(
    ExamStates.waiting_for_subject,
    SummaryStates.waiting_for_section,
    MaterialsStates.waiting_for_material_type,
    MaterialsStates.waiting_for_subject,
    AdminStates.waiting_for_materials_type,
    AdminStates.waiting_for_materials_course,
    AdminStates.waiting_for_summary_course,
    AdminStates.waiting_for_broadcast_confirm,
    AdminStates.waiting_for_restore_file,
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

    logger.info(
        "Бот запущен (админы: %s, Google Drive: %s)...",
        sorted(ADMIN_IDS),
        "ВКЛ" if gdrive.is_enabled else "ВЫКЛ",
    )
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