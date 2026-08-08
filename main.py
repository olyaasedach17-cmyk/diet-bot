import asyncio
import os
import base64
import json
from datetime import datetime
from dotenv import load_dotenv
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from openai import AsyncOpenAI

import firebase_admin
from firebase_admin import credentials, firestore

load_dotenv()
TOKEN = os.getenv('BOT_TOKEN')
client = AsyncOpenAI(api_key=os.getenv('AI_API_KEY'), base_url=os.getenv('AI_BASE_URL'))

# --- ПОДКЛЮЧАЕМ FIREBASE ---
firebase_json_str = os.getenv('FIREBASE_JSON')
if firebase_json_str:
    creds_dict = json.loads(firebase_json_str)
    cred = credentials.Certificate(creds_dict)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("🔥 Firebase успешно подключен!")
else:
    print("❌ ОШИБКА: Ключ FIREBASE_JSON не найден!")
    db = None

bot = Bot(token=TOKEN)
dp = Dispatcher()

class BotStates(StatesGroup):
    waiting_for_status = State()
    waiting_for_user_params = State()
    waiting_for_goal = State()
    waiting_for_activity = State()
    waiting_for_menu_ingredients = State()
    waiting_for_extra_permission = State()

main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🍎 Составить меню"), KeyboardButton(text="📊 Мой дневник")],
        [KeyboardButton(text="❌ Сбросить шаг / Начать заново")]
    ],
    resize_keyboard=True,
    input_field_placeholder="Выбери действие..."
)

status_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="🥩 Это сырые продукты", callback_data="status_raw"),
        InlineKeyboardButton(text="🍳 Это готовое блюдо", callback_data="status_cooked")
    ]
])

goal_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📉 Похудение", callback_data="goal_loss")],
    [InlineKeyboardButton(text="⚖️ Поддержание веса", callback_data="goal_maintain")],
    [InlineKeyboardButton(text="📈 Набор массы", callback_data="goal_gain")]
])

activity_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🛋 Низкая (сидячая работа)", callback_data="act_low")],
    [InlineKeyboardButton(text="🚶 Средняя (1-3 тренировки)", callback_data="act_med")],
    [InlineKeyboardButton(text="🏃 Высокая (спорт 3+ раз)", callback_data="act_high")]
])

extra_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🧑‍🍳 Добавь базу (масло, лук)", callback_data="extra_yes")],
    [InlineKeyboardButton(text="🛑 СТРОГО из моего списка", callback_data="extra_no")]
])

SYSTEM_PROMPT = """Ты — ИИ-нутрициолог. 
ЕСЛИ СЧИТАЕШЬ ФОТО: Верь пользователю, если он указал вес текстом. Ищи масштабы (вилки). Читай этикетки. Выдавай расчет четко и без лишней воды.
ЕСЛИ ПИШЕШЬ МЕНЮ/ДНЕВНИК: Считай математику безупречно, выдавай понятные отчеты."""

async def ask_ai(image_base64=None, text_prompt=None, context=""):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if image_base64:
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": f"Контекст: {context}. Изучи еду на фото, оцени вес и КБЖУ."},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
            ]
        })
    elif text_prompt:
        messages.append({"role": "user", "content": text_prompt})

    response = await client.chat.completions.create(model="gpt-4o", messages=messages, temperature=0.3)
    return response.choices[0].message.content

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Привет! 👋 Я твой ИИ-нутрициолог.\n\nПросто отправь мне фото еды или нажми кнопку меню внизу!", reply_markup=main_menu)

# --- ИСПРАВЛЕН БАГ №2: Полная отмена процессов ---
@dp.message(F.text == "❌ Сбросить шаг / Начать заново")
async def cancel_action(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🔄 Все текущие расчеты отменены. Можешь прислать новое фото!", reply_markup=main_menu)

# --- ЛОГИКА FIREBASE ДНЕВНИКА ---
def get_today_doc_id(user_id):
    today = datetime.now().strftime("%Y-%m-%d")
    return f"{user_id}_{today}"

@dp.message(F.text == "📊 Мой дневник")
async def show_diary(message: Message):
    if not db:
        await message.answer("⚠️ База данных не подключена.")
        return
        
    user_id = message.from_user.id
    doc_ref = db.collection('diaries').document(get_today_doc_id(user_id))
    doc = doc_ref.get()
    
    if not doc.exists or not doc.to_dict().get('meals'):
        await message.answer("Твой дневник за сегодня пуст! 🍽 Отправь фото еды и нажми «Записать в дневник».", reply_markup=main_menu)
        return
    
    meals = doc.to_dict().get('meals', [])
    msg = await message.answer("📊 Достаю записи из базы и считаю итоги...")
    
    diary_text = "\n\n---\n".join(meals)
    
    # --- ПРОКАЧАННАЯ ИНСТРУКЦИЯ ДЛЯ НЕЙРОСЕТИ ---
    prompt = f"""Вот список всего, что я съел(а) за сегодня:
{diary_text}

Сделай красивый и понятный отчет:
1. Кратко перечисли всё съеденное (списком).
2. Посчитай ИТОГОВУЮ СУММУ КБЖУ за день (выведи жирным).
3. МОЯ НОРМА: 1600 ккал. Сравни сумму с моей нормой и напиши, сколько калорий мне еще осталось съесть сегодня (или на сколько я превысил(а) лимит)."""
    
    try:
        ai_response = await ask_ai(text_prompt=prompt)
        await msg.edit_text(ai_response)
    except Exception as e:
        await msg.edit_text("Произошла ошибка при анализе дневника.")
@dp.callback_query(F.data == "save_to_diary")
async def save_to_diary(callback: CallbackQuery, state: FSMContext):
    if not db:
        await callback.answer("Ошибка базы данных!", show_alert=True)
        return
        
    user_id = callback.from_user.id
    data = await state.get_data()
    last_response = data.get("last_ai_response")
    
    if not last_response:
        await callback.answer("Нечего сохранять!", show_alert=True)
        return

    # Запись в Firebase
    doc_ref = db.collection('diaries').document(get_today_doc_id(user_id))
    doc_ref.set({'meals': firestore.ArrayUnion([last_response])}, merge=True)
    
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("✅ Блюдо записано в твой профиль Firebase!")
    await callback.answer()

# --- ФОТО ЕДЫ ---
@dp.message(F.photo)
async def handle_photo(message: Message, state: FSMContext):
    await state.clear()
    photo_id = message.photo[-1].file_id
    file = await bot.get_file(photo_id)
    downloaded_file = await bot.download_file(file.file_path)
    image_base64 = base64.b64encode(downloaded_file.read()).decode('utf-8')
    
    await state.update_data(saved_photo=image_base64, photo_caption=message.caption or "")
    await state.set_state(BotStates.waiting_for_status)
    
    # ИСПРАВЛЕН БАГ №3: Добавлена четкая инструкция нажать на кнопку
    await message.answer("Супер! Уточни статус продукта (нажми на одну из кнопок ниже 👇):", reply_markup=status_keyboard)

@dp.callback_query(BotStates.waiting_for_status, F.data.in_(["status_raw", "status_cooked"]))
async def process_photo_status(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.edit_text("⏳ Нейросеть изучает фото...")
    
    user_data = await state.get_data()
    image_base64 = user_data.get("saved_photo")
    
    status_text = "ЭТО СЫРЫЕ ПРОДУКТЫ" if callback.data == "status_raw" else "ЭТО ГОТОВОЕ БЛЮДО"
    if user_data.get("photo_caption"):
        status_text += f". Комментарий пользователя: {user_data.get('photo_caption')}"
        
    try:
        ai_response = await ask_ai(image_base64=image_base64, context=status_text)
        current_state = await state.get_state()
        if current_state != BotStates.waiting_for_status.state:
            return 
            
        await state.update_data(last_ai_response=ai_response)
        diary_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Записать в дневник", callback_data="save_to_diary")]])
        await callback.message.edit_text(ai_response, reply_markup=diary_kb)
        
        # ВАЖНОЕ ИЗМЕНЕНИЕ: мы убрали полную очистку памяти (state.clear)
        # Теперь бот просто перестает ждать статус, но текст расчета бережно хранит!
        await state.set_state(None)
        
    except Exception as e:
        await callback.message.edit_text("Упс, произошла ошибка.")
        await state.clear()
# --- МЕНЮ ---
@dp.message(F.text == "🍎 Составить меню")
async def handle_menu_btn(message: Message, state: FSMContext):
    await message.answer("Напиши параметры: **вес, рост, возраст** (например: 65, 170, 25).")
    await state.set_state(BotStates.waiting_for_user_params)

@dp.message(BotStates.waiting_for_user_params)
async def process_user_params(message: Message, state: FSMContext):
    await state.update_data(user_params=message.text)
    await message.answer("🎯 Выбери цель:", reply_markup=goal_keyboard)
    await state.set_state(BotStates.waiting_for_goal)

@dp.callback_query(BotStates.waiting_for_goal, F.data.startswith("goal_"))
async def process_goal(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    goals = {"goal_loss": "Похудение", "goal_maintain": "Поддержание", "goal_gain": "Набор массы"}
    await state.update_data(user_goal=goals[callback.data])
    await callback.message.answer("🏃‍♀️ Оцени активность:", reply_markup=activity_keyboard)
    await state.set_state(BotStates.waiting_for_activity)

@dp.callback_query(BotStates.waiting_for_activity, F.data.startswith("act_"))
async def process_activity(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    activities = {"act_low": "Низкая", "act_med": "Средняя", "act_high": "Высокая"}
    await state.update_data(user_activity=activities[callback.data])
    await callback.message.answer("📝 Напиши список продуктов:")
    await state.set_state(BotStates.waiting_for_menu_ingredients)

@dp.message(BotStates.waiting_for_menu_ingredients)
async def process_menu_ingredients(message: Message, state: FSMContext):
    await state.update_data(user_ingredients=message.text)
    await message.answer("Можно добавить базу (масло, специи)?", reply_markup=extra_keyboard)
    await state.set_state(BotStates.waiting_for_extra_permission)

@dp.callback_query(BotStates.waiting_for_extra_permission, F.data.startswith("extra_"))
async def process_extra_permission(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.edit_text("🍳 Считаю...")
    
    user_data = await state.get_data()
    extra = "РАЗРЕШЕНО добавлять базу." if callback.data == "extra_yes" else "СТРОГО ЗАПРЕЩЕНО добавлять чужое."
    prompt = f"Параметры: {user_data.get('user_params')}. Цель: {user_data.get('user_goal')}. Активность: {user_data.get('user_activity')}. Продукты: {user_data.get('user_ingredients')}. {extra}"
    
    try:
        ai_response = await ask_ai(text_prompt=prompt)
        await callback.message.edit_text(ai_response)
    except Exception:
        await callback.message.edit_text("Ошибка.")
    finally:
        await state.clear()

async def health_check(request):
    return web.Response(text="Bot is running!")

async def main():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
