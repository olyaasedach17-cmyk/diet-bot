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

# --- СОСТОЯНИЯ БОТА ---
class BotStates(StatesGroup):
    waiting_for_status = State()
    waiting_for_user_params = State()
    waiting_for_goal = State()
    waiting_for_activity = State()
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
ЕСЛИ СЧИТАЕШЬ ФОТО: Верь пользователю, если он указал вес текстом. Ищи масштабы. Выдавай расчет четко.
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

# --- МЕДИЦИНСКАЯ ФОРМУЛА ---
def calculate_norm(gender, age, height, weight, goal, activity):
    # Формула Миффлина - Сан Жеора
    if gender == 'M':
        bmr = (10 * weight) + (6.25 * height) - (5 * age) + 5
    else:
        bmr = (10 * weight) + (6.25 * height) - (5 * age) - 161
        
    act_mults = {"Низкая": 1.2, "Средняя": 1.55, "Высокая": 1.725}
    tdee = bmr * act_mults.get(activity, 1.2)
    
    if goal == "Похудение": tdee *= 0.8
    elif goal == "Набор массы": tdee *= 1.2
        
    return int(tdee)

# --- ЛОГИКА ПРОФИЛЯ ---
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = str(message.from_user.id)
    doc = db.collection('users').document(user_id).get()
    
    if doc.exists:
        await message.answer("Привет! 👋 Я твой ИИ-нутрициолог.\nЖду фото еды!", reply_markup=main_menu)
    else:
        await message.answer("Привет! 👋 Давай настроим твой профиль, чтобы я мог точно считать калории.\n\nУкажи свой пол:", reply_markup=gender_kb)
        await state.set_state(ProfileStates.gender)

@dp.callback_query(F.data == "save_to_diary")
async def save_to_diary(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    data = await state.get_data()
    last_response = data.get("last_ai_response")
    
    if not last_response:
        return await callback.answer("Нечего сохранять!", show_alert=True)

    # ДОБАВЛЯЕМ ВРЕМЯ К ЗАПИСИ, ЧТОБЫ ОНА БЫЛА УНИКАЛЬНОЙ
    current_time = datetime.now().strftime("%H:%M:%S")
    unique_record = f"⏰ Время записи: {current_time}\n{last_response}"

    db.collection('diaries').document(get_today_doc_id(user_id)).set({
        'meals': firestore.ArrayUnion([unique_record])
    }, merge=True)
    
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("✅ Блюдо записано в дневник!")
    await callback.answer()

@dp.message(ProfileStates.age)
async def ask_height(message: Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("Пожалуйста, введи только цифры (например: 25).")
    await state.update_data(age=int(message.text))
    await message.answer("Укажи свой рост в см (например: 170):")
    await state.set_state(ProfileStates.height)

@dp.message(ProfileStates.height)
async def ask_weight(message: Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("Пожалуйста, введи только цифры (например: 170).")
    await state.update_data(height=int(message.text))
    await message.answer("Укажи свой текущий вес в кг (можно с точкой, например: 65.5):")
    await state.set_state(ProfileStates.weight)

@dp.message(ProfileStates.weight)
async def ask_prof_goal(message: Message, state: FSMContext):
    try:
        weight = float(message.text.replace(',', '.'))
        await state.update_data(weight=weight)
        await message.answer("Какая у тебя цель?", reply_markup=goal_keyboard)
        await state.set_state(ProfileStates.goal)
    except ValueError:
        await message.answer("Пожалуйста, введи вес цифрами.")

@dp.callback_query(ProfileStates.goal, F.data.startswith("goal_"))
async def ask_prof_act(callback: CallbackQuery, state: FSMContext):
    goals = {"goal_loss": "Похудение", "goal_maintain": "Поддержание", "goal_gain": "Набор массы"}
    await state.update_data(goal=goals[callback.data])
    await callback.message.edit_text("Выбери уровень активности:", reply_markup=activity_keyboard)
    await state.set_state(ProfileStates.activity)

@dp.callback_query(ProfileStates.activity, F.data.startswith("act_"))
async def finish_profile(callback: CallbackQuery, state: FSMContext):
    acts = {"act_low": "Низкая", "act_med": "Средняя", "act_high": "Высокая"}
    user_data = await state.get_data()
    gender, age, height, weight, goal = user_data['gender'], user_data['age'], user_data['height'], user_data['weight'], user_data['goal']
    activity = acts[callback.data]
    
    norm = calculate_norm(gender, age, height, weight, goal, activity)
    
    db.collection('users').document(str(callback.from_user.id)).set({
        'gender': gender, 'age': age, 'height': height, 'weight': weight,
        'goal': goal, 'activity': activity, 'norm': norm
    })
    
    await callback.message.delete()
    await callback.message.answer(f"✅ Профиль создан!\n\nТвоя индивидуальная норма калорий: **{norm} ккал**.\nТеперь присылай фото еды, а я всё посчитаю!", reply_markup=main_menu)
    await state.clear()

@dp.message(F.text == "👤 Мой профиль")
async def show_my_profile(message: Message):
    doc = db.collection('users').document(str(message.from_user.id)).get()
    if not doc.exists:
        return await message.answer("Профиль не найден. Нажми /start для регистрации.")
    
    data = doc.to_dict()
    text = f"👤 **Твой профиль:**\n\nВес: {data['weight']} кг\nРост: {data['height']} см\nВозраст: {data['age']} лет\nЦель: {data['goal']}\n\n🔥 **Дневная норма: {data['norm']} ккал**"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚖️ Обновить вес", callback_data="update_weight")]])
    await message.answer(text, reply_markup=kb)

@dp.callback_query(F.data == "update_weight")
async def req_new_weight(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введи свой новый вес (кг):")
    await state.set_state(ProfileStates.new_weight)

@dp.message(ProfileStates.new_weight)
async def save_new_weight(message: Message, state: FSMContext):
    try:
        new_weight = float(message.text.replace(',', '.'))
        user_id = str(message.from_user.id)
        doc_ref = db.collection('users').document(user_id)
        data = doc_ref.get().to_dict()
        
        # Пересчитываем норму с новым весом!
        new_norm = calculate_norm(data['gender'], data['age'], data['height'], new_weight, data['goal'], data['activity'])
        doc_ref.update({'weight': new_weight, 'norm': new_norm})
        
        await message.answer(f"🎉 Вес обновлен!\nНовая норма пересчитана: **{new_norm} ккал**.", reply_markup=main_menu)
        await state.clear()
    except ValueError:
        await message.answer("Введи цифры.")

# --- ОТМЕНА ---
@dp.message(F.text == "❌ Сбросить шаг")
async def cancel_action(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🔄 Шаг отменен.", reply_markup=main_menu)

# --- ЛОГИКА ДНЕВНИКА С УЧЕТОМ НОРМЫ ---
def get_today_doc_id(user_id):
    today = datetime.now().strftime("%Y-%m-%d")
    return f"{user_id}_{today}"

@dp.message(F.text == "📊 Мой дневник")
async def show_diary(message: Message):
    user_id = str(message.from_user.id)
    
    # Достаем норму из профиля
    user_doc = db.collection('users').document(user_id).get()
    if not user_doc.exists:
        return await message.answer("Заполни профиль (/start), чтобы я мог считать остаток калорий.")
    norm = user_doc.to_dict().get('norm', 2000)
        
    doc_ref = db.collection('diaries').document(get_today_doc_id(user_id))
    doc = doc_ref.get()
    
    if not doc.exists or not doc.to_dict().get('meals'):
        return await message.answer(f"Дневник пуст! Отправь фото.\n(Твоя цель на сегодня: {norm} ккал)", reply_markup=main_menu)
    
    meals = doc.to_dict().get('meals', [])
    msg = await message.answer("📊 Считаю итоги за сегодня...")
    
    diary_text = "\n\n---\n".join(meals)
    prompt = f"Съедено сегодня:\n{diary_text}\n\nСделай отчет:\n1. Краткий список съеденного.\n2. ИТОГ КБЖУ.\n3. МОЯ НОРМА: {norm} ккал. Напиши, сколько калорий осталось съесть (или перебор)."
    
    try:
        ai_response = await ask_ai(text_prompt=prompt)
        await msg.edit_text(ai_response)
    except Exception:
        await msg.edit_text("Ошибка при анализе.")

@dp.callback_query(F.data == "save_to_diary")
async def save_to_diary(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    data = await state.get_data()
    last_response = data.get("last_ai_response")
    
    if not last_response:
        return await callback.answer("Нечего сохранять!", show_alert=True)

    db.collection('diaries').document(get_today_doc_id(user_id)).set({'meals': firestore.ArrayUnion([last_response])}, merge=True)
    
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("✅ Блюдо записано в дневник!")
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
    await message.answer("Супер! Уточни статус продукта:", reply_markup=status_keyboard)

@dp.callback_query(BotStates.waiting_for_status, F.data.in_(["status_raw", "status_cooked"]))
async def process_photo_status(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.edit_text("⏳ Нейросеть изучает фото...")
    user_data = await state.get_data()
    status_text = "СЫРЫЕ ПРОДУКТЫ" if callback.data == "status_raw" else "ГОТОВОЕ БЛЮДО"
    if user_data.get("photo_caption"): status_text += f". Коммент: {user_data.get('photo_caption')}"
        
    try:
        ai_response = await ask_ai(image_base64=user_data.get("saved_photo"), context=status_text)
        if await state.get_state() != BotStates.waiting_for_status.state: return 
            
        await state.update_data(last_ai_response=ai_response)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Записать в дневник", callback_data="save_to_diary")]])
        await callback.message.edit_text(ai_response, reply_markup=kb)
        await state.set_state(None) # ВАЖНО: не стираем память, просто снимаем состояние
    except Exception:
        await callback.message.edit_text("Ошибка.")
        await state.clear()

# --- ВЕБ-СЕРВЕР ДЛЯ RENDER ---
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
    # --- ЛОГИКА СОСТАВЛЕНИЯ МЕНЮ ---
@dp.message(F.text.contains("Составить меню"))async def start_menu_generation(message: Message, state: FSMContext):
    user_id = str(message.from_user.id)
    doc = db.collection('users').document(user_id).get()
    
    if not doc.exists:
        return await message.answer("Сначала заполни профиль (нажми /start), чтобы я знал твою норму калорий!")

    await message.answer("📝 Напиши список продуктов, которые у тебя сейчас есть (например: курица, гречка, яйца, помидоры):")
    await state.set_state(BotStates.waiting_for_menu_ingredients)

@dp.message(BotStates.waiting_for_menu_ingredients)
async def process_menu_ingredients(message: Message, state: FSMContext):
    await state.update_data(user_ingredients=message.text)
    await message.answer("Можно добавить базовые продукты (масло, специи, лук)?", reply_markup=extra_keyboard)
    await state.set_state(BotStates.waiting_for_extra_permission)

@dp.callback_query(BotStates.waiting_for_extra_permission, F.data.startswith("extra_"))
async def process_extra_permission(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.edit_text("🍳 Придумываю рецепт и считаю граммовки...")
    
    user_id = str(callback.from_user.id)
    user_data = db.collection('users').document(user_id).get().to_dict()
    state_data = await state.get_data()
    
    extra = "РАЗРЕШЕНО добавлять базу (масло, специи, лук и т.д.)." if callback.data == "extra_yes" else "СТРОГО ЗАПРЕЩЕНО добавлять чужие ингредиенты, только из моего списка."
    
    # Формируем умный запрос, используя норму из профиля
    prompt = f"""Составь меню на один прием пищи (завтрак, обед или ужин).
    Параметры человека: цель {user_data['goal']}, активность {user_data['activity']}. 
    Дневная норма калорий: {user_data['norm']} ккал.
    
    Ингредиенты: {state_data.get('user_ingredients')}.
    {extra}
    
    Напиши рецепт, укажи точные граммовки продуктов так, чтобы калорийность блюда составляла примерно 25-35% от дневной нормы ({user_data['norm']} ккал). В конце выведи итоговое КБЖУ блюда."""
    
    try:
        ai_response = await ask_ai(text_prompt=prompt)
        await callback.message.edit_text(ai_response)
    except Exception:
        await callback.message.edit_text("Произошла ошибка при составлении меню.")
    finally:
        await state.clear()
