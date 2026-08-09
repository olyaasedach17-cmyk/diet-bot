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

# --- FIREBASE ---
firebase_json_str = os.getenv('FIREBASE_JSON')
if firebase_json_str:
    creds_dict = json.loads(firebase_json_str)
    cred = credentials.Certificate(creds_dict)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("🔥 Firebase подключен!")
else:
    print("❌ ОШИБКА: Нет FIREBASE_JSON!")
    db = None

bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- СОСТОЯНИЯ ---
class BotStates(StatesGroup):
    waiting_for_status = State()
    waiting_for_menu_ingredients = State()
    waiting_for_extra_permission = State()

class ProfileStates(StatesGroup):
    gender = State()
    age = State()
    height = State()
    weight = State()
    goal = State()
    activity = State()
    new_weight = State()

# --- КЛАВИАТУРЫ ---
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🍎 Составить меню"), KeyboardButton(text="📊 Мой дневник")],
        [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="❌ Сбросить шаг")]
    ],
    resize_keyboard=True,
    input_field_placeholder="Выбери действие..."
)

gender_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="👨 Мужчина", callback_data="gender_M"), InlineKeyboardButton(text="👩 Женщина", callback_data="gender_F")]
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

status_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🥩 Сырые продукты", callback_data="status_raw"), InlineKeyboardButton(text="🍳 Готовое блюдо", callback_data="status_cooked")]
])

extra_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🧑‍🍳 Добавь базу (масло, лук)", callback_data="extra_yes")],
    [InlineKeyboardButton(text="🛑 СТРОГО из моего списка", callback_data="extra_no")]
])

SYSTEM_PROMPT = """Ты — ИИ-нутрициолог. 
ЕСЛИ СЧИТАЕШЬ ФОТО: Верь пользователю. Выдавай расчет четко.
ЕСЛИ ПИШЕШЬ МЕНЮ: Считай математику безупречно, выдавай понятные рецепты."""

async def ask_ai(image_base64=None, text_prompt=None, context=""):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if image_base64:
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": f"Контекст: {context}. Изучи еду, оцени вес и КБЖУ."},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
            ]
        })
    elif text_prompt:
        messages.append({"role": "user", "content": text_prompt})

    response = await client.chat.completions.create(model="gpt-4o", messages=messages, temperature=0.3)
    return response.choices[0].message.content

def calculate_norm(gender, age, height, weight, goal, activity):
    if gender == 'M':
        bmr = (10 * weight) + (6.25 * height) - (5 * age) + 5
    else:
        bmr = (10 * weight) + (6.25 * height) - (5 * age) - 161
        
    act_mults = {"Низкая": 1.2, "Средняя": 1.55, "Высокая": 1.725}
    tdee = bmr * act_mults.get(activity, 1.2)
    
    if goal == "Похудение": tdee *= 0.8
    elif goal == "Набор массы": tdee *= 1.2
        
    return int(tdee)

# --- ПРОФИЛЬ ---
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    doc = db.collection('users').document(str(message.from_user.id)).get()
    if doc.exists:
        await message.answer("Привет! 👋 Я твой ИИ-нутрициолог.\nЖду фото еды!", reply_markup=main_menu)
    else:
        await message.answer("Привет! 👋 Давай настроим профиль.\nУкажи пол:", reply_markup=gender_kb)
        await state.set_state(ProfileStates.gender)

@dp.callback_query(ProfileStates.gender, F.data.startswith("gender_"))
async def ask_age(callback: CallbackQuery, state: FSMContext):
    await state.update_data(gender=callback.data.split("_")[1])
    await callback.message.edit_text("Отлично! Напиши свой возраст (цифрой):")
    await state.set_state(ProfileStates.age)

@dp.message(ProfileStates.age)
async def ask_height(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("Только цифры.")
    await state.update_data(age=int(message.text))
    await message.answer("Укажи свой рост в см:")
    await state.set_state(ProfileStates.height)

@dp.message(ProfileStates.height)
async def ask_weight(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("Только цифры.")
    await state.update_data(height=int(message.text))
    await message.answer("Укажи свой текущий вес в кг:")
    await state.set_state(ProfileStates.weight)

@dp.message(ProfileStates.weight)
async def ask_prof_goal(message: Message, state: FSMContext):
    try:
        await state.update_data(weight=float(message.text.replace(',', '.')))
        await message.answer("Какая у тебя цель?", reply_markup=goal_keyboard)
        await state.set_state(ProfileStates.goal)
    except ValueError:
        await message.answer("Введи вес цифрами.")

@dp.callback_query(ProfileStates.goal, F.data.startswith("goal_"))
async def ask_prof_act(callback: CallbackQuery, state: FSMContext):
    goals = {"goal_loss": "Похудение", "goal_maintain": "Поддержание", "goal_gain": "Набор массы"}
    await state.update_data(goal=goals[callback.data])
    await callback.message.edit_text("Уровень активности:", reply_markup=activity_keyboard)
    await state.set_state(ProfileStates.activity)

@dp.callback_query(ProfileStates.activity, F.data.startswith("act_"))
async def finish_profile(callback: CallbackQuery, state: FSMContext):
    acts = {"act_low": "Низкая", "act_med": "Средняя", "act_high": "Высокая"}
    data = await state.get_data()
    norm = calculate_norm(data['gender'], data['age'], data['height'], data['weight'], data['goal'], acts[callback.data])
    
    db.collection('users').document(str(callback.from_user.id)).set({
        'gender': data['gender'], 'age': data['age'], 'height': data['height'], 'weight': data['weight'],
        'goal': data['goal'], 'activity': acts[callback.data], 'norm': norm
    })
    
    await callback.message.delete()
    await callback.message.answer(f"✅ Профиль создан!\nНорма: **{norm} ккал**.", reply_markup=main_menu)
    await state.clear()

@dp.message(F.text == "👤 Мой профиль")
async def show_my_profile(message: Message):
    doc = db.collection('users').document(str(message.from_user.id)).get()
    if not doc.exists: return await message.answer("Профиль не найден. Нажми /start")
    data = doc.to_dict()
    text = f"👤 **Твой профиль:**\n\nВес: {data['weight']} кг\nРост: {data['height']} см\nВозраст: {data['age']} лет\nЦель: {data['goal']}\n\n🔥 **Дневная норма: {data['norm']} ккал**"
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚖️ Обновить вес", callback_data="update_weight")]]))

@dp.callback_query(F.data == "update_weight")
async def req_new_weight(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введи свой новый вес (кг):")
    await state.set_state(ProfileStates.new_weight)

@dp.message(ProfileStates.new_weight)
async def save_new_weight(message: Message, state: FSMContext):
    try:
        new_w = float(message.text.replace(',', '.'))
        user_id = str(message.from_user.id)
        doc_ref = db.collection('users').document(user_id)
        data = doc_ref.get().to_dict()
        new_norm = calculate_norm(data['gender'], data['age'], data['height'], new_w, data['goal'], data['activity'])
        doc_ref.update({'weight': new_w, 'norm': new_norm})
        await message.answer(f"🎉 Вес обновлен! Новая норма: **{new_norm} ккал**.", reply_markup=main_menu)
        await state.clear()
    except ValueError:
        await message.answer("Введи цифры.")

@dp.message(F.text == "❌ Сбросить шаг")
async def cancel_action(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🔄 Шаг отменен.", reply_markup=main_menu)

# --- МЕНЮ ---
@dp.message(F.text == "🍎 Составить меню")
async def start_menu_generation(message: Message, state: FSMContext):
    doc = db.collection('users').document(str(message.from_user.id)).get()
    if not doc.exists: return await message.answer("Сначала заполни профиль (/start).")
    await message.answer("📝 Напиши список продуктов, которые у тебя сейчас есть (например: курица, рис, помидоры):")
    await state.set_state(BotStates.waiting_for_menu_ingredients)

@dp.message(BotStates.waiting_for_menu_ingredients)
async def process_menu_ingredients(message: Message, state: FSMContext):
    await state.update_data(user_ingredients=message.text)
    await message.answer("Можно добавить базовые продукты (масло, специи, лук)?", reply_markup=extra_keyboard)
    await state.set_state(BotStates.waiting_for_extra_permission)

@dp.callback_query(BotStates.waiting_for_extra_permission, F.data.startswith("extra_"))
async def process_extra_permission(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.edit_text("🍳 Придумываю рецепт...")
    data = db.collection('users').document(str(callback.from_user.id)).get().to_dict()
    s_data = await state.get_data()
    ex = "РАЗРЕШЕНО добавлять базу." if callback.data == "extra_yes" else "СТРОГО ЗАПРЕЩЕНО добавлять чужие ингредиенты."
    prompt = f"Составь меню на 1 прием пищи. Цель {data['goal']}, норма {data['norm']} ккал. Ингредиенты: {s_data.get('user_ingredients')}. {ex} Напиши рецепт и граммовки (на 25-35% от нормы). Выведи КБЖУ."
    try:
        res = await ask_ai(text_prompt=prompt)
        await callback.message.edit_text(res)
    except Exception:
        await callback.message.edit_text("Ошибка при составлении меню.")
    finally:
        await state.clear()

# --- ДНЕВНИК ---
def get_today_doc_id(user_id):
    return f"{user_id}_{datetime.now().strftime('%Y-%m-%d')}"

@dp.message(F.text == "📊 Мой дневник")
async def show_diary(message: Message):
    user_id = str(message.from_user.id)
    u_doc = db.collection('users').document(user_id).get()
    if not u_doc.exists: return await message.answer("Заполни профиль (/start).")
    norm = u_doc.to_dict().get('norm', 2000)
    doc = db.collection('diaries').document(get_today_doc_id(user_id)).get()
    if not doc.exists or not doc.to_dict().get('meals'):
        return await message.answer(f"Дневник пуст! Отправь фото. (Цель: {norm} ккал)", reply_markup=main_menu)
    meals = doc.to_dict().get('meals', [])
    msg = await message.answer("📊 Считаю итоги за сегодня...")
    prompt = f"Съедено:\n{chr(10).join(meals)}\nСделай отчет, итог КБЖУ. МОЯ НОРМА: {norm} ккал. Сколько осталось?"
    try:
        await msg.edit_text(await ask_ai(text_prompt=prompt))
    except Exception:
        await msg.edit_text("Ошибка.")

@dp.callback_query(F.data == "save_to_diary")
async def save_to_diary(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("last_ai_response"): return await callback.answer("Нечего сохранять!", show_alert=True)
    record = f"⏰ {datetime.now().strftime('%H:%M:%S')}\n{data['last_ai_response']}"
    db.collection('diaries').document(get_today_doc_id(callback.from_user.id)).set({'meals': firestore.ArrayUnion([record])}, merge=True)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("✅ Блюдо записано в дневник!")

# --- ФОТО ---
@dp.message(F.photo)
async def handle_photo(message: Message, state: FSMContext):
    await state.clear()
    file = await bot.get_file(message.photo[-1].file_id)
    d_file = await bot.download_file(file.file_path)
    await state.update_data(saved_photo=base64.b64encode(d_file.read()).decode('utf-8'), photo_caption=message.caption or "")
    await state.set_state(BotStates.waiting_for_status)
    await message.answer("Уточни статус продукта:", reply_markup=status_keyboard)

@dp.callback_query(BotStates.waiting_for_status, F.data.in_(["status_raw", "status_cooked"]))
async def process_photo_status(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.edit_text("⏳ Нейросеть изучает фото...")
    data = await state.get_data()
    st = "СЫРЫЕ ПРОДУКТЫ" if callback.data == "status_raw" else "ГОТОВОЕ БЛЮДО"
    if data.get("photo_caption"): st += f". Коммент: {data['photo_caption']}"
    try:
        res = await ask_ai(image_base64=data.get("saved_photo"), context=st)
        if await state.get_state() != BotStates.waiting_for_status.state: return 
        await state.update_data(last_ai_response=res)
        await callback.message.edit_text(res, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Записать в дневник", callback_data="save_to_diary")]]))
        await state.set_state(None)
    except Exception:
        await callback.message.edit_text("Ошибка.")
        await state.clear()

# --- СЕРВЕР ---
async def health_check(request):
    return web.Response(text="Bot is running!")

async def main():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 8080)))
    await site.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
