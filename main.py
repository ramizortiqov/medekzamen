import os
import json
import asyncio
from dotenv import load_dotenv

# Импорты для FSM
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from aiogram.types import WebAppInfo, ContentType, CallbackQuery


# -------------------- 1. НАСТРОЙКА И КОНСТАНТЫ --------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

# ВАШИ АДМИНИСТРАТОРЫ (Используется напрямую)
ADMIN_IDS = [6720999592, 6520890849] 

# 🚨 КОНСТАНТЫ ДЛЯ ПРОВЕРКИ ПОДПИСКИ
CHANNEL_ID = -1002034189536 
CHANNEL_URL = "https://t.me/fr_ray7"
FEEDBACK_USERNAME_URL = "https://t.me/parviz_medik" # ⚠️ ЗАМЕНИТЕ НА ВАШ @ЛОГИН

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен в .env")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

CONTENT_FILE = "content.json"


# --- 2. FSM СОСТОЯНИЯ ---
class AdminStates(StatesGroup):
    """Состояния для загрузки и удаления контента администратором."""
    waiting_for_content = State()
    waiting_for_index_to_delete = State()


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


# --- 4. СЕРВИСНЫЕ ФУНКЦИИ ---

def get_admin_menu(tag: str) -> types.InlineKeyboardMarkup:
    """Клавиатура с кнопками 'Загрузить' и 'Удалить' для админа."""
    builder = InlineKeyboardBuilder()
    builder.button(text=f"➕ Загрузить в #{tag}", callback_data=f"upload__{tag}") 
    builder.button(text=f"🗑️ Удалить по индексу из #{tag}", callback_data=f"delete_indexed__{tag}") 
    builder.adjust(1)
    return builder.as_markup()

def get_subscription_keyboard() -> types.InlineKeyboardMarkup:
    """Клавиатура для запроса подписки."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔔 Подписка", url=CHANNEL_URL)
    builder.button(text="✅ Проверить", callback_data="check_subscription")
    builder.adjust(2)
    return builder.as_markup()

def get_no_access_message() -> str:
    """Сообщение о недоступности контента."""
    return f"🚫 **Доступ ограничен.** Для использования бота, пожалуйста, **подпишитесь** на наш канал."

async def check_subscription(user_id: int) -> bool:
    """Проверяет, подписан ли пользователь на обязательный канал."""
    if user_id in ADMIN_IDS:
        return True
        
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        print(f"Ошибка при проверке подписки: {e}")
        return False

def clean_tags(text: str, tag: str) -> str:
    """Удаляет указанный тег и лишние пробелы из текста."""
    cleaned_text = text.replace(f"#{tag}", "").replace(f"#{tag.upper()}", "")
    cleaned_text = ' '.join(cleaned_text.split())
    return cleaned_text


# -------------------- 5. ФУНКЦИИ КЛАВИАТУР (Reply) --------------------
def get_reply_main_menu_keyboard():
    """ОБНОВЛЕНО: Добавлен раздел 'Обратная связь'."""
    builder = ReplyKeyboardBuilder()
    builder.button(text="📚 Экзамен")
    builder.button(text="📋 Итог")
    builder.button(text="📂 Материалы")
    builder.button(text="🚪 Личный кабинет")
    builder.button(text="✉️ Обратная связь") # НОВАЯ КНОПКА
    builder.adjust(2, 2, 1) # Расположение: 2 в первом, 2 во втором, 1 в третьем
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


# -------------------- 6. ОТПРАВКА КОНТЕНТА --------------------

async def send_content_by_tag(chat_id: int, tag: str):
    """Находит контент в JSON-словаре по тегу и отправляет его."""
    tag = tag.lower()
    materials = content_data.get(tag, [])
    
    if not materials:
        await bot.send_message(chat_id, f"❌ Материалы по тегу **#{tag}** не найдены.", parse_mode="Markdown")
        return

    await bot.send_message(chat_id, f"📦 **Материалы по запросу:**", parse_mode="Markdown")

    for item in materials:
        caption = item.get("caption", None)
        file_id = item.get("file_id", None)
        item_type = item.get("type")
        file_name = item.get("file_name", None) 
        
        if caption:
            caption = clean_tags(caption, tag)
            
        final_caption = ""
        if file_name and item_type != "text":
             final_caption += f"📄 **{file_name}**\n\n" 
             
        if caption:
            final_caption += caption
            
        if not final_caption and item_type != "text":
             final_caption = f"*{item_type.capitalize()} без описания*"
            
        try:
            if item_type == "text":
                await bot.send_message(chat_id, final_caption)
            elif item_type == "photo" and file_id:
                await bot.send_photo(chat_id, file_id, caption=final_caption, parse_mode="Markdown")
            elif item_type == "video" and file_id:
                await bot.send_video(chat_id, file_id, caption=final_caption, parse_mode="Markdown")
            elif item_type == "document" and file_id:
                await bot.send_document(chat_id, file_id, caption=final_caption, parse_mode="Markdown")
        except Exception as e:
            print(f"Ошибка отправки контента {item_type} для тега #{tag}: {e}")
            await bot.send_message(chat_id, "⚠️ Произошла внутренняя ошибка при отправке файла.")

    await bot.send_message(chat_id, "✅ Все материалы по запросу отправлены.")


# -------------------- 7. ОБРАБОТЧИКИ FSM (ЗАГРУЗКА И УДАЛЕНИЕ) --------------------

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
        
    entry = {"type": "text", "file_id": None, "caption": message.caption or message.text, "file_name": None}
    
    if message.document:
        entry["type"] = "document"
        entry["file_id"] = message.document.file_id
        entry["file_name"] = message.document.file_name
    elif message.photo:
        entry["type"] = "photo"
        entry["file_id"] = message.photo[-1].file_id
        entry["file_name"] = "Изображение"
    elif message.video:
        entry["type"] = "video"
        entry["file_id"] = message.video.file_id
        entry["file_name"] = message.video.file_name or "Видеозапись"
    elif not message.text:
        await message.answer("⚠️ Неподдерживаемый тип контента (нужен файл, фото, видео или текст).")
        return

    if target_tag not in content_data:
        content_data[target_tag] = []
    
    entry['caption'] = entry['caption'].strip() if entry['caption'] else ""
    content_data[target_tag].append(entry)
    save_content(content_data)

    await message.answer(f"✅ Материал успешно сохранён в раздел **#{target_tag}**!\nИмя файла: **{entry.get('file_name', 'Нет имени')}**", parse_mode="Markdown")
    
    await state.clear()
    await message.answer("Загрузка завершена. Выберите следующий раздел или вернитесь в главное меню.", reply_markup=get_reply_main_menu_keyboard())


# --- FSM: Запуск удаления по индексу ---

@dp.callback_query(lambda c: c.data.startswith('delete_indexed__'), lambda m: m.from_user.id in ADMIN_IDS)
async def start_indexed_delete(callback: types.CallbackQuery, state: FSMContext):
    """Callback-обработчик: Запускает удаление для текущего раздела и показывает список."""
    tag = callback.data.split('__')[1]
    
    materials = content_data.get(tag)
    if not materials:
        return await callback.message.answer(f"❌ В разделе **#{tag}** нет материалов для удаления.", parse_mode="Markdown")

    await state.update_data(target_tag=tag, materials=materials)
    await state.set_state(AdminStates.waiting_for_index_to_delete)
    
    response_text = f"🗑️ **УДАЛЕНИЕ:** Раздел **#{tag}**\n\n"
    for i, item in enumerate(materials):
        caption = item.get("caption") or ""
        file_name = item.get("file_name", "Текст/Файл")
        item_type = item.get("type", "файл")
        
        display_name = file_name if item_type != "text" and file_name else caption.strip()
        display_caption = display_name[:60] + "..." if len(display_name) > 60 else display_name

        response_text += f"**{i}:** [`{item_type.upper()}`] {display_caption}\n"
    
    response_text += "\n\n🔢 **Для удаления:** Введите **номер(а) (индекс)** материала через пробел (например, `0 5 8`), **ALL** для удаления всех материалов, или `/start` для отмены."

    await callback.message.answer(response_text, parse_mode="Markdown")
    await callback.answer()

@dp.message(AdminStates.waiting_for_index_to_delete)
async def process_indexed_deletion(message: types.Message, state: FSMContext):
    """FSM-обработчик: Принимает индексы, удаляет материалы и выходит из FSM."""
    
    if message.text and message.text.lower() == '/start':
        await state.clear()
        return await message.answer("Удаление отменено. Возврат в главное меню.", reply_markup=get_reply_main_menu_keyboard())

    data = await state.get_data()
    tag = data.get('target_tag')
    materials = data.get('materials')

    input_text = message.text.strip().upper()
    
    # 1. ЛОГИКА "УДАЛИТЬ ВСЕ" (ALL)
    if input_text == "ALL":
        if tag in content_data:
            content_data[tag] = []
            save_content(content_data)
            await message.answer(f"✅ **ВСЕ** материалы из раздела **#{tag}** были успешно **УДАЛЕНЫ**.", parse_mode="Markdown")
        
        await state.clear()
        return await message.answer("Удаление завершено. Выберите следующий раздел или вернитесь в главное меню.", reply_markup=get_reply_main_menu_keyboard())

    # 2. ЛОГИКА МНОЖЕСТВЕННОГО УДАЛЕНИЯ
    try:
        indices_str = input_text.replace(',', ' ').split()
        indices_to_delete = sorted([int(i) for i in indices_str], reverse=True) 
        
        if not indices_to_delete:
            return await message.answer("⚠️ Введите один или несколько чисел (индексы) через пробел (например, `0 5 8`) или слово ALL.")

    except ValueError:
        return await message.answer("⚠️ Неверный формат ввода. Введите числа через пробел (например, `0 5 8`) или слово ALL.")
        
    # 3. Валидация и удаление
    deleted_count = 0
    max_index = len(materials) - 1
    
    for index in indices_to_delete:
        if 0 <= index <= max_index:
            materials.pop(index)
            deleted_count += 1
        else:
            await message.answer(f"❌ Индекс {index} не существует (максимум {max_index}). Пропуск.", parse_mode="Markdown")

    if deleted_count > 0:
        content_data[tag] = materials
        save_content(content_data)
        
        await message.answer(
            f"✅ **{deleted_count}** материал(а) был(и) успешно **УДАЛЕН(Ы)** из раздела **#{tag}**.", 
            parse_mode="Markdown"
        )
    else:
        await message.answer("⚠️ Ни один материал не был удален. Проверьте введенные индексы.", parse_mode="Markdown")

    # 4. ПЛАВНЫЙ ВЫХОД
    await state.clear()
    await message.answer("Удаление завершено. Выберите следующий раздел или вернитесь в главное меню.", reply_markup=get_reply_main_menu_keyboard())


# -------------------- 8. ОБРАБОТЧИКИ МЕНЮ (ПОЛЬЗОВАТЕЛЬСКИЙ ФЛОУ) --------------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    
    if not await check_subscription(user_id):
        await message.answer(get_no_access_message(), reply_markup=get_subscription_keyboard(), parse_mode="Markdown")
    else:
        await message.answer("✅ Подписка подтверждена! Выберите раздел:", reply_markup=get_reply_main_menu_keyboard())

@dp.callback_query(F.data == "check_subscription")
async def process_check_subscription(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    if await check_subscription(user_id):
        await callback.message.delete()
        await callback.message.answer(
            "✅ Подписка подтверждена! Добро пожаловать! Выберите раздел:", 
            reply_markup=get_reply_main_menu_keyboard()
        )
    else:
        await callback.answer("❌ Вы ещё не подписались или подписка не подтверждена.", show_alert=True)
    
    await callback.answer() 


@dp.message(lambda m: m.text == "📂 Материалы")
async def materials_menu(message: types.Message):
    if not await check_subscription(message.from_user.id):
        return await message.answer(get_no_access_message(), reply_markup=get_subscription_keyboard(), parse_mode="Markdown")
    await message.answer("Выберите тип материалов:", reply_markup=get_reply_materials_menu_keyboard())

@dp.message(lambda m: m.text == "📖 Лекции")
async def lectures_handler(message: types.Message):
    if not await check_subscription(message.from_user.id):
        return await message.answer(get_no_access_message(), reply_markup=get_subscription_keyboard(), parse_mode="Markdown")
    await send_content_by_tag(message.chat.id, "lec")

@dp.message(lambda m: m.text == "🔬 Практика")
async def practice_handler(message: types.Message):
    if not await check_subscription(message.from_user.id):
        return await message.answer(get_no_access_message(), reply_markup=get_subscription_keyboard(), parse_mode="Markdown")
    await send_content_by_tag(message.chat.id, "prac")

@dp.message(lambda m: m.text == "🎥 Видео")
async def video_handler(message: types.Message):
    if not await check_subscription(message.from_user.id):
        return await message.answer(get_no_access_message(), reply_markup=get_subscription_keyboard(), parse_mode="Markdown")
    await send_content_by_tag(message.chat.id, "vid")

@dp.message(lambda m: m.text == "📚 Экзамен")
async def exam_menu(message: types.Message):
    await message.answer("Выберите курс:", reply_markup=get_reply_exam_menu_keyboard())

@dp.message(lambda m: m.text.endswith("-курс") and not m.text.startswith("Итог -"))
async def exam_handler(message: types.Message):
    if not await check_subscription(message.from_user.id):
        return await message.answer(get_no_access_message(), reply_markup=get_subscription_keyboard(), parse_mode="Markdown")

    course = message.text.split("-")[0]
    tag = f"exam{course}"
    
    await send_content_by_tag(message.chat.id, tag)

    if message.from_user.id in ADMIN_IDS:
        await message.answer(
            f"**АДМИН-ПАНЕЛЬ:**",
            reply_markup=get_admin_menu(tag),
            parse_mode="Markdown"
        )

    await message.answer("Выберите раздел:", reply_markup=get_reply_exam_menu_keyboard())


@dp.message(lambda m: m.text == "📋 Итог")
async def summary_menu(message: types.Message):
    await message.answer("Выберите курс:", reply_markup=get_reply_summary_course_keyboard())

@dp.message(lambda m: m.text.startswith("Итог -") and m.text.endswith("курс"))
async def summary_course(message: types.Message):
    if not await check_subscription(message.from_user.id):
        return await message.answer(get_no_access_message(), reply_markup=get_subscription_keyboard(), parse_mode="Markdown")
        
    course_num = int(message.text.split('-')[1].split()[0])
    await message.answer(
        f"**Итог: {course_num} курс**\nВыберите конкретный итог:",
        parse_mode="Markdown",
        reply_markup=get_reply_final_summary_keyboard(course_num)
    )

@dp.message(lambda m: m.text.startswith("Итог ") and "." in m.text)
async def summary_result(message: types.Message):
    if not await check_subscription(message.from_user.id):
        return await message.answer(get_no_access_message(), reply_markup=get_subscription_keyboard(), parse_mode="Markdown")
        
    tag = "itog" + message.text.split(" ")[1].replace(".", "-") 
    
    await send_content_by_tag(message.chat.id, tag)

    if message.from_user.id in ADMIN_IDS:
        await message.answer(
            f"**АДМИН-ПАНЕЛЬ:**",
            reply_markup=get_admin_menu(tag),
            parse_mode="Markdown"
        )
    
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

# --- Обратная связь ---
@dp.message(lambda m: m.text == "✉️ Обратная связь")
async def feedback_handler(message: types.Message):
    FEEDBACK_CONTACT_URL = "https://t.me/parviz_medik"
    builder = InlineKeyboardBuilder()
    builder.button(text="💬 Написать администратору", url=FEEDBACK_CONTACT_URL)
    
    await message.answer(
        "**ОБРАТНАЯ СВЯЗЬ**\n\n"
        "Мы всегда рады вашим предложениям, жалобам и пожеланиям по улучшению бота и материалов.\n"
        "Нажмите кнопку ниже, чтобы связаться с разработчиком или администратором:",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )

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


# -------------------- 9. ЗАПУСК --------------------
async def main():
    print("Бот запущен. Ожидание команд и загрузок от администратора...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())