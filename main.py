import asyncio
import os
import base64
import json
import re
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup, 
                           InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton)
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from openai import AsyncOpenAI
import firebase_admin
from firebase_admin import credentials, firestore

load_dotenv()

# --- ИНИЦИАЛИЗАЦИЯ ИИ ---
TOKEN = os.getenv('BOT_TOKEN')
AI_MODEL = os.getenv('AI_MODEL', 'gpt-4o') 
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
    db = None

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# --- СОСТОЯНИЯ ---
class Onboarding(StatesGroup):
    pace = State()
    name = State()
    gender = State()
    height = State()
    weight = State()
    age = State()
    body_type = State()
    goal = State()

class BotStates(StatesGroup):
    waiting_for_edit_text = State()
    waiting_for_recipe = State()
    waiting_for_fridge_ingredients = State()

# --- КЛАВИАТУРЫ ---
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📸 Фото еды"), KeyboardButton(text="📊 Дневник")],
        [KeyboardButton(text="📝 Прислать рецепт"), KeyboardButton(text="🧊 Рецепт из того что есть")]
    ],
    resize_keyboard=True,
    input_field_placeholder="Выбери действие..."
)

# --- МАТЕМАТИКА (Кэтч-Макардл) ---
def calculate_katch_mcardle(weight, body_fat_pct, goal):
    lbm = weight * (1 - (body_fat_pct / 100))
    bmr = 370 + (21.6 * lbm)
    tdee = bmr * 1.375 
    
    if goal == "loss": norm = tdee * 0.85
    elif goal == "gain": norm = tdee * 1.15
    else: norm = tdee
        
    p = int((norm * 0.30) / 4)
    f = int((norm * 0.30) / 9)
    c = int((norm * 0.40) / 4)
    
    return {'bmr': int(bmr), 'tdee': int(tdee), 'norm': int(norm), 'p': p, 'f': f, 'c': c}

def get_user_profile(user_id):
    if not db: return None
    doc = db.collection('users').document(str(user_id)).get()
    return doc.to_dict() if doc.exists else None

def get_today_doc_id(user_id):
    return f"{user_id}_{datetime.now().strftime('%Y-%m-%d')}"

async def ask_ai(image_base64=None, text_prompt=None, system_prompt="Ты AI-нутрициолог."):
    messages = [{"role": "system", "content": system_prompt}]
    if image_base64:
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": text_prompt or "Изучи фото."},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}", "detail": "low"}}
            ]
        })
    elif text_prompt:
        messages.append({"role": "user", "content": text_prompt})

    try:
        res = await client.chat.completions.create(model=AI_MODEL, messages=messages, temperature=0.2)
        return res.choices[0].message.content
    except Exception as e:
        return f"Ошибка ИИ: {e}"

# ==========================================
# 🚀 ОНБОРДИНГ (МАГНИТ ДЛЯ ПРОДАЖ)
# ==========================================
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    if get_user_profile(message.from_user.id):
        return await message.answer("С возвращением! Жду фото еды или команду 👇", reply_markup=main_menu)
    
    text = (
        "Привет! Я <b>Артём</b> — твой AI-нутрициолог 🥗\n\n"
        "Моя философия: мы делаем форму <b>не «к лету», а навсегда</b>. "
        "Без жестких диет, без чувства вины за съеденную шоколадку и с полным принятием себя.\n\n"
        "Готов(а) начать этот путь вместе со мной?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🤝 Договор", callback_data="onb_agreement")]])
    await message.answer(text, reply_markup=kb)

@dp.callback_query(F.data == "onb_agreement")
async def onb_pace(callback: CallbackQuery, state: FSMContext):
    text = "Супер! Какой темп работы выберем на старте?"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Полный вперёд", callback_data="pace_fast")],
        [InlineKeyboardButton(text="👀 Давай понаблюдаем", callback_data="pace_slow")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)

@dp.callback_query(F.data.startswith("pace_"))
async def onb_name(callback: CallbackQuery, state: FSMContext):
    await state.update_data(pace=callback.data)
    await callback.message.edit_text("Отличный настрой! Как мне к тебе обращаться? (напиши имя)")
    await state.set_state(Onboarding.name)

@dp.message(Onboarding.name)
async def onb_gender(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👨 Мужской", callback_data="gender_M"), 
         InlineKeyboardButton(text="👩 Женский", callback_data="gender_F")]
    ])
    await message.answer(f"Приятно познакомиться, {message.text}! Укажи свой пол:", reply_markup=kb)

@dp.callback_query(F.data.startswith("gender_"))
async def onb_height(callback: CallbackQuery, state: FSMContext):
    await state.update_data(gender=callback.data.split("_")[1])
    await callback.message.edit_text("Укажи свой рост (в см):")
    await state.set_state(Onboarding.height)

@dp.message(Onboarding.height)
async def onb_weight(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("Напиши цифрами (например, 175).")
    await state.update_data(height=int(message.text))
    await message.answer("Укажи свой текущий вес (в кг):")
    await state.set_state(Onboarding.weight)

@dp.message(Onboarding.weight)
async def onb_age(message: Message, state: FSMContext):
    try:
        w = float(message.text.replace(',', '.'))
        await state.update_data(weight=w)
        await message.answer("Сколько тебе лет?")
        await state.set_state(Onboarding.age)
    except:
        await message.answer("Напиши цифрами (например, 65.5).")

@dp.message(Onboarding.age)
async def onb_body_type(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("Только цифры.")
    await state.update_data(age=int(message.text))
    
    text = (
        "Почти всё! Чтобы я рассчитал норму идеально точно, выбери свой тип фигуры:\n\n"
        "1️⃣ Худощавое телосложение, минимум жира\n"
        "2️⃣ Обычное / Спортивное телосложение\n"
        "3️⃣ Есть заметный лишний вес / животик\n"
        "4️⃣ Плотное телосложение / Ожирение"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1️⃣", callback_data="body_1"), InlineKeyboardButton(text="2️⃣", callback_data="body_2")],
        [InlineKeyboardButton(text="3️⃣", callback_data="body_3"), InlineKeyboardButton(text="4️⃣", callback_data="body_4")]
    ])
    await message.answer(text, reply_markup=kb)

@dp.callback_query(F.data.startswith("body_"))
async def onb_goal(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    gender = data.get("gender")
    b_type = callback.data.split("_")[1]
    
    bf_map = {
        'M': {'1': 12, '2': 18, '3': 25, '4': 35},
        'F': {'1': 18, '2': 24, '3': 32, '4': 40}
    }
    await state.update_data(body_fat=bf_map[gender][b_type])
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📉 Сбросить вес", callback_data="goal_loss")],
        [InlineKeyboardButton(text="⚖️ Поддержание", callback_data="goal_maintain")],
        [InlineKeyboardButton(text="📈 Набрать массу", callback_data="goal_gain")]
    ])
    await callback.message.edit_text("Какая у нас глобальная цель?", reply_markup=kb)

@dp.callback_query(F.data.startswith("goal_"))
async def onb_finish(callback: CallbackQuery, state: FSMContext):
    goal = callback.data.split("_")[1]
    data = await state.get_data()
    
    calc = calculate_katch_mcardle(data['weight'], data['body_fat'], goal)
    
    if db:
        db.collection('users').document(str(callback.from_user.id)).set({
            'name': data.get('name', 'друг'), 'gender': data['gender'], 'age': data['age'], 
            'height': data['height'], 'weight': data['weight'], 'body_fat': data['body_fat'],
            'goal': goal, 'norm': calc['norm'], 'p': calc['p'], 'f': calc['f'], 'c': calc['c'],
            'created_at': firestore.SERVER_TIMESTAMP
        })

    text = (
        f"🎉 <b>Профиль готов!</b>\n___\n"
        f"Твоя цель: <b>{calc['norm']} ккал</b> в день.\n"
        f"Б: {calc['p']}г | Ж: {calc['f']}г | У: {calc['c']}г\n___\n"
        f"<i>Я рассчитал это по научному методу Кэтча-Макардла.\n\n"
        f"Теперь мы работаем каждый день. Присылай мне фото еды, а я буду вести твои расчеты.</i> 👇"
    )
    await callback.message.edit_text(text)
    await callback.message.answer("Главное меню:", reply_markup=main_menu)
    await state.clear()

# ==========================================
# 🍔 РАСПОЗНАВАНИЕ ЕДЫ И ДНЕВНИК
# ==========================================
@dp.message(F.text == "📸 Фото еды")
async def wait_for_photo(message: Message):
    await message.answer("Пришли мне фото твоей тарелки прямо в этот чат 📸")

@dp.message(F.photo)
async def handle_photo(message: Message, state: FSMContext):
    wait_msg = await message.answer("👀 Анализирую...")
    try:
        await state.clear()
        file = await bot.get_file(message.photo[-1].file_id)
        d_file = await bot.download_file(file.file_path)
        encoded_photo = base64.b64encode(d_file.read()).decode('utf-8')
        
        prompt = (
            "Напиши КРАТКО, что на тарелке и примерный вес (БЕЗ калорий). "
            "Ответь СТРОГО по шаблону:\n"
            "🍽 <b>[Название блюда]</b>\n___\n"
            "• [ингредиент] — [вес] г\n___\n"
            "⚖️ Общий вес порции: ~[вес] г"
        )
        food_desc = await ask_ai(image_base64=encoded_photo, text_prompt=prompt)
        await state.update_data(saved_photo=encoded_photo, recognized_food=food_desc)
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Всё верно", callback_data="food_correct")],
            [InlineKeyboardButton(text="✏️ Поправить", callback_data="food_edit"),
             InlineKeyboardButton(text="❌ Не то", callback_data="food_wrong")]
        ])
        await wait_msg.edit_text(f"{food_desc}\n___\nВсё верно? Жду подтверждения для расчета.", reply_markup=kb)
    except Exception:
        await wait_msg.edit_text("Ошибка распознавания.")

@dp.callback_query(F.data == "food_correct")
async def calc_food(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    food_desc = data.get("recognized_food", "")
    await callback.message.edit_text(f"{food_desc}\n\n⏳ Считаю калории...")
    
    u_data = get_user_profile(callback.from_user.id)
    norm = u_data['norm'] if u_data else 2000
    
    prompt = (
        f"Пользователь съел: {food_desc}\nЕго дневная норма: {norm} ккал. Посчитай КБЖУ.\n"
        "Ответь СТРОГО по шаблону (используй HTML теги <b> и <i>):\n"
        "✅ <b>[Название блюда]</b>\n___\n"
        "• [ингредиент] <b>[вес] г</b> — [ккал] ккал\n"
        "<i>Б[белки] Ж[жиры] У[углеводы]</i>\n___\n"
        "<b>Итого: [сумма_ккал] ккал</b>\n"
        "<b>Б [б] · Ж [ж] · У [у]</b>\n___\n"
        "💬 [Комментарий]"
    )
    res = await ask_ai(text_prompt=prompt)
    await state.update_data(last_ai_response=res)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ В дневник", callback_data="save_to_diary")]])
    await callback.message.edit_text(res, reply_markup=kb)

@dp.callback_query(F.data == "food_edit")
async def edit_food(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Напиши текстом, что исправить (например: 'курицы 250г, а не 150г'):")
    await state.set_state(BotStates.waiting_for_edit_text)

@dp.message(BotStates.waiting_for_edit_text)
async def process_edit(message: Message, state: FSMContext):
    data = await state.get_data()
    wait_msg = await message.answer("⏳ Пересчитываю...")
    prompt = (
        f"Прошлый ответ: {data.get('recognized_food')}\nИсправление: {message.text}\n"
        "Выдай новый состав СТРОГО по шаблону (используй HTML теги):\n"
        "🍽 <b>[Название]</b>\n___\n• [инг] — [вес] г\n___\n⚖️ Общий вес: ~[вес] г"
    )
    new_desc = await ask_ai(text_prompt=prompt)
    await state.update_data(recognized_food=new_desc)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Всё верно", callback_data="food_correct")],
        [InlineKeyboardButton(text="✏️ Поправить еще", callback_data="food_edit")]
    ])
    await wait_msg.edit_text(f"{new_desc}\n___\nВсё верно? После подтверждения посчитаю КБЖУ.", reply_markup=kb)
    await state.set_state(None)

@dp.callback_query(F.data == "food_wrong")
async def wrong_food(callback: CallbackQuery):
    await callback.message.edit_text("Понял, промахнулся 🙈 Напиши текстом или сфоткай заново.")

@dp.callback_query(F.data == "save_to_diary")
async def save_diary(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    text = data.get("last_ai_response", "")
    user_id = str(callback.from_user.id)
    
    record = f"{datetime.now().strftime('%H:%M')} · {text.split('___')[0].replace('✅', '').strip()}"
    if db: 
        db.collection('diaries').document(get_today_doc_id(user_id)).set({'meals': firestore.ArrayUnion([record])}, merge=True)
    
    await callback.message.edit_text(f"✅ Записано в дневник!\n\n{text}")
    
    u_data = get_user_profile(user_id)
    doc = db.collection('diaries').document(get_today_doc_id(user_id)).get()
    meals = doc.to_dict().get('meals', []) if doc.exists else []
    
    res = await ask_ai(text_prompt=f"Съедено сегодня: {meals}. Норма: {u_data['norm']}. Вычти сумму калорий из нормы. Напиши СТРОГО: 'Остаток на сегодня: [число] ккал.'")
    await callback.message.answer(f"📊 {res}")

# --- ОТОБРАЖЕНИЕ ДНЕВНИКА ---
@dp.message(F.text == "📊 Дневник")
async def show_diary_btn(message: Message):
    user_id = str(message.from_user.id)
    u_data = get_user_profile(user_id)
    if not u_data: return await message.answer("Нажми /start")
    
    doc = db.collection('diaries').document(get_today_doc_id(user_id)).get()
    meals = doc.to_dict().get('meals', []) if doc.exists else []
    
    header = f"📊 <b>ДНЕВНИК ЗА СЕГОДНЯ</b>\n___\n"
    if not meals:
        return await message.answer(header + "Пока пусто. Пришли фото еды — я всё посчитаю 📸")
        
    msg = await message.answer("⏳ Собираю дневник...")
    meals_text = "\n".join(meals)
    prompt = (
        f"Список еды: {meals_text}\nНорма: {u_data['norm']} ккал, Б:{u_data.get('p')} Ж:{u_data.get('f')} У:{u_data.get('c')}\n"
        "Выдай отчет СТРОГО по шаблону (используй HTML):\n"
        "[Список еды 그대로 из текста пользователя, разделенный ___]\n___\n"
        "🔥 Калории <b>[сумма]</b> / [норма]\n"
        "🥩 Белки <b>[сумма]</b> / [норма] г\n"
        "🥑 Жиры <b>[сумма]</b> / [норма] г\n"
        "🍚 Углеводы <b>[сумма]</b> / [норма] г\n___\n"
        "Осталось на сегодня: <b>[остаток] ккал</b>"
    )
    res = await ask_ai(text_prompt=prompt)
    await msg.edit_text(header + res)

# ==========================================
# 📝 РЕЦЕПТЫ И ХОЛОДИЛЬНИК
# ==========================================
@dp.message(F.text == "📝 Прислать рецепт")
async def ask_recipe(message: Message, state: FSMContext):
    await message.answer("Какое блюдо хочешь приготовить? Напиши название, и я рассчитаю граммовки под твой остаток калорий! 👨‍🍳")
    await state.set_state(BotStates.waiting_for_recipe)

@dp.message(BotStates.waiting_for_recipe)
async def generate_recipe(message: Message, state: FSMContext):
    msg = await message.answer("⏳ Сочиняю рецепт под твою норму...")
    u_data = get_user_profile(message.from_user.id)
    
    prompt = (
        f"Пользователь хочет приготовить: {message.text}.\n"
        f"Его дневная норма: {u_data['norm']} ккал. "
        "Сделай рецепт на 1 порцию. Распиши СТРОГО с использованием HTML (<b>, <i>):\n"
        "🍽 <b>[Название]</b>\n___\n"
        "<b>Ингредиенты:</b>\n• [ингредиент] — [вес] г\n___\n"
        "<b>Рецепт:</b>\n1. [шаг]\n___\n"
        "<b>КБЖУ:</b> [ккал] ккал | Б:[б] Ж:[ж] У:[у]"
    )
    res = await ask_ai(text_prompt=prompt)
    await msg.edit_text(res)
    await state.clear()

@dp.message(F.text == "🧊 Рецепт из того что есть")
async def fridge_menu(message: Message, state: FSMContext):
    await message.answer("Напиши список продуктов через запятую, и я соберу из них крутое блюдо (базовые специи и масло добавляю сам):")
    await state.set_state(BotStates.waiting_for_fridge_ingredients)

@dp.message(BotStates.waiting_for_fridge_ingredients)
async def generate_fridge_menu(message: Message, state: FSMContext):
    msg = await message.answer("⏳ Сочиняю рецепт...")
    u_data = get_user_profile(message.from_user.id)
    prompt = (
        f"У пользователя есть продукты: {message.text}. Разрешено использовать базовое масло и специи.\n"
        f"Его дневная норма {u_data['norm']} ккал. "
        "Придумай блюдо на 1 порцию. Распиши СТРОГО с HTML:\n"
        "🍽 <b>[Название]</b>\n___\n"
        "<b>Ингредиенты:</b>\n• [ингредиент] — [вес] г\n___\n"
        "<b>Рецепт:</b>\n1. [шаг]\n___\n"
        "<b>КБЖУ:</b> [ккал] ккал | Б:[б] Ж:[ж] У:[у]"
    )
    res = await ask_ai(text_prompt=prompt)
    await msg.edit_text(res)
    await state.clear()

# ==========================================
# ⚙️ ЕЖЕДНЕВНЫЙ ЦИКЛ (СЕРВЕР И CRON)
# ==========================================
async def broadcast_morning():
    """Рассылка каждое утро в 09:00"""
    if not db: return
    users_ref = db.collection('users').stream()
    for doc in users_ref:
        try:
            await bot.send_message(doc.id, "Доброе утро! ☀️ Не забудь сфотографировать свой завтрак — я всё посчитаю!")
        except: pass

async def broadcast_afternoon():
    """Проактивный вопрос каждый день в 15:00"""
    if not db: return
    users_ref = db.collection('users').stream()
    for doc in users_ref:
        try:
            await bot.send_message(doc.id, "Как успехи? 🍽 Помочь собрать меню на ужин или завтрашний день? Жми 'Прислать рецепт' в меню!")
        except: pass

async def broadcast_evening():
    """Итоги дня каждый вечер в 20:00"""
    if not db: return
    users_ref = db.collection('users').stream()
    for doc in users_ref:
        try:
            await bot.send_message(doc.id, "День подходит к концу! 🌙 Загляни в 'Дневник', чтобы проверить свой остаток калорий. Ты молодец!")
        except: pass

async def health_check(request): 
    return web.Response(text="Bot is running!")

async def main():
    try:
        # Планировщик ежедневного цикла
        scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
        scheduler.add_job(broadcast_morning, trigger=CronTrigger(hour=9, minute=0))
        scheduler.add_job(broadcast_afternoon, trigger=CronTrigger(hour=15, minute=0))
        scheduler.add_job(broadcast_evening, trigger=CronTrigger(hour=20, minute=0))
        scheduler.start()

        # Сервер для Render
        app = web.Application()
        app.router.add_get('/', health_check)
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.environ.get("PORT", 8080))
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    except Exception as e:
        print(f"Критическая ошибка: {e}")

if __name__ == "__main__":
    asyncio.run(main())
