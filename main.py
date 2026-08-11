import asyncio
import os
import base64
import json
import re
from datetime import datetime, timedelta

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
class BotStates(StatesGroup):
    waiting_for_edit_text = State()
    waiting_for_menu_ingredients = State()

class ProfileStates(StatesGroup):
    gender = State()
    stats = State() # Возраст, рост, вес в одну строку
    goal = State()
    activity = State()

# --- КЛАВИАТУРЫ (Точь-в-точь как на скринах) ---
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Сегодня"), KeyboardButton(text="🥗 Что приготовить")],
        [KeyboardButton(text="⚖️ Вес"), KeyboardButton(text="👤 Профиль")]
    ],
    resize_keyboard=True,
    input_field_placeholder="Пришли фото ед..."
)

gender_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="👨 Мужчина", callback_data="gender_M")], 
    [InlineKeyboardButton(text="👩 Женщина", callback_data="gender_F")]
])

goal_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📉 Похудеть", callback_data="goal_loss")],
    [InlineKeyboardButton(text="⚖️ Удержать вес", callback_data="goal_maintain")],
    [InlineKeyboardButton(text="📈 Набрать массу", callback_data="goal_gain")]
])

activity_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🛋 Сидячий", callback_data="act_low")],
    [InlineKeyboardButton(text="🚶‍♀️ Лёгкая (1–2 трен.)", callback_data="act_light")],
    [InlineKeyboardButton(text="🏃‍♂️ Умеренная (3–4)", callback_data="act_med")],
    [InlineKeyboardButton(text="🏋️‍♂️ Высокая (5–6)", callback_data="act_high")],
    [InlineKeyboardButton(text="🏅 Спортивная (2/день)", callback_data="act_pro")]
])

# --- УТИЛИТЫ ---
def make_progress_bar(current: float, total: float, length: int = 10) -> str:
    if total <= 0: return "□" * length
    percent = min(current / total, 1.0)
    filled_length = int(length * percent)
    return "■" * filled_length + "□" * (length - filled_length)

def calculate_norm(gender, age, height, weight, goal, activity):
    if gender == 'M': bmr = (10 * weight) + (6.25 * height) - (5 * age) + 5
    else: bmr = (10 * weight) + (6.25 * height) - (5 * age) - 161
    
    act_mults = {"act_low": 1.2, "act_light": 1.375, "act_med": 1.55, "act_high": 1.725, "act_pro": 1.9}
    tdee = bmr * act_mults.get(activity, 1.2)
    
    final_norm = tdee
    deficit_pct = 0
    if goal == "goal_loss": 
        final_norm = tdee * 0.78 # -22% как на скрине
        deficit_pct = -22
    elif goal == "goal_gain": 
        final_norm = tdee * 1.15
        deficit_pct = 15
        
    return {
        'bmr': int(bmr), 'tdee': int(tdee), 'norm': int(final_norm), 
        'deficit': deficit_pct, 'goal_code': goal
    }

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

# --- 1. ОНБОРДИНГ (Шаг в шаг со скринами) ---
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    if get_user_profile(message.from_user.id):
        await message.answer("С возвращением! Пришли фото еды — я всё посчитаю 📸", reply_markup=main_menu)
    else:
        # Точная копия текста со скрина
        text = (
            f"Привет, {message.from_user.first_name or 'друг'} 🕊! Это «Умная Тарелка» — "
            "я <b>Артём</b>, твой нутрициолог в телефоне 🥗\n\n"
            "Что я умею:\n"
            "📸 считать КБЖУ по фото еды — просто сфоткай тарелку;\n"
            "📊 вести дневник, чтобы ты не выходил за свою норму;\n"
            "🧊 собирать меню из того, что есть в холодильнике;\n"
            "❓ отвечать на вопросы про питание.\n\n"
            "<b>Шаг 1 из 4</b>\nУкажи свой пол:"
        )
        await message.answer(text, reply_markup=gender_kb)
        await state.set_state(ProfileStates.gender)

@dp.callback_query(ProfileStates.gender, F.data.startswith("gender_"))
async def ask_stats(callback: CallbackQuery, state: FSMContext):
    await state.update_data(gender=callback.data.split("_")[1])
    text = (
        "<i>Шаг 2 из 4</i>\n\n"
        "Напиши в одну строку через пробел:\n"
        "<b>возраст, рост в см и вес в кг.</b>\n\n"
        "Например: 32 182 92\n"
        "<i>Можно и словами — «мне 32, рост 182, вешу 92,4».</i>"
    )
    await callback.message.edit_text(text)
    await state.set_state(ProfileStates.stats)

@dp.message(ProfileStates.stats)
async def process_stats(message: Message, state: FSMContext):
    # Умный парсинг трех чисел из строки
    numbers = re.findall(r'\d+[.,]?\d*', message.text)
    if len(numbers) < 3:
        return await message.answer("Не смог найти 3 цифры. Напиши просто: 32 182 92")
    
    age, height, weight = int(float(numbers[0].replace(',','.'))), int(float(numbers[1].replace(',','.'))), float(numbers[2].replace(',','.'))
    await state.update_data(age=age, height=height, weight=weight)
    
    await message.answer("<i>Шаг 3 из 4</i>\n\nКакая <b>цель</b>?", reply_markup=goal_keyboard)
    await state.set_state(ProfileStates.goal)

@dp.callback_query(ProfileStates.goal, F.data.startswith("goal_"))
async def ask_act(callback: CallbackQuery, state: FSMContext):
    await state.update_data(goal=callback.data)
    await callback.message.edit_text("<i>Шаг 4 из 4</i>\n\nУровень активности:", reply_markup=activity_keyboard)
    await state.set_state(ProfileStates.activity)

@dp.callback_query(ProfileStates.activity, F.data.startswith("act_"))
async def finish_profile(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    calc = calculate_norm(data['gender'], data['age'], data['height'], data['weight'], data['goal'], callback.data)
    
    norm = calc['norm']
    # Расчет БЖУ как на скрине (30/40/30)
    p = int((norm * 0.27) / 4)
    f = int((norm * 0.40) / 9)
    c = int((norm * 0.33) / 4)
    
    target_date = (datetime.now() + timedelta(days=23*7)).strftime("%Y-%m-%d")
    goal_name = "Похудеть" if data['goal'] == 'goal_loss' else "Набрать массу" if data['goal'] == 'goal_gain' else "Удержать вес"

    if db:
        db.collection('users').document(str(callback.from_user.id)).set({
            'gender': data['gender'], 'age': data['age'], 'height': data['height'], 'weight': data['weight'],
            'goal': goal_name, 'norm': norm, 'p': p, 'f': f, 'c': c
        })

    # Экран итогов (1-в-1 со скрином)
    result_text = (
        "🎯 <b>ТВОЯ НОРМА НА ДЕНЬ</b>\n"
        "___\n"
        f"🔥 <b>{norm} ккал</b>\n\n"
        f"🥩 Белки <b>{p} г</b> 27%\n"
        f"🥑 Жиры <b>{f} г</b> 40%\n"
        f"🍚 Углеводы <b>{c} г</b> 33%\n"
        "🌾 Клетчатка 24 г 💧 Вода 2.6 л\n"
        "___\n"
        f"Обмен покоя: <b>{calc['bmr']}</b> · расход за день: <b>{calc['tdee']}</b>\n"
        f"Цель «{goal_name}» -> дефицит <b>{calc['deficit']}%</b>\n"
        f"Темп ≈ <b>0.44 кг/нед</b> -> цель к <b>{target_date}</b>\n"
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


# --- 2. РАСПОЗНАВАНИЕ ФОТО (2 шага как на скрине) ---
@dp.message(F.photo)
async def handle_photo(message: Message, state: FSMContext):
    wait_msg = await message.answer("👀 Изучаю тарелку...")
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
        
        await wait_msg.edit_text(f"{food_desc}\n___\nВсё верно? После подтверждения посчитаю КБЖУ.", reply_markup=kb)
    except Exception:
        await wait_msg.edit_text("Ошибка. Попробуй еще раз.")

@dp.callback_query(F.data == "food_correct")
async def calc_food(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    food_desc = data.get("recognized_food", "")
    await callback.message.edit_text(f"{food_desc}\n\n⏳ Считаю калории...")
    
    u_data = get_user_profile(callback.from_user.id)
    norm = u_data['norm'] if u_data else 2000
    
    prompt = (
        f"Пользователь подтвердил еду: {food_desc}\n"
        f"Его норма: {norm} ккал. Посчитай КБЖУ.\n"
        "Ответь СТРОГО по шаблону (замени скобки на цифры, добавь совет):\n"
        "✅ <b>[Название блюда]</b>\n___\n"
        "• [ингредиент] <b>[вес] г</b> — [ккал] ккал\n"
        "<i>Б[белки] Ж[жиры] У[углеводы]</i>\n___\n"
        "<b>Итого: [сумма_ккал] ккал</b> · вес порции ~[вес] г\n"
        "<b>Б [б] · Ж [ж] · У [у]</b>\n___\n"
        "💬 [Короткий комментарий нутрициолога. 💡 Совет]"
    )
    
    res = await ask_ai(text_prompt=prompt)
    await state.update_data(last_ai_response=res)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Дневник (Сохранить)", callback_data="save_to_diary")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="undo_diary")]
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
    prompt = (
        f"Прошлый ответ: {data.get('recognized_food')}\nИсправление: {message.text}\n"
        "Выдай новый состав СТРОГО по шаблону:\n"
        "🍽 <b>[Название]</b>\n___\n• [инг] — [вес] г\n___\n⚖️ Общий вес: ~[вес] г"
    )
    new_desc = await ask_ai(text_prompt=prompt)
    await state.update_data(recognized_food=new_desc)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Всё верно", callback_data="food_correct")],
        [InlineKeyboardButton(text="✏️ Поправить", callback_data="food_edit")]
    ])
    await wait_msg.edit_text(f"{new_desc}\n___\nВсё верно? После подтверждения посчитаю КБЖУ.", reply_markup=kb)
    await state.set_state(None)

@dp.callback_query(F.data == "food_wrong")
async def wrong_food(callback: CallbackQuery):
    await callback.message.edit_text("Понял, промахнулся 🙈 Напиши текстом или сфоткай заново.")

# --- 3. ДНЕВНИК (Дизайн 1-в-1) ---
@dp.callback_query(F.data == "save_to_diary")
async def save_diary(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    text = data.get("last_ai_response", "")
    user_id = str(callback.from_user.id)
    doc_id = get_today_doc_id(user_id)
    
    record = f"{datetime.now().strftime('%H:%M')} · {text.split('___')[0].replace('✅', '').strip()}"
    if db: db.collection('diaries').document(doc_id).set({'meals': firestore.ArrayUnion([record])}, merge=True)
    
    await callback.answer("Сохранено в дневник!", show_alert=False)
    await show_diary_logic(callback.message, user_id)

@dp.message(F.text == "📊 Сегодня")
async def show_diary_btn(message: Message):
    await show_diary_logic(message, str(message.from_user.id))

async def show_diary_logic(message: Message, user_id: str):
    u_data = get_user_profile(user_id)
    if not u_data: return await message.answer("Нажми /start")
    
    doc = db.collection('diaries').document(get_today_doc_id(user_id)).get()
    meals = doc.to_dict().get('meals', []) if doc.exists else []
    
    date_str = datetime.now().strftime("%d.%m")
    header = f"📊 <b>ДНЕВНИК ЗА СЕГОДНЯ</b>\n<i>{date_str}</i>\n___\n"
    
    if not meals:
        body = "Пока пусто. Пришли фото еды — я всё посчитаю 📸\n___\n"
        body += f"🔥 Калории <b>0</b> / {u_data['norm']} (0%)\n□□□□□□□□□□\n"
        body += f"🥩 Белки <b>0</b> / {u_data.get('p', 0)} г\n□□□□□□□□□□\n"
        body += f"🥑 Жиры <b>0</b> / {u_data.get('f', 0)} г\n□□□□□□□□□□\n"
        body += f"🍚 Углеводы <b>0</b> / {u_data.get('c', 0)} г\n□□□□□□□□□□\n___\n"
        body += f"Осталось на сегодня: <b>{u_data['norm']} ккал</b>"
        return await message.answer(header + body)
        
    msg = await message.answer("⏳ Собираю дневник...")
    meals_text = "\n".join(meals)
    prompt = (
        f"Список еды: {meals_text}\nНорма: {u_data['norm']} ккал, Б:{u_data.get('p')} Ж:{u_data.get('f')} У:{u_data.get('c')}\n"
        "Выдай отчет СТРОГО по шаблону:\n"
        "[Список еды 그대로 из текста пользователя, разделенный ___]\n___\n"
        "🔥 Калории <b>[сумма]</b> / [норма] ([процент]%)\n[здесь 10 символов ■/□]\n"
        "🥩 Белки <b>[сумма]</b> / [норма] г\n[здесь 10 символов ■/□]\n"
        "🥑 Жиры <b>[сумма]</b> / [норма] г\n[здесь 10 символов ■/□]\n"
        "🍚 Углеводы <b>[сумма]</b> / [норма] г\n[здесь 10 символов ■/□]\n___\n"
        "Осталось на сегодня: <b>[остаток] ккал</b>"
    )
    res = await ask_ai(text_prompt=prompt)
    await msg.edit_text(header + res)

# Заглушки для остальных кнопок меню
@dp.message(F.text == "⚖️ Вес")
async def set_weight(message: Message): await message.answer("Раздел в разработке 🚀")
@dp.message(F.text == "👤 Профиль")
async def show_prof(message: Message): await message.answer("Твой профиль 👤", reply_markup=goal_keyboard)
@dp.message(F.text == "🥗 Что приготовить")
async def what_to_cook(message: Message): await message.answer("Пришли фото холодильника 🧊")


# --- СЕРВЕР И ЗАПУСК ---
async def health_check(request): return web.Response(text="Bot is running!")

async def main():
    try:
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
        print(e)

if __name__ == "__main__":
    asyncio.run(main())
