import os
import json
import asyncio
from dotenv import load_dotenv

# Импорты для FSM
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from aiogram.types import WebAppInfo, ContentType 
from aiogram import F 


# -------------------- 1. НАСТРОЙКА И КОНСТАНТЫ --------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен в .env")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

CONTENT_FILE = "content.json"
# 🚨 ВАЖНО: Замените на ВАШ Telegram ID 
ADMIN_IDS = [6720999592, 6520890849]
# ADMIN_IDS = [int(id.strip()) for id in ADMIN_IDS_STR.split(',') if id.strip().isdigit()] if ADMIN_IDS_STR else []


# --- 2. FSM СОСТОЯНИЯ ---
class AdminStates(StatesGroup):
    """Состояния для загрузки контента администратором."""
    waiting_for_content = State()


# --- 3. ЗАГРУЗКА / СОХРАНЕНИЕ КОНТЕНТА ---
def load_content():
    if not os.path.exists(CONTENT_FILE):
        return {}
    with open(CONTENT_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_content(data):
    with open(CONTENT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

content_data = load_content()


# --- 4. ФУНКЦИИ КЛАВИАТУР (ОСТАВЛЕНЫ БЕЗ ИЗМЕНЕНИЙ) ---
def get_reply_main_menu_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.button(text="📚 Экзамен")
    builder.button(text="📋 Итог")
    builder.button(text="📂 Материалы")
    builder.button(text="🚪 Личный кабинет")
    builder.adjust(2, 2)
    return builder.as_markup(resize_keyboard=True)

def get_reply_exam_menu_keyboard():
    builder = ReplyKeyboardBuilder()
    for i in range(1, 7):
        builder.button(text=f"{i}-курс")
    builder.button(text="🔙 Главное меню")
    builder.adjust(3, 3, 1)
    return builder.as_markup(resize_keyboard=True)

def get_reply_materials_menu_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.button(text="📖 Лекции")
    builder.button(text="🔬 Практика")
    builder.button(text="🎥 Видео")
    builder.button(text="🔙 Главное меню")
    builder.adjust(3, 1)
    return builder.as_markup(resize_keyboard=True)

def get_reply_summary_course_keyboard():
    builder = ReplyKeyboardBuilder()
    for i in range(1, 4): 
        builder.button(text=f"Итог - {i} курс") 
    builder.button(text="🔙 Главное меню") 
    builder.adjust(3, 1) 
    return builder.as_markup(resize_keyboard=True)

def get_reply_final_summary_keyboard(course_num: int):
    builder = ReplyKeyboardBuilder()
    for i in range(1, 5):
        builder.button(text=f"Итог {course_num}.{i}")
    builder.button(text="🔙 К курсам Итога")
    builder.adjust(4, 1)
    return builder.as_markup(resize_keyboard=True)


# -------------------- 5. ФУНКЦИЯ ОТПРАВКИ КОНТЕНТА (ИСПРАВЛЕНО) --------------------

def clean_tags(text: str, tag: str) -> str:
    """Удаляет указанный тег и лишние пробелы из текста."""
    # Удаляем тег в нижнем регистре и в верхнем регистре, если он есть в тексте
    cleaned_text = text.replace(f"#{tag}", "").replace(f"#{tag.upper()}", "")
    
    # Удаляем случайные теги (#exam1) и лишние пробелы
    cleaned_text = ' '.join(cleaned_text.split())
    
    return cleaned_text

async def send_content_by_tag(chat_id: int, tag: str):
    """Находит контент в JSON-словаре по тегу и отправляет его БЕЗ ТЕГОВ."""
    tag = tag.lower()
    materials = content_data.get(tag, [])
    
    if not materials:
        await bot.send_message(chat_id, f"❌ Материалы не найдены(")
        return

    await bot.send_message(chat_id, f"📦 **Материалы по запросу:**", parse_mode="Markdown")

    for item in materials:
        caption = item.get("caption", None)
        file_id = item.get("file_id", None)
        item_type = item.get("type")
        
        # ⚠️ НОВАЯ ЛОГИКА: ОЧИСТКА ТЕКСТА ОТ ТЕГОВ
        if caption:
            caption = clean_tags(caption, tag)
            
        # Если это чистый текст (не медиа), удаляем тег из самого текста
        if item_type == "text" and caption:
            caption = clean_tags(caption, tag)
            
        try:
            if item_type == "text":
                await bot.send_message(chat_id, caption)
            elif item_type == "photo" and file_id:
                await bot.send_photo(chat_id, file_id, caption=caption)
            elif item_type == "video" and file_id:
                await bot.send_video(chat_id, file_id, caption=caption)
            elif item_type == "document" and file_id:
                await bot.send_document(chat_id, file_id, caption=caption)
        except Exception as e:
            print(f"Ошибка отправки контента {item_type} для тега #{tag}: {e}")
            await bot.send_message(chat_id, "⚠️ Произошла внутренняя ошибка при отправке файла.")

    await bot.send_message(chat_id, "✅ Все материалы по запросу отправлены.")


# -------------------- 6. ОБРАБОТЧИКИ FSM (ЗАГРУЗКА) --------------------

@dp.callback_query(lambda c: c.data.startswith('upload__'), lambda m: m.from_user.id in ADMIN_IDS)
async def start_upload_fsm(callback: types.CallbackQuery, state: FSMContext):
    target_tag = callback.data.split('__')[1]
    
    await state.update_data(target_tag=target_tag)
    await state.set_state(AdminStates.waiting_for_content)
    
    await callback.message.answer(
        f"✅ Режим загрузки активирован.\nОтправьте файл, фото или текст для раздела **#{target_tag}**.\nДля отмены введите /start.", 
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.message(AdminStates.waiting_for_content)
async def process_content_upload(message: types.Message, state: FSMContext):
    
    if message.text and message.text.lower() == '/start':
        await state.clear()
        return await message.answer("Загрузка отменена. Возврат в главное меню.", reply_markup=get_reply_main_menu_keyboard())

    data = await state.get_data()
    target_tag = data.get('target_tag')
    
    if not target_tag:
        await state.clear()
        return await message.answer("Ошибка: не удалось определить целевой раздел. Попробуйте снова.")
        
    entry = {"type": "text", "file_id": None, "caption": message.caption or message.text}
    
    if message.document:
        entry["type"] = "document"
        entry["file_id"] = message.document.file_id
    elif message.photo:
        entry["type"] = "photo"
        entry["file_id"] = message.photo[-1].file_id
    elif message.video:
        entry["type"] = "video"
        entry["file_id"] = message.video.file_id
    elif not message.text:
        await message.answer("⚠️ Неподдерживаемый тип контента (нужен файл, фото, видео или текст).")
        return

    if target_tag not in content_data:
        content_data[target_tag] = []
    
    entry['caption'] = entry['caption'].strip() # Очистка лишних пробелов для сохранения
    content_data[target_tag].append(entry)
    save_content(content_data)

    await message.answer(f"✅ Материал успешно сохранён в раздел **#{target_tag}**!", parse_mode="Markdown")
    await state.clear()
    
    await cmd_start(message)


# -------------------- 7. ОБРАБОТЧИКИ МЕНЮ (ПОЛЬЗОВАТЕЛЬСКИЙ ФЛОУ) --------------------

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Добро пожаловать! Выберите раздел:", reply_markup=get_reply_main_menu_keyboard())

# --- Материалы ---
@dp.message(lambda m: m.text == "📂 Материалы")
async def materials_menu(message: types.Message):
    await message.answer("Выберите тип материалов:", reply_markup=get_reply_materials_menu_keyboard())

@dp.message(lambda m: m.text == "📖 Лекции")
async def lectures_handler(message: types.Message):
    await send_content_by_tag(message.chat.id, "lec")

@dp.message(lambda m: m.text == "🔬 Практика")
async def practice_handler(message: types.Message):
    await send_content_by_tag(message.chat.id, "prac")

@dp.message(lambda m: m.text == "🎥 Видео")
async def video_handler(message: types.Message):
    await send_content_by_tag(message.chat.id, "vid")

# --- Экзамен (Обновлено для FSM) ---
@dp.message(lambda m: m.text == "📚 Экзамен")
async def exam_menu(message: types.Message):
    await message.answer("Выберите курс:", reply_markup=get_reply_exam_menu_keyboard())

# main.py (Секция 7)

@dp.message(lambda m: m.text.endswith("-курс") and not m.text.startswith("Итог -"))
async def exam_handler(message: types.Message):
    course = message.text.split("-")[0]
    tag = f"exam{course}"
    
    # 1. Сначала отправляем контент (пользовательский флоу)
    await send_content_by_tag(message.chat.id, tag)

    # 2. БЕЗУСЛОВНО показываем кнопку загрузки, если это админ
    if message.from_user.id in ADMIN_IDS:
        upload_builder = InlineKeyboardBuilder()
        upload_builder.button(text=f"➕ Загрузить в #{tag}", callback_data=f"upload__{tag}") 
        
        await message.answer(
            f"**АДМИН-ПАНЕЛЬ:** Нажмите для загрузки нового материала в раздел в текущий раз.",
            reply_markup=upload_builder.as_markup(),
            parse_mode="Markdown"
        )
    # 3. Возвращаем клавиатуру
    await message.answer("Выберите раздел:", reply_markup=get_reply_exam_menu_keyboard())

# --- Итог (Обновлено для FSM) ---
@dp.message(lambda m: m.text == "📋 Итог")
async def summary_menu(message: types.Message):
    await message.answer("Выберите курс:", reply_markup=get_reply_summary_course_keyboard())

@dp.message(lambda m: m.text.startswith("Итог -") and m.text.endswith("курс"))
async def summary_course(message: types.Message):
    course_num = int(message.text.split('-')[1].split()[0])
    await message.answer(
        f"**Итог: {course_num} курс**\nВыберите конкретный итог:",
        parse_mode="Markdown",
        reply_markup=get_reply_final_summary_keyboard(course_num)
    )

# main.py (Секция 7)

@dp.message(lambda m: m.text.startswith("Итог ") and "." in m.text)
async def summary_result(message: types.Message):
    tag = "itog" + message.text.split(" ")[1].replace(".", "-") 
    
    # 1. Сначала отправляем контент (пользовательский флоу)
    await send_content_by_tag(message.chat.id, tag)

    # 2. БЕЗУСЛОВНО показываем кнопку загрузки, если это админ
    if message.from_user.id in ADMIN_IDS:
        upload_builder = InlineKeyboardBuilder()
        upload_builder.button(text=f"➕ Загрузить в #{tag}", callback_data=f"upload__{tag}") 
        
        await message.answer(
            f"**АДМИН-ПАНЕЛЬ:** Нажмите для загрузки нового материала в текущий раздел.",
            reply_markup=upload_builder.as_markup(),
            parse_mode="Markdown"
        )
    
    # 3. Возвращаем клавиатуру
    course_num = int(message.text.split(' ')[1].split('.')[0])
    await message.answer(
        "Материал отправлен. Выберите следующий:",
        reply_markup=get_reply_final_summary_keyboard(course_num) 
    )

# --- Личный кабинет ---
@dp.message(lambda m: m.text == "🚪 Личный кабинет")
async def personal_account(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.button(text="Открыть study.tj", web_app=WebAppInfo(url="https://study.tj/"))
    await message.answer("Нажмите кнопку ниже:", reply_markup=builder.as_markup())

# --- Навигация назад ---
@dp.message(lambda m: m.text == "🔙 Главное меню")
async def back_main(message: types.Message):
    await message.answer("Вы вернулись в главное меню:", reply_markup=get_reply_main_menu_keyboard())

@dp.message(lambda m: m.text == "🔙 К курсам Итога")
async def back_summary(message: types.Message):
    await message.answer("Выберите курс:", reply_markup=get_reply_summary_course_keyboard())


# --- Неизвестные команды ---
@dp.message()
async def unknown_message(message: types.Message):
    if message.text:
        await message.answer("Неизвестная команда. Используйте кнопки меню.")


# -------------------- 8. ЗАПУСК --------------------
async def main():
    print("Бот запущен. Ожидание команд и загрузок от администратора...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
