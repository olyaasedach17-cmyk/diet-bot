import asyncio
import os
import base64
import json
import re
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup, 
                           InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, BotCommand)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from openai import AsyncOpenAI
import firebase_admin
from firebase_admin import credentials, firestore

load_dotenv()

# --- ИНИЦИАЛИЗАЦИЯ ИИ ---
TOKEN = os.getenv('BOT_TOKEN')
# По умолчанию ставим Луну, если в переменных окружения вдруг пусто
AI_MODEL = os.getenv('AI_MODEL', 'openai/gpt-5.6-luna') 
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

# --- НАСТРОЙКА КНОПКИ "МЕНЮ" СЛЕВА ВНИЗУ (СИНЯЯ) ---
async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="today", description="Дневник за сегодня"),
        BotCommand(command="week", description="Неделя в цифрах"),
        BotCommand(command="plan", description="Моя норма КБЖУ"),
        BotCommand(command="fridge", description="Меню из холодильника"),
        BotCommand(command="weight", description="Динамика веса"),
        BotCommand(command="profile", description="Профиль и цель"),
        BotCommand(command="help", description="Как пользоваться")
    ]
    await bot.set_my_commands(commands)

# --- ГЛАВНАЯ КЛАВИАТУРА ВНИЗУ (КАК НА СКРИНЕ) ---
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Сегодня"), KeyboardButton(text="🥗 Что приготовить")],
        [KeyboardButton(text="⚖️ Вес"), KeyboardButton(text="👤 Профиль")]
    ],
    resize_keyboard=True,
    input_field_placeholder="Пришли фото ед..."
)

# --- СОСТОЯНИЯ ---
class Onboarding(StatesGroup):
    gender = State()
    stats = State()
    goal = State()
    activity = State()

class BotStates(StatesGroup):
    waiting_for_edit_text = State()
    waiting_for_fridge_ingredients = State()

# --- МАТЕМАТИКА (Миффлин-Сан Жеор) ---
def calculate_mifflin(gender, age, height, weight, goal_code, activity_code):
    if gender == 'M': bmr = (10 * weight) + (6.25 * height) - (5 * age) + 5
    else: bmr = (10 * weight) + (6.25 * height) - (5 * age) - 161
    
    act_mults = {"act_low": 1.2, "act_light": 1.375, "act_med": 1.55, "act_high": 1.725, "act_pro": 1.9}
    tdee = bmr * act_mults.get(activity_code, 1.2)
    
    deficit_pct = 0
    if goal_code == "loss": 
        norm = tdee * 0.78 # -22%
        deficit_pct = -22
    elif goal_code == "gain": 
        norm = tdee * 1.15
        deficit_pct = 15
    else: 
        norm = tdee
        
    p = int((norm * 0.27) / 4)
    f = int((norm * 0.40) / 9)
    c = int((norm * 0.33) / 4)
    
    return {'bmr': int(bmr), 'tdee': int(tdee), 'norm': int(norm), 'p': p, 'f': f, 'c': c, 'deficit': deficit_pct}

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
        print(f"🔥 ОШИБКА ИИ: {e}")
        return f"Ошибка связи с нейросетью. Попробуй еще раз."

# ==========================================
# 🚀 ОНБОРДИНГ
# ==========================================
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    if get_user_profile(message.from_user.id):
        return await message.answer("С возвращением! Пришли фото еды — я всё посчитаю 📸", reply_markup=main_menu)
    
    text = (
        f"Привет, {message.from_user.first_name or 'друг'} 🕊! Это «Умная Тарелка» — я <b>Артём</b>, твой нутрициолог в телефоне 🥗\n\n"
        "Что я умею:\n"
        "📸 считать КБЖУ по фото еды — просто сфоткай тарелку;\n"
        "📊 вести дневник, чтобы ты не выходил за свою норму;\n"
        "🧊 собирать меню из того, что лежит в холодильнике;\n"
        "❓ отвечать на вопросы про питание — текстом или голосом.\n\n"
        "Сначала короткий опрос — <b>4 вопроса</b>, меньше минуты. Он нужен, чтобы посчитать <b>твою</b> норму, а не среднюю по больнице."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚀 Начать", callback_data="start_onb")]])
    await message.answer(text, reply_markup=main_menu) 
    await message.answer("Жми кнопку ниже 👇", reply_markup=kb)

@dp.callback_query(F.data == "start_onb")
async def step_1(callback: CallbackQuery, state: FSMContext):
    text = "<i>Шаг 1 из 4</i>\n\nТвой <b>пол</b>?\n<i>От него зависит формула расчёта.</i>"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👨 Мужчина", callback_data="gender_M")],
        [InlineKeyboardButton(text="👩 Женщина", callback_data="gender_F")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)

@dp.callback_query(F.data.startswith("gender_"))
async def step_2(callback: CallbackQuery, state: FSMContext):
    await state.update_data(gender=callback.data.split("_")[1])
    text = (
        "<i>Шаг 2 из 4</i>\n\n"
        "Напиши в одну строку через пробел:\n"
        "<b>возраст, рост в см и вес в кг.</b>\n\n"
        "Например: 32 182 92\n"
        "<i>Можно и словами — «мне 32, рост 182, вешу 92,4».</i>"
    )
    await callback.message.edit_text(text)
    await state.set_state(Onboarding.stats)

@dp.message(Onboarding.stats)
async def step_3(message: Message, state: FSMContext):
    numbers = re.findall(r'\d+[.,]?\d*', message.text)
    if len(numbers) < 3: return await message.answer("Пожалуйста, напиши 3 цифры (возраст, рост, вес).")
    
    age, height = int(float(numbers[0].replace(',','.'))), int(float(numbers[1].replace(',','.')))
    weight = float(numbers[2].replace(',','.'))
    await state.update_data(age=age, height=height, weight=weight)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📉 Похудеть", callback_data="goal_loss")],
        [InlineKeyboardButton(text="⚖️ Удержать вес", callback_data="goal_maintain")],
        [InlineKeyboardButton(text="📈 Набрать массу", callback_data="goal_gain")]
    ])
    await message.answer("<i>Шаг 3 из 4</i>\n\nКакая <b>цель</b>?", reply_markup=kb)

@dp.callback_query(F.data.startswith("goal_"))
async def step_4(callback: CallbackQuery, state: FSMContext):
    await state.update_data(goal=callback.data.split("_")[1])
    text = "<i>Шаг 4 из 4</i>\n\nИ последнее — какая у тебя <b>физическая активность</b>?"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛋 Сидячий", callback_data="act_low")],
        [InlineKeyboardButton(text="🚶‍♀️ Лёгкая (1–2 трен.)", callback_data="act_light")],
        [InlineKeyboardButton(text="🏃‍♂️ Умеренная (3–4)", callback_data="act_med")],
        [InlineKeyboardButton(text="🏋️‍♂️ Высокая (5–6)", callback_data="act_high")],
        [InlineKeyboardButton(text="🏅 Спортивная (2/день)", callback_data="act_pro")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)

@dp.callback_query(F.data.startswith("act_"))
async def finish_onboarding(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    calc = calculate_mifflin(data['gender'], data['age'], data['height'], data['weight'], data['goal'], callback.data)
    
    goal_str = "Похудеть" if data['goal'] == 'loss' else "Набрать массу" if data['goal'] == 'gain' else "Удержать вес"
    target_date = (datetime.now() + timedelta(days=23*7)).strftime("%d.%m.%Y")
    
    if db:
        db.collection('users').document(str(callback.from_user.id)).set({
            'gender': data['gender'], 'age': data['age'], 'height': data['height'], 'weight': data['weight'],
            'goal': goal_str, 'norm': calc['norm'], 'p': calc['p'], 'f': calc['f'], 'c': calc['c']
        })

    result_text = (
        "🎯 <b>ТВОЯ НОРМА НА ДЕНЬ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🔥 <b>{calc['norm']} ккал</b>\n\n"
        f"🥩 Белки <b>{calc['p']} г</b> 27%\n"
        f"🥑 Жиры <b>{calc['f']} г</b> 40%\n"
        f"🍚 Углеводы <b>{calc['c']} г</b> 33%\n"
        "🌾 Клетчатка 24 г 💧 Вода 2.6 л\n\n"
        f"Обмен покоя: <b>{calc['bmr']}</b> · расход за день: <b>{calc['tdee']}</b>\n"
        f"Цель «{goal_str}» → дефицит <b>{calc['deficit']}%</b>\n"
        f"Темп ≈ <b>0.44 кг/нед</b> → цель к <b>{target_date}</b>\n"
        "<i>Расчёт по формуле Миффлина. Уточню его, когда определим твой процент жира — станет точнее.</i>"
    )
    
    guide_text = (
        "📸 <b>Как пользоваться дальше</b>\n"
        "Сфоткай тарелку — я скажу, что вижу, спрошу «верно?» и посчитаю КБЖУ.\n"
        "Можно и словами: съел 2 яйца и кашу. А если нечего есть — пришли фото холодильника, соберу меню 🧊\n\n"
        "<i>Остальное про тебя узнаю по ходу — буду иногда задавать по одному короткому вопросу, чтобы советы были точнее.</i>"
    )
    
    await callback.message.edit_text(result_text)
    await callback.message.answer(guide_text, reply_markup=main_menu)
    await state.clear()

# ==========================================
# 🍔 РАСПОЗНАВАНИЕ ФОТО 
# ==========================================
@dp.message(F.photo)
async def handle_photo(message: Message, state: FSMContext):
    wait_msg = await message.answer("👀 Анализирую...")
    try:
        await state.clear()
        file = await bot.get_file(message.photo[-1].file_id)
        d_file = await bot.download_file(file.file_path)
        encoded_photo = base64.b64encode(d_file.read()).decode('utf-8')
        
        prompt = (
            "Напиши КРАТКО, что на тарелке и вес (БЕЗ калорий). Шаблон:\n"
            "🍽 <b>[Название блюда]</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "• [ингредиент] — [вес] г\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "⚖️ Общий вес порции: ~[вес] г"
        )
        food_desc = await ask_ai(image_base64=encoded_photo, text_prompt=prompt)
        await state.update_data(saved_photo=encoded_photo, recognized_food=food_desc)
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Всё верно", callback_data="food_correct")],
            [InlineKeyboardButton(text="✏️ Поправить", callback_data="food_edit"),
             InlineKeyboardButton(text="❌ Не то", callback_data="food_wrong")]
        ])
        await wait_msg.edit_text(f"{food_desc}\n\nВсё верно? После подтверждения посчитаю КБЖУ.", reply_markup=kb)
    except Exception:
        await wait_msg.edit_text("Ошибка. Попробуй еще раз.")

@dp.callback_query(F.data == "food_correct")
async def calc_food(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    food_desc = data.get("recognized_food", "")
    await callback.message.edit_text(f"{food_desc}\n\n⏳ Считаю калории...")
    
    u_data = get_user_profile(callback.from_user.id)
    norm = u_data.get('norm', 2000) if u_data else 2000
    
    prompt = (
        f"Съедено: {food_desc}\nНорма: {norm} ккал.\n"
        "Ответь СТРОГО по шаблону (как на скрине):\n"
        "✅ <b>[Название блюда]</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "• [ингредиент] <b>[вес] г</b> — [ккал] ккал\n"
        "<i>Б[б] Ж[ж] У[у]</i>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>Итого: [сумма] ккал</b> · вес порции ~[вес] г\n"
        "<b>Б [б] · Ж [ж] · У [у]</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💬 [Комментарий и совет]"
    )
    res = await ask_ai(text_prompt=prompt)
    await state.update_data(last_ai_response=res)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Дневник", callback_data="save_to_diary"),
         InlineKeyboardButton(text="🗑 Удалить", callback_data="undo_diary")]
    ])
    await callback.message.edit_text(res, reply_markup=kb)

@dp.callback_query(F.data == "food_edit")
async def edit_food(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Напиши текстом, что исправить (например: 'курицы 250г, а не 150г'):")
    await state.set_state(BotStates.waiting_for_edit_text)

@dp.message(BotStates.waiting_for_edit_text)
async def process_edit(message: Message, state: FSMContext):
    data = await state.get_data()
    wait_msg = await message.answer("⏳ Пересчитываю...")
    prompt = f"Прошлый ответ: {data.get('recognized_food')}\nИсправление: {message.text}\nВыдай новый состав по старому шаблону."
    new_desc = await ask_ai(text_prompt=prompt)
    await state.update_data(recognized_food=new_desc)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Всё верно", callback_data="food_correct")],
        [InlineKeyboardButton(text="✏️ Поправить еще", callback_data="food_edit")]
    ])
    await wait_msg.edit_text(f"{new_desc}\n\nВсё верно? После подтверждения посчитаю КБЖУ.", reply_markup=kb)
    await state.set_state(None)

@dp.callback_query(F.data == "food_wrong")
async def wrong_food(callback: CallbackQuery):
    await callback.message.edit_text("Понял, промахнулся 🙈 Напиши текстом или сфоткай заново.")

@dp.callback_query(F.data == "save_to_diary")
async def save_diary(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    text = data.get("last_ai_response", "")
    user_id = str(callback.from_user.id)
    
    title_match = re.search(r'✅\s*<b>(.*?)</b>', text)
    title = title_match.group(1) if title_match else "Блюдо"
    kcal_match = re.search(r'Итого:\s*(\d+)', text)
    kcal = kcal_match.group(1) if kcal_match else "0"
    macros_match = re.search(r'Б\s*([\d.]+)\s*·\s*Ж\s*([\d.]+)\s*·\s*У\s*([\d.]+)', text)
    b, j, u = macros_match.groups() if macros_match else ("0", "0", "0")
    
    time_str = datetime.now().strftime('%H:%M')
    record = f"{time_str} · {title} — <b>{kcal} ккал</b>\n<i>Б{b} Ж{j} У{u}</i>"
    
    if db: db.collection('diaries').document(get_today_doc_id(user_id)).set({'meals': firestore.ArrayUnion([record]), 'total_kcal': firestore.Increment(int(kcal))}, merge=True)
    
    await callback.answer("Сохранено в дневник!", show_alert=False)
    await cmd_today(callback.message, state, user_id=user_id)

@dp.callback_query(F.data == "undo_diary")
async def undo_diary_action(callback: CallbackQuery):
    await callback.message.edit_text("❌ Запись удалена.")

# ==========================================
# 📊 МЕНЮ (НИЖНИЕ КНОПКИ + СИНЯЯ КНОПКА)
# ==========================================
@dp.message(F.text == "📊 Сегодня")
@dp.message(Command("today"))
async def cmd_today(message: Message, state: FSMContext = None, user_id: str = None):
    if not user_id: user_id = str(message.from_user.id)
    u_data = get_user_profile(user_id)
    if not u_data: return await message.answer("Нажми /start")
    
    doc = db.collection('diaries').document(get_today_doc_id(user_id)).get()
    meals = doc.to_dict().get('meals', []) if doc.exists else []
    
    date_str = datetime.now().strftime("%d.%m, %A").lower()
    header = f"📊 <b>ДНЕВНИК ЗА СЕГОДНЯ</b>\n<i>{date_str}</i>\n━━━━━━━━━━━━━━━━━━━━\n"
    
    if not meals:
        await send_empty_diary(message, header, u_data)
        return
        
    msg = await message.answer("⏳ Обновляю дневник...")
    meals_text = "\n\n".join(meals)
    
    # ИСПРАВЛЕНА ОШИБКА KEYERROR ЗДЕСЬ (используем .get)
    prompt = (
        f"Список съеденного:\n{meals_text}\n\nНорма: {u_data.get('norm', 0)} ккал, Б:{u_data.get('p', 0)} Ж:{u_data.get('f', 0)} У:{u_data.get('c', 0)}\n"
        "Сделай красивый дневник как на фото. Шаблон:\n"
        "[Вставь список съеденного как есть]\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔥 Калории <b>[сумма]</b> / [норма] ([процент]%)\n[10 символов ■/□]\n"
        "🥩 Белки <b>[сумма]</b> / [норма] г\n[10 символов ■/□]\n"
        "🥑 Жиры <b>[сумма]</b> / [норма] г\n[10 символов ■/□]\n"
        "🍚 Углеводы <b>[сумма]</b> / [норма] г\n[10 символов ■/□]\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Осталось на сегодня: <b>[остаток] ккал</b>"
    )
    res = await ask_ai(text_prompt=prompt)
    if isinstance(message, Message): await msg.edit_text(header + res)
    else: await message.edit_text(header + res)

async def send_empty_diary(message, header, u_data):
    body = "Пока пусто. Пришли фото еды — я всё посчитаю 📸\n━━━━━━━━━━━━━━━━━━━━\n"
    body += f"🔥 Калории <b>0</b> / {u_data.get('norm', 0)} (0%)\n□□□□□□□□□□\n"
    body += f"🥩 Белки <b>0</b> / {u_data.get('p', 0)} г\n□□□□□□□□□□\n"
    body += f"🥑 Жиры <b>0</b> / {u_data.get('f', 0)} г\n□□□□□□□□□□\n"
    body += f"🍚 Углеводы <b>0</b> / {u_data.get('c', 0)} г\n□□□□□□□□□□\n━━━━━━━━━━━━━━━━━━━━\n"
    body += f"Осталось на сегодня: <b>{u_data.get('norm', 0)} ккал</b>"
    if isinstance(message, Message): await message.answer(header + body)
    else: await message.edit_text(header + body)

@dp.message(F.text == "🥗 Что приготовить")
@dp.message(Command("fridge"))
async def cmd_fridge(message: Message, state: FSMContext):
    await message.answer("Напиши список продуктов через запятую, и я соберу из них меню 🧊:")
    await state.set_state(BotStates.waiting_for_fridge_ingredients)

@dp.message(BotStates.waiting_for_fridge_ingredients)
async def gen_fridge(message: Message, state: FSMContext):
    msg = await message.answer("⏳ Сочиняю рецепт...")
    res = await ask_ai(text_prompt=f"Продукты: {message.text}. Придумай вкусный рецепт с КБЖУ.")
    await msg.edit_text(res)
    await state.clear()

@dp.message(F.text == "⚖️ Вес")
@dp.message(Command("weight"))
async def cmd_weight(message: Message): 
    await message.answer("Динамика веса в разработке 🚀")

@dp.message(F.text == "👤 Профиль")
@dp.message(Command("profile"))
async def cmd_prof(message: Message): 
    await message.answer("Раздел профиля в разработке 🚀")

@dp.message(Command("plan"))
async def cmd_plan(message: Message):
    u = get_user_profile(message.from_user.id)
    if u: await message.answer(f"🎯 Твоя норма: <b>{u.get('norm', 0)} ккал</b>\nБ: {u.get('p', 0)}г | Ж: {u.get('f', 0)}г | У: {u.get('c', 0)}г")

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer("📸 Просто сфоткай еду, а я её распознаю и посчитаю. Всё управление — через нижние кнопки или синее <b>☰ Меню</b>.")

@dp.message(Command("week"))
async def cmd_week(message: Message): 
    await message.answer("Статистика за неделю в разработке 🚀")

# --- СЕРВЕР И ЗАПУСК ---
async def health_check(request):
    return web.Response(text="Я бот, я жив, не убивай меня, Timeweb!")

async def main():
    try:
        # ЗАПУСК ФЕЙКОВОГО СЕРВЕРА ДЛЯ TIMEWEB
        app = web.Application()
        app.router.add_get('/', health_check)
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.environ.get("PORT", 8080))
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        print(f"✅ Сервер-обманка запущен на порту {port}")
        
        # ЗАЩИТА ОТ ТАЙМАУТОВ ТЕЛЕГРАМА ПРИ СТАРТЕ
        try:
            print("⏳ Настраиваем синюю кнопку меню...")
            await set_bot_commands(bot)
        except Exception as e:
            print(f"⚠️ Телеграм тормозит с меню, пропускаем: {e}")
            
        try:
            print("⏳ Очищаем старые зависшие сообщения...")
            await bot.delete_webhook(drop_pending_updates=True)
        except Exception as e:
            print(f"⚠️ Телеграм тормозит с очисткой, пропускаем: {e}")
            
        print("🚀 БОТ УСПЕШНО ЗАПУЩЕН И ГОТОВ К РАБОТЕ!")
        await dp.start_polling(bot)
        
    except Exception as e:
        print(f"🛑 Критическая ошибка: {e}")

if __name__ == "__main__":
    asyncio.run(main())
