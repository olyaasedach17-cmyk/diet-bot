import asyncio
import base64
import io
import json
import logging
import os
import re
from datetime import datetime, timedelta

from aiohttp import web
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from openai import AsyncOpenAI
import firebase_admin
from firebase_admin import credentials, firestore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# =========================================================
# НАСТРОЙКИ И ЛОГИ
# =========================================================
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
AI_API_KEY = os.getenv("AI_API_KEY") or os.getenv("POLZA_API_KEY")
AI_BASE_URL = os.getenv("AI_BASE_URL") or os.getenv("POLZA_BASE_URL")

AI_MODEL = os.getenv("AI_MODEL", "openai/gpt-5.6-luna")
AI_VISION_MODEL = os.getenv("AI_VISION_MODEL") or AI_MODEL

BEPAID_SHOP_ID = os.getenv("BEPAID_SHOP_ID", "")
BEPAID_SECRET_KEY = os.getenv("BEPAID_SECRET_KEY", "")

# Данные владельца бота
OWNER_NAME = "Оля"
OWNER_LINK = "https://instagram.com/your_profile"

if not BOT_TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN")
if not AI_API_KEY:
    raise RuntimeError("Не найден AI_API_KEY")
if not AI_BASE_URL:
    raise RuntimeError("Не найден AI_BASE_URL")

# =========================================================
# FIREBASE ИНИЦИАЛИЗАЦИЯ
# =========================================================
firebase_json_str = os.getenv("FIREBASE_JSON")
if firebase_json_str:
    try:
        creds_dict = json.loads(firebase_json_str)
        cred = credentials.Certificate(creds_dict)
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        db = firestore.client()
        logger.info("🔥 Firebase Firestore успешно подключен!")
    except Exception as e:
        logger.error(f"❌ Ошибка подключения Firebase: {e}")
        db = None
else:
    logger.warning("⚠️ FIREBASE_JSON не найден в переменных окружения.")
    db = None

# =========================================================
# AI-КЛИЕНТ
# =========================================================
ai_client = AsyncOpenAI(
    api_key=AI_API_KEY,
    base_url=AI_BASE_URL.rstrip("/"),
    timeout=90,
    max_retries=2,
)

# =========================================================
# TELEGRAM И КЛАВИАТУРЫ
# =========================================================
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

dp = Dispatcher()

main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Сегодня"), KeyboardButton(text="😋 Вкусняшка")],
        [KeyboardButton(text="🥗 Что приготовить"), KeyboardButton(text="🏋️ Тренировка")],
        [KeyboardButton(text="⚖️ Вес"), KeyboardButton(text="👤 Профиль")]
    ],
    resize_keyboard=True,
    input_field_placeholder="Пришли фото еды, наговори голосом...",
)
# =========================================================
# СОСТОЯНИЯ
# =========================================================
class Onboarding(StatesGroup):
    stats = State()

class FoodStates(StatesGroup):
    correcting = State()
    waiting_for_recipe = State()

class WeightStates(StatesGroup):
    waiting_for_weight = State()

class ActivityStates(StatesGroup):
    waiting_for_activity = State()
class TreatStates(StatesGroup):
    waiting_for_treat = State()

# =========================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =========================================================
def clean_html_tags(text: str) -> str:
    return re.sub(r'<(?!/?(b|i|code|s|u)\b)[^>]*>', '', text)

def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")

def make_progress_bar(current: int, target: int, length: int = 10) -> str:
    if target <= 0:
        return "□" * length
    fraction = min(max(current / target, 0.0), 1.0)
    filled_length = int(round(length * fraction))
    return "■" * filled_length + "□" * (length - filled_length)

async def get_user_profile(user_id: int) -> dict | None:
    if not db:
        return None
    doc = await asyncio.to_thread(db.collection('users').document(str(user_id)).get)
    if not doc.exists:
        return None
        
    data = doc.to_dict()
    calories = data.get('calories') or data.get('norm') or 2000
    protein = data.get('protein') or data.get('p') or 100
    fat = data.get('fat') or data.get('f') or 70
    carbs = data.get('carbs') or data.get('c') or 200
    
    data['calories'] = int(calories)
    data['protein'] = int(protein)
    data['fat'] = int(fat)
    data['carbs'] = int(carbs)
    return data

async def save_user_profile(user_id: int, data: dict):
    if db:
        await asyncio.to_thread(db.collection('users').document(str(user_id)).set, data, merge=True)

async def check_user_access(user_id: int) -> bool:
    user = await get_user_profile(user_id)
    if not user:
        return False
        
    now = datetime.now()
    
    premium_str = user.get("premium_until")
    if premium_str:
        try:
            if now < datetime.fromisoformat(premium_str):
                return True
        except Exception:
            pass

    trial_str = user.get("trial_until")
    if not trial_str:
        new_trial_end = now + timedelta(days=14)
        await save_user_profile(user_id, {"trial_until": new_trial_end.isoformat()})
        return True
        
    try:
        if now < datetime.fromisoformat(trial_str):
            return True
    except Exception:
        pass
            
    return False

async def send_paywall(message: Message):
    text = (
        "🔒 <b>Твой бесплатный период (14 дней) завершился.</b>\n\n"
        "Чтобы продолжить считать КБЖУ по фото, генерировать тренировки и вести дневник, выбери подписку:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 месяц — 15 BYN", callback_data="buy_1_month")],
        [InlineKeyboardButton(text="3 месяца — 29 BYN 🔥 (Скидка 35%)", callback_data="buy_3_months")],
        [InlineKeyboardButton(text="6 месяцев — 49 BYN 💎 (Скидка 45%)", callback_data="buy_6_months")],
    ])
    await message.answer(text, reply_markup=kb)

async def set_bot_description(bot: Bot):
    description = (
        "Что умеет этот бот?\n"
        "🍽 Сфотографируй еду — NutriAI определит состав, оценит граммовки, "
        "рассчитает КБЖУ и добавит приём пищи в дневник.\n\n"
        "🎤 Поймёт голосовое сообщение\n"
        "🏋️ Составит программу тренировок под твой уровень\n"
        "🔥 Посчитает сожжённые калории за любую активность\n"
        "💧 Поможет вести трекер выпитой воды\n"
        "⚖️ Проследит за динамикой веса и рассчитает дни до цели\n"
        "🧊 Соберёт меню из продуктов в холодильнике\n"
        "🎯 Рассчитает личную норму КБЖУ\n\n"
        "Без ручного поиска продуктов и сложных таблиц.\n\n"
        "Нажимай «Старт» и попробуй 👇"
    )
    try:
        await bot.set_my_description(description)
    except Exception as e:
        logger.warning(f"Не удалось установить описание бота: {e}")

async def get_today_meals(user_id: int) -> list:
    if not db:
        return []
    doc_id = f"{user_id}_{today_str()}"
    doc = await asyncio.to_thread(db.collection('diaries').document(doc_id).get)
    return doc.to_dict().get('meals', []) if doc.exists else []

async def add_meal_to_today(user_id: int, meal_data: dict):
    if not db:
        return
    doc_id = f"{user_id}_{today_str()}"
    doc_ref = db.collection('diaries').document(doc_id)
    doc = await asyncio.to_thread(doc_ref.get)
    current_meals = doc.to_dict().get('meals', []) if doc.exists else []
    current_meals.append(meal_data)
    await asyncio.to_thread(doc_ref.set, {'meals': current_meals}, merge=True)

def calculate_norm(gender: str, age: int, height: float, weight: float, goal: str, activity: str) -> dict:
    if gender == "M":
        bmr = 10 * weight + 6.25 * height - 5 * age + 5
    else:
        bmr = 10 * weight + 6.25 * height - 5 * age - 161
        
    activity_coefficients = {"low": 1.2, "light": 1.375, "medium": 1.55, "high": 1.725}
    tdee = bmr * activity_coefficients.get(activity, 1.2)

    if goal == "loss":
        calories = tdee * 0.78
    elif goal == "gain":
        calories = tdee * 1.15
    else:
        calories = tdee

    calories = int(calories)
    return {
        "calories": calories,
        "protein": int(calories * 0.27 / 4),
        "fat": int(calories * 0.40 / 9),
        "carbs": int(calories * 0.33 / 4),
    }

def extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("AI не вернул валидный JSON")
    return json.loads(match.group(0))

async def ask_ai(prompt: str, image_base64: str | None = None, model: str | None = None) -> str:
    used_model = model or AI_MODEL
    if image_base64:
        user_content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}", "detail": "low"}},
        ]
    else:
        user_content = prompt
        
    try:
        response = await ai_client.chat.completions.create(
            model=used_model,
            messages=[
                {"role": "system", "content": "Ты нутрициолог и персональный фитнес-тренер. Отвечай кратко, используй красивый HTML (<b>, <i>) без Markdown таблиц (|) и без заголовков markdown (###)."},
                {"role": "user", "content": user_content},
            ],
            temperature=0.2,
        )
        return response.choices[0].message.content or "" if response.choices else ""
    except Exception as e:
        logger.error(f"🔥 Ошибка AI ({used_model}): {e}")
        raise

async def create_bepaid_bill(user_id: int, amount_byn: float, months: int) -> str | None:
    if not BEPAID_SHOP_ID or not BEPAID_SECRET_KEY:
        return None
    url = "https://checkout.bepaid.by/v2/redirect_biller/bills"
    payload = {
        "request": {
            "amount": int(amount_byn * 100),
            "currency": "BYN",
            "description": f"Подписка Nutrition AI на {months} мес.",
            "notification_url": "https://diet-bot-zqpn.onrender.com/webhook/bepaid",
            "tracking_id": f"sub_{user_id}_{months}_{int(datetime.now().timestamp())}",
        }
    }
    auth = aiohttp.BasicAuth(login=BEPAID_SHOP_ID, password=BEPAID_SECRET_KEY)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, auth=auth) as resp:
                if resp.status in (200, 201):
                    data = await resp.json()
                    return data.get("checkout", {}).get("redirect_url")
    except Exception as e:
        logger.error(f"Ошибка бепид: {e}")
    return None

def food_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Верно", callback_data="food_correct")],
            [
                InlineKeyboardButton(text="✏️ Поправить", callback_data="food_edit"),
                InlineKeyboardButton(text="❌ Удалить", callback_data="food_delete"),
            ],
        ]
    )

def result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Сохранить в дневник", callback_data="meal_save")],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data="food_delete")],
        ]
    )
    def activity_result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Добавить активность", callback_data="activity_save")],
            [InlineKeyboardButton(text="🗑 Отмена", callback_data="food_delete")],
        ]
    )

async def send_today(message: Message, user_id: int | None = None):
    target_id = user_id or message.from_user.id
    user = await get_user_profile(target_id)
    if not user:
        await message.answer("Сначала нажмите /start.")
        return
        
    meals = await get_today_meals(target_id)
    
    doc_id = f"{target_id}_{today_str()}"
    doc_data = {}
    if db:
        doc = await asyncio.to_thread(db.collection('diaries').document(doc_id).get)
        if doc.exists:
            doc_data = doc.to_dict()
            
    water_ml = doc_data.get('water', 0)
    burned_kcal = doc_data.get('burned_kcal', 0)

    total_kcal = sum(m.get('calories', 0) for m in meals)
    total_p = sum(m.get('protein', 0) for m in meals)
    total_f = sum(m.get('fat', 0) for m in meals)
    total_c = sum(m.get('carbs', 0) for m in meals)
    
    norm_kcal = user.get('calories', 2000)
    norm_p = user.get('protein', 100)
    norm_f = user.get('fat', 70)
    norm_c = user.get('carbs', 200)
    
    pct_kcal = int((total_kcal / norm_kcal) * 100) if norm_kcal > 0 else 0
    
    meals_text = ""
    for meal in meals:
        title_clean = clean_html_tags(str(meal.get('title', 'Блюдо')))
        meals_text += (
            f"🍽 <b>{title_clean}</b>\n"
            f"🔥 {meal.get('calories', 0)} ккал · Б {meal.get('protein', 0)} г · Ж {meal.get('fat', 0)} г · У {meal.get('carbs', 0)} г\n\n"
        )
        
    net_kcal = total_kcal - burned_kcal
    if net_kcal > norm_kcal:
        overage_kcal = net_kcal - norm_kcal
        status_text = (
            f"⚠️ Перебор: <b>+{overage_kcal} ккал</b>\n\n"
            f"💬 <i>Сегодня немного перебрали норму, но это нормально! Однодневный профицит не испортит прогресс. Не урезай рацион завтра 💪</i>"
        )
    else:
        rem_kcal = norm_kcal - net_kcal
        status_text = f"Осталось на сегодня: <b>{rem_kcal} ккал</b>"

    burned_str = f" <i>(-{burned_kcal} ккал активностью)</i>" if burned_kcal > 0 else ""

    text = (
        f"{meals_text}"
        f"🔥 <b>Калории</b>  <b>{total_kcal}</b> / {norm_kcal} ккал ({pct_kcal}%){burned_str}\n"
        f"<code>{make_progress_bar(total_kcal, norm_kcal)}</code>\n\n"
        f"🥩 <b>Белки</b>  <b>{total_p}</b> / {norm_p} г\n"
        f"<code>{make_progress_bar(total_p, norm_p)}</code>\n\n"
        f"🥑 <b>Жиры</b>  <b>{total_f}</b> / {norm_f} г\n"
        f"<code>{make_progress_bar(total_f, norm_f)}</code>\n\n"
        f"🍚 <b>Углеводы</b>  <b>{total_c}</b> / {norm_c} г\n"
        f"<code>{make_progress_bar(total_c, norm_c)}</code>\n\n"
        f"💧 <b>Выпитая вода:</b> {water_ml} мл\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{status_text}"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💧 +250 мл воды", callback_data="add_water_250")]
    ])
    await message.answer(text, reply_markup=kb)

# =========================================================
# ОНБОРДИНГ
# =========================================================
@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    await state.clear()
    user = await get_user_profile(message.from_user.id)
    if user:
        await message.answer("С возвращением! Пришли фото еды 📸", reply_markup=main_menu)
        return

    user_name = message.from_user.first_name or "друг"
    
    welcome_text = (
        f"Привет, {user_name} 🕊! Это «Nutri Ai» — я <a href='{OWNER_LINK}'><b>{OWNER_NAME}</b></a>, твой нутрициолог в телефоне 🥗\n\n"
        "Что я умею:\n"
        "📸 считать КБЖУ по фото еды — просто сфоткай тарелку;\n"
        "📊 вести дневник, чтобы ты не выходил за свою норму;\n"
        "🎤 понимать голосовые сообщения;\n"
        "🏋️ подбирать программы тренировок (дома или в зале);\n"
        "🔥 учитывать сожжённые калории за любую активность;\n"
        "💧 вести учёт выпитой воды;\n"
        "⚖️ отслеживать динамику веса и дни до цели;\n"
        "🧊 собирать меню из того, что лежит в холодильнике.\n\n"
        "🎁 <b>Пробный период:</b> 14 дней полностью бесплатно! После этого доступ продолжится по подписке.\n\n"
        "Сначала короткий опрос — <b>4 вопроса</b>, меньше минуты. Он нужен, чтобы посчитать <b>твою</b> норму, а не среднюю по больнице."
    )
    
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Начать (14 дней бесплатно)", callback_data="start_onb")]
        ]
    )
    
    await message.answer(welcome_text, reply_markup=main_menu)
    await message.answer("Жми кнопку ниже 👇", reply_markup=kb)

@dp.callback_query(F.data == "start_onb")
async def start_onboarding_callback(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👨 Мужчина", callback_data="gender_M")],
            [InlineKeyboardButton(text="👩 Женщина", callback_data="gender_F")],
        ]
    )
    await callback.message.edit_text(
        "<i>Шаг 1 из 4</i>\n\nТвой <b>пол</b>?\n<i>От него зависит формула расчёта.</i>", 
        reply_markup=keyboard
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("gender_"))
async def gender_handler(callback: CallbackQuery, state: FSMContext):
    gender = callback.data.split("_")[1]
    await state.update_data(gender=gender)
    await state.set_state(Onboarding.stats)
    await callback.message.edit_text("Напиши через пробел:\n<b>возраст рост вес</b>\n\nНапример: <code>32 182 92</code>")
    await callback.answer()

@dp.message(Onboarding.stats)
async def stats_handler(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Напиши возраст, рост и вес числами.")
        return
        
    numbers = re.findall(r"\d+(?:[.,]\d+)?", message.text)
    if len(numbers) < 3:
        await message.answer("Нужно написать три значения: возраст, рост и вес.")
        return
        
    age = int(float(numbers[0].replace(",", ".")))
    height = float(numbers[1].replace(",", "."))
    weight = float(numbers[2].replace(",", "."))
    
    await state.update_data(age=age, height=height, weight=weight)
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📉 Похудеть", callback_data="goal_loss")],
            [InlineKeyboardButton(text="⚖️ Удержать вес", callback_data="goal_maintain")],
            [InlineKeyboardButton(text="📈 Набрать массу", callback_data="goal_gain")],
        ]
    )
    await message.answer("Какая у тебя цель?", reply_markup=keyboard)

@dp.callback_query(F.data.startswith("goal_"))
async def goal_handler(callback: CallbackQuery, state: FSMContext):
    goal = callback.data.split("_")[1]
    await state.update_data(goal=goal)
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛋 Сидячий образ жизни", callback_data="activity_low")],
            [InlineKeyboardButton(text="🚶 Лёгкая активность", callback_data="activity_light")],
            [InlineKeyboardButton(text="🏃 Умеренная активность", callback_data="activity_medium")],
            [InlineKeyboardButton(text="🏋️ Высокая активность", callback_data="activity_high")],
        ]
    )
    await callback.message.edit_text("Выбери уровень физической активности:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("activity_"))
async def activity_handler(callback: CallbackQuery, state: FSMContext):
    activity = callback.data.split("_")[1]
    data = await state.get_data()
    
    norm = calculate_norm(
        gender=data["gender"],
        age=data["age"],
        height=data["height"],
        weight=data["weight"],
        goal=data["goal"],
        activity=activity,
    )
    
    trial_end = datetime.now() + timedelta(days=14)
    
    user_data = {
        "user_id": callback.from_user.id,
        "gender": data["gender"],
        "age": data["age"],
        "height": data["height"],
        "weight": data["weight"],
        "goal": data["goal"],
        "activity": activity,
        "calories": norm["calories"],
        "protein": norm["protein"],
        "fat": norm["fat"],
        "carbs": norm["carbs"],
        "target_weight": 58.0 if data["gender"] == "F" else 75.0,
        "trial_until": trial_end.isoformat(),
        "premium_until": None,
        "created_at": datetime.now().isoformat()
    }
    await save_user_profile(callback.from_user.id, user_data)
    await state.clear()
    
    await callback.message.edit_text(
        "🎯 <b>Твоя дневная норма</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🔥 {norm['calories']} ккал\n"
        f"🥩 Белки: {norm['protein']} г\n"
        f"🥑 Жиры: {norm['fat']} г\n"
        f"🍚 Углеводы: {norm['carbs']} г\n\n"
        f"🎁 <b>Тебе активировано 14 дней бесплатного доступа!</b>\n"
        f"<i>По истечении 14 дней для продолжения использования понадобится подписка (от 15 BYN/мес).</i>\n\n"
        "Теперь пришли фотографию еды 📸"
    )
    await callback.message.answer("Готово! Пришли фото еды.", reply_markup=main_menu)
    await callback.answer()

# =========================================================
# ТРЕКЕР ВОДЫ
# =========================================================
@dp.callback_query(F.data == "add_water_250")
async def add_water_handler(callback: CallbackQuery):
    user_id = callback.from_user.id
    doc_id = f"{user_id}_{today_str()}"
    
    if db:
        doc_ref = db.collection('diaries').document(doc_id)
        doc = await asyncio.to_thread(doc_ref.get)
        current_water = doc.to_dict().get('water', 0) if doc.exists else 0
        new_water = current_water + 250
        await asyncio.to_thread(doc_ref.set, {'water': new_water}, merge=True)
        await callback.answer(f"💧 Добавлено 250 мл! Всего сегодня: {new_water} мл")
        await send_today(callback.message, user_id=user_id)
    else:
        await callback.answer("Ошибка БД")

# =========================================================
# ВЕС И ОТСЧЕТ ДО ЦЕЛИ
# =========================================================
@dp.message(F.text == "⚖️ Вес")
@dp.message(Command("weight"))
async def weight_handler(message: Message, state: FSMContext):
    user = await get_user_profile(message.from_user.id)
    if not user:
        await message.answer("Сначала пройди регистрацию: /start")
        return
    
    curr_weight = user.get("weight", "—")
    await state.set_state(WeightStates.waiting_for_weight)
    await message.answer(
        f"Твой текущий вес: <b>{curr_weight} кг</b>\n\n"
        "Напиши свой новый вес в кг (например: <code>74.5</code>):"
    )

@dp.message(WeightStates.waiting_for_weight)
async def process_weight_update(message: Message, state: FSMContext):
    numbers = re.findall(r"\d+(?:[.,]\d+)?", message.text)
    if not numbers:
        await message.answer("Пожалуйста, напиши вес числом.")
        return
        
    new_weight = float(numbers[0].replace(",", "."))
    user = await get_user_profile(message.from_user.id)
    old_weight = float(user.get("weight", new_weight))
    goal = user.get("goal", "maintain")
    target_weight = float(user.get("target_weight", 58.0))
    
    diff = round(new_weight - old_weight, 1)
    
    new_norm = calculate_norm(
        gender=user.get("gender", "M"),
        age=user.get("age", 25),
        height=user.get("height", 170),
        weight=new_weight,
        goal=goal,
        activity=user.get("activity", "low")
    )
    
    if goal == "loss":
        if diff < 0:
            praise = f"🎉 <b>Супер результат!</b> Минус <b>{abs(diff)} кг</b>! Ты молодец, продолжаем! 🔥"
        elif diff > 0:
            praise = f"⚖️ Вес +<b>{diff} кг</b>. Это может быть вода или отёк, не переживай 💪"
        else:
            praise = "⚖️ Вес остался прежним. Стабильность — это тоже отлично!"
    else:
        praise = f"⚖️ Новые данные зафиксированы!"

    kg_left = round(abs(new_weight - target_weight), 1)
    days_left = int((kg_left / 0.5) * 7)

    user_update = {
        "weight": new_weight,
        "calories": new_norm["calories"],
        "protein": new_norm["protein"],
        "fat": new_norm["fat"],
        "carbs": new_norm["carbs"]
    }
    await save_user_profile(message.from_user.id, user_update)
    await state.clear()
    
    response_text = (
        f"{praise}\n\n"
        f"📊 <b>Прогресс цели:</b>\n"
        f"• Новый вес: <b>{new_weight} кг</b> (цель: {target_weight} кг)\n"
        f"• Осталось сбросить: <b>{kg_left} кг</b> (~{days_left} дней)\n"
        f"• Новая норма: <b>{new_norm['calories']} ккал</b>"
    )
    await message.answer(response_text)

# =========================================================
# УМНЫЙ ГЕНЕРАТОР ТРЕНИРОВОК И ВВОД АКТИВНОСТИ
# =========================================================
@dp.message(F.text == "🏋️ Тренировка")
@dp.message(Command("workout"))
async def workout_menu_handler(message: Message):
    if not await check_user_access(message.from_user.id):
        await send_paywall(message)
        return

    text = (
        "🏋️ <b>ТРЕНИРОВКИ И АКТИВНОСТЬ</b>\n\n"
        "Выбери вариант тренировки на сегодня или впиши свои шаги/активность:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Дома · Лёгкая (Новичок 🟢)", callback_data="gen_workout_home_easy")],
        [InlineKeyboardButton(text="🏠 Дома · Обычная (Средний 🟡)", callback_data="gen_workout_home_medium")],
        [InlineKeyboardButton(text="🏋️ В зале · Базовая 🟢", callback_data="gen_workout_gym_easy")],
        [InlineKeyboardButton(text="🏋️ В зале · Продвинутая 🔴", callback_data="gen_workout_gym_hard")],
        [InlineKeyboardButton(text="👣 Своя активность или Шаги", callback_data="enter_custom_activity")]
    ])
    await message.answer(text, reply_markup=kb)

@dp.callback_query(F.data == "enter_custom_activity")
async def enter_activity_callback(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ActivityStates.waiting_for_activity)
    await callback.message.edit_text(
        "👣 <b>Введи свою активность или шаги:</b>\n\n"
        "Напиши текстом или наговори голосом, сколько шагов ты прошел(ла) или какую тренировку сделал(а).\n\n"
        "Например: <i>«Прошла 12 000 шагов»</i>, <i>«Силовая тренировка в зале 1 час»</i> или <i>«Плавание 45 минут»</i>."
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("gen_workout_"))
async def generate_workout_callback(callback: CallbackQuery):
    params = callback.data.replace("gen_workout_", "").split("_") # location, level
    location = "дома без оборудования" if params[0] == "home" else "в тренажёрном зале с гантелями/тренажерами"
    
    levels = {
        "easy": "для новичка (короткая, без суставной нагрузки, суставная разминка)",
        "medium": "средний уровень подготовки",
        "hard": "для продвинутого (интенсивная силовая)"
    }
    level_str = levels.get(params[1], "средний уровень")

    user = await get_user_profile(callback.from_user.id)
    weight = user.get("weight", 70) if user else 70
    goal = user.get("goal", "loss") if user else "loss"

    await callback.message.edit_text("⏳ Подбираю программу тренировки...")

    prompt = (
        f"Составь программу тренировки {location}. Уровень: {level_str}. Цель человека: '{goal}', вес: {weight} кг.\n\n"
        "ТРЕБОВАНИЯ К ФОРМАТИРОВАНИЮ (КРИТИЧЕСКИ ВАЖНО):\n"
        "1. НЕ ИСПОЛЬЗУЙ таблицы Markdown (символы |).\n"
        "2. НЕ ИСПОЛЬЗУЙ заголовки Markdown (###).\n"
        "3. Используй только HTML теги Telegram: <b>текст</b>, <i>текст</i>.\n"
        "4. Выдавай список упражнений через эмодзи и списки (например: 1. <b>Приседания</b> — 3 подхода по 12 раз).\n\n"
        "Структура ответа:\n"
        "<b>[Заголовок тренировки]</b>\n"
        "⏱ <b>Время:</b> ~20 мин | 🎯 <b>Фокус:</b> [группы мышц]\n\n"
        "<b>Упражнения:</b>\n"
        "1. ...\n"
        "2. ...\n"
        "3. ...\n"
        "4. ...\n\n"
        "🔥 <b>Примерный расход:</b> ~[число] ккал\n"
        "💡 <i>Совет по технике или отдыху.</i>\n\n"
        "Верни В КОНЦЕ строго строку вида: ESTIMATED_KCAL:[число] (например ESTIMATED_KCAL:160)"
    )

    try:
        raw_response = await ask_ai(prompt=prompt, model=AI_MODEL)
        
        # Извлекаем расчитанные калории
        kcal_match = re.search(r"ESTIMATED_KCAL:(\d+)", raw_response)
        est_kcal = int(kcal_match.group(1)) if kcal_match else 150
        
        clean_text = re.sub(r"ESTIMATED_KCAL:\d+", "", raw_response).strip()
        clean_text = clean_html_tags(clean_text)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"✅ Выполнил(а) (+{est_kcal} ккал)", callback_data=f"done_workout_{est_kcal}")]
        ])
        await callback.message.edit_text(clean_text, reply_markup=kb)
    except Exception as e:
        logger.error(f"Ошибка тренировки: {e}")
        await callback.message.edit_text("Не удалось составить тренировку. Попробуй ещё раз.")
    await callback.answer()

@dp.callback_query(F.data == "enter_custom_activity")
async def enter_activity_callback(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ActivityStates.waiting_for_activity)
    await callback.message.edit_text(
        "✍️ <b>Введи свою активность:</b>\n\n"
        "Напиши текстом или наговори голосом, чем ты занимался(ась) и сколько времени.\n\n"
        "Например: <i>«Пробежка 30 минут»</i> или <i>«Плавание в бассейне 45 минут»</i>"
    )
    await callback.answer()

@dp.message(ActivityStates.waiting_for_activity)
async def process_custom_activity(message: Message, state: FSMContext):
    # Снимаем состояние, но данные сохраним
    await state.set_state(None)
    wait_msg = await message.answer("⏳ Рассчитываю расход калорий...")
    
    user = await get_user_profile(message.from_user.id)
    weight = user.get("weight", 70) if user else 70

    prompt = (
        f"Пользователь весом {weight} кг выполнил активность: \"{message.text}\".\n"
        "Если речь о шагах, учти, что в среднем 1000 шагов = ~30-40 ккал (зависит от веса).\n"
        "Рассчитай примерный расход сожжённых калорий.\n"
        "Верни строго JSON:\n"
        "{\n"
        '  "title": "название активности (например: 10000 шагов или Бег 30 мин)",\n'
        '  "burned_kcal": 0,\n'
        '  "comment": "короткая похвала"\n'
        "}"
    )

    try:
        res = await ask_ai(prompt=prompt, model=AI_MODEL)
        act_data = extract_json(res)
        
        # Сохраняем рассчитанную активность в память бота, чтобы кнопка сработала
        await state.update_data(calculated_activity=act_data)
        
        burned = int(act_data.get("burned_kcal", 150))
        title = clean_html_tags(str(act_data.get("title", "Активность")))
        comment = clean_html_tags(str(act_data.get("comment", "Отличная работа!")))

        text = (
            f"🏃 <b>Активность:</b> {title}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🔥 Примерный расход: <b>{burned} ккал</b>\n\n"
            f"💬 <i>{comment}</i>\n\n"
            "Добавить эту активность в дневник?"
        )
        
        # Выводим карточку и кнопки (Добавить / Отмена)
        await wait_msg.edit_text(text, reply_markup=activity_result_keyboard())
    except Exception as e:
        logger.error(f"Ошибка расчёта активности: {e}")
        await wait_msg.edit_text("Не удалось рассчитать активность. Попробуй описать точнее.")

# Обработчик нажатия на кнопку "Добавить активность"
@dp.callback_query(F.data == "activity_save")
async def save_activity_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    act_data = data.get("calculated_activity")
    
    if not act_data:
        await callback.message.edit_text("❌ Данные устарели. Попробуй внести активность заново.")
        return

    burned = int(act_data.get("burned_kcal", 0))
    user_id = callback.from_user.id
    doc_id = f"{user_id}_{today_str()}"
    
    if db and burned > 0:
        doc_ref = db.collection('diaries').document(doc_id)
        doc = await asyncio.to_thread(doc_ref.get)
        current_burned = doc.to_dict().get('burned_kcal', 0) if doc.exists else 0
        new_burned = current_burned + burned
        await asyncio.to_thread(doc_ref.set, {'burned_kcal': new_burned}, merge=True)

    await state.clear()
    await callback.message.edit_text(f"✅ <b>Сожжено {burned} ккал! Зачтено в дневник.</b>")
    await send_today(callback.message, user_id=user_id)
    await callback.answer()

# =========================================================
# ГОЛОСОВЫЕ СООБЩЕНИЯ (WHISPER)
# =========================================================
@dp.message(F.voice)
async def voice_handler(message: Message, state: FSMContext):
    if not await check_user_access(message.from_user.id):
        await send_paywall(message)
        return

    wait_msg = await message.answer("Слушаю голосовое... 🎧")

    try:
        voice_file = await bot.get_file(message.voice.file_id)
        buffer = io.BytesIO()
        await bot.download_file(voice_file.file_path, destination=buffer)
        buffer.name = "voice.ogg"

        transcript = await ai_client.audio.transcriptions.create(
            model="whisper-1", 
            file=buffer
        )
        text = transcript.text
        await wait_msg.edit_text(f"🗣 <b>Вы сказали:</b> «{clean_html_tags(text)}»\n\n⏳ Распознаю...")

        # Проверяем: речь про тренировку/активность или про еду
        prompt_check = f"Текст: \"{text}\". Если это про спорт/тренировку/активность — ответь 'ACTIVITY'. Если про еду/приём пищи — ответь 'FOOD'."
        check_res = await ask_ai(prompt=prompt_check, model=AI_MODEL)

        if "ACTIVITY" in check_res:
            message.text = text
            await process_custom_activity(message, state)
        else:
            prompt = f"Пользователь наговорил голосом еду: \"{text}\". Рассчитай калории и БЖУ."
            ai_response = await ask_ai(prompt=prompt, model=AI_MODEL)
            await message.answer(clean_html_tags(ai_response))
    except Exception as e:
        logger.error(f"Ошибка голосового: {e}")
        await wait_msg.edit_text("Не удалось распознать голосовое сообщение.")

# =========================================================
# ФОТО ЕДЫ
# =========================================================
@dp.message(F.photo)
async def photo_handler(message: Message, state: FSMContext):
    if not await check_user_access(message.from_user.id):
        await send_paywall(message)
        return

    wait_message = await message.answer("👀 Анализирую фотографию...")
    
    try:
        telegram_file = await bot.get_file(message.photo[-1].file_id)
        buffer = io.BytesIO()
        await bot.download_file(telegram_file.file_path, destination=buffer)
        image_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
        
        result = await ask_ai(
            image_base64=image_base64,
            prompt="Определи еду на фото, укажи название, ингредиенты и вес. Калории пока не считай.",
            model=AI_VISION_MODEL
        )
        
        await state.update_data(recognized_food=result, image_base64=image_base64)
        clean_result = clean_html_tags(result)
        await wait_message.edit_text(f"{clean_result}\n\nВсё верно?", reply_markup=food_keyboard())
    except Exception:
        logger.exception("Ошибка фото")
        await wait_message.edit_text("Не удалось обработать фотографию.")

@dp.callback_query(F.data == "food_correct")
async def food_correct_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    food_description = data.get("recognized_food", "")
    await callback.message.edit_text("⏳ Рассчитываю калории и БЖУ...")
    
    try:
        result = await ask_ai(
            prompt=(
                f"Рассчитай калории и БЖУ блюда:\n{food_description}\n\n"
                "Верни строго JSON:\n{\n\"title\": \"\", \"calories\": 0, \"protein\": 0, \"fat\": 0, \"carbs\": 0, \"weight\": 0, \"comment\": \"\"\n}"
            ),
            model=AI_MODEL
        )
        food_data = extract_json(result)
        await state.update_data(calculated_food=food_data)
        
        title = clean_html_tags(str(food_data.get("title", "Блюдо")))
        comment = clean_html_tags(str(food_data.get("comment", "")))
        
        text = (
            f"🍽 <b>{title}</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            f"🔥 Калории: <b>{food_data.get('calories', 0)} ккал</b>\n"
            f"🥩 Белки: {food_data.get('protein', 0)} г\n"
            f"🥑 Жиры: {food_data.get('fat', 0)} г\n"
            f"🍚 Углеводы: {food_data.get('carbs', 0)} г\n"
            f"⚖️ Вес: {food_data.get('weight', 0)} г\n\n💬 {comment}"
        )
        await callback.message.edit_text(text, reply_markup=result_keyboard())
    except Exception:
        await callback.message.edit_text("Не удалось рассчитать блюдо.")
    await callback.answer()

@dp.callback_query(F.data == "food_edit")
async def food_edit_handler(callback: CallbackQuery, state: FSMContext):
    await state.set_state(FoodStates.correcting)
    await callback.message.edit_text("Напиши, что нужно исправить (например: курицы 250 г, а не 150 г):")
    await callback.answer()

@dp.message(FoodStates.correcting)
async def correcting_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    result = await ask_ai(
        prompt=f"Старое: {data.get('recognized_food')}\nИсправление: {message.text}\nВыдай новое описание.",
        model=AI_MODEL
    )
    await state.update_data(recognized_food=result)
    await state.set_state(None)
    await message.answer(f"{clean_html_tags(result)}\n\nТеперь всё верно?", reply_markup=food_keyboard())

@dp.callback_query(F.data == "meal_save")
async def save_meal_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    food = data.get("calculated_food", {})
    meal_record = {
        "title": str(food.get("title", "Приём пищи")),
        "calories": int(food.get("calories", 0) or 0),
        "protein": int(food.get("protein", 0) or 0),
        "fat": int(food.get("fat", 0) or 0),
        "carbs": int(food.get("carbs", 0) or 0),
        "created_at": datetime.now().isoformat()
    }
    await add_meal_to_today(callback.from_user.id, meal_record)
    await state.clear()
    await callback.message.edit_text("✅ Приём пищи сохранён в дневник.")
    await send_today(callback.message, user_id=callback.from_user.id)
    await callback.answer()

@dp.callback_query(F.data == "food_delete")
async def delete_food_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🗑 Запись удалена.")
    await callback.answer()

# =========================================================
# ОПЛАТА И ТАРИФЫ
# =========================================================
@dp.callback_query(F.data.startswith("buy_"))
async def process_buy_callback(callback: CallbackQuery):
    plan = callback.data.split("_")[1]
    user_id = callback.from_user.id
    
    pricing = {"1": (15.0, 1), "3": (29.0, 3), "6": (49.0, 6)}
    amount, months = pricing.get(plan, (15.0, 1))
    
    pay_url = await create_bepaid_bill(user_id=user_id, amount_byn=amount, months=months)
    
    if pay_url:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 Оплатить {amount} BYN (ЕРИП/Карта)", url=pay_url)]
        ])
        await callback.message.edit_text(
            f"<b>Оформление подписки на {months} мес.</b>\n\nСумма к оплате: <b>{amount} BYN</b>\n"
            "После оплаты доступ откроется автоматически!",
            reply_markup=kb
        )
    else:
        await callback.message.edit_text("Ошибка формирования счета. Напишите в поддержку.")
# =========================================================
# БЫСТРЫЙ УЧЁТ ЛАКОМСТВ И ДЕСЕРТОВ (ВКУСНЯШКА)
# =========================================================
@dp.message(F.text == "😋 Вкусняшка")
@dp.message(Command("treat"))
async def treat_button_handler(message: Message, state: FSMContext):
    if not await check_user_access(message.from_user.id):
        await send_paywall(message)
        return

    await state.set_state(TreatStates.waiting_for_treat)
    await message.answer(
        "😋 <b>Съел(а) что-то вкусное?</b>\n\n"
        "Напиши текстом или наговори голосом, что это было (например: <i>«кусочек торта Наполеон»</i>, <i>«2 дольки тёмного шоколада»</i>, <i>«маленький баунти»</i>).\n\n"
        "<i>Я аккуратно внизу внесу это в дневник без чувства вины — баловать себя это абсолютно нормально! ✨</i>"
    )

@dp.message(TreatStates.waiting_for_treat)
async def process_treat_input(message: Message, state: FSMContext):
    # Снимаем состояние ожидания, но сохраняем данные (чтобы кнопка Сохранить сработала)
    await state.set_state(None) 
    
    wait_msg = await message.answer("⏳ Считаю КБЖУ вкусняшки...")

    user = await get_user_profile(message.from_user.id)
    goal = user.get("goal", "loss") if user else "loss"

    prompt = (
        f"Пользователь съел лакомство/десерт: \"{message.text}\". Цель человека: '{goal}'.\n"
        "Рассчитай примерный КБЖУ этого лакомства.\n"
        "Верни строго JSON без комментария и markdown блоков:\n"
        "{\n"
        '  "title": "название десерта",\n'
        '  "calories": 0,\n'
        '  "protein": 0,\n'
        '  "fat": 0,\n'
        '  "carbs": 0,\n'
        '  "comment": "Короткая, очень теплая фраза поддержки (1 предложение) про то, что зацикливаться на ругани себя нельзя!"\n'
        "}"
    )

    try:
        res = await ask_ai(prompt=prompt, model=AI_MODEL)
        food_data = extract_json(res)

        # Добавляем эмодзи, чтобы в дневнике было красиво
        title = clean_html_tags(str(food_data.get("title", "Вкусняшка")))
        if not title.startswith("😋"):
            food_data["title"] = f"😋 {title}"

        # ❗️ СОХРАНЯЕМ ДАННЫЕ В ПАМЯТЬ БОТА (для кнопки "Сохранить в дневник")
        await state.update_data(calculated_food=food_data)

        comment = clean_html_tags(str(food_data.get("comment", "Приятного аппетита!")))

        text = (
            f"🍽 <b>{food_data['title']}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🔥 Калории: <b>{food_data.get('calories', 0)} ккал</b>\n"
            f"🥩 Белки: {food_data.get('protein', 0)} г\n"
            f"🥑 Жиры: {food_data.get('fat', 0)} г\n"
            f"🍚 Углеводы: {food_data.get('carbs', 0)} г\n\n"
            f"💬 <i>{comment}</i>\n\n"
            "Внести эту вкусняшку в дневник?"
        )
        
        # ❗️ ВЫВОДИМ КНОПКУ СОХРАНЕНИЯ (как и везде)
        await wait_msg.edit_text(text, reply_markup=result_keyboard())
        
    except Exception as e:
        logger.error(f"Ошибка вкусняшки: {e}")
        await wait_msg.edit_text("Не удалось рассчитать лакомство. Попробуй описать чуть точнее (например: «1 сникерс урбан» или «мороженое стаканчик»).")
# =========================================================
# КНОПКИ И КОМАНДЫ
# =========================================================
@dp.message(F.text == "📊 Сегодня")
@dp.message(Command("today"))
async def today_handler(message: Message):
    await send_today(message)

@dp.message(F.text == "🎯 Моя норма")
@dp.message(Command("plan"))
async def plan_handler(message: Message):
    user = await get_user_profile(message.from_user.id)
    if not user:
        await message.answer("Сначала нажми /start.")
        return
    await message.answer(
        "🎯 <b>Твоя дневная норма</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"🔥 {user.get('calories', 2000)} ккал\n"
        f"🥩 Белки: {user.get('protein', 100)} г\n"
        f"🥑 Жиры: {user.get('fat', 70)} г\n"
        f"🍚 Углеводы: {user.get('carbs', 200)} г"
    )

@dp.message(F.text == "🥗 Что приготовить")
@dp.message(Command("fridge"))
async def fridge_handler(message: Message, state: FSMContext):
    if not await check_user_access(message.from_user.id):
        await send_paywall(message)
        return
    await state.set_state(FoodStates.waiting_for_recipe)
    await message.answer("Напиши продукты через запятую:")

@dp.message(FoodStates.waiting_for_recipe)
async def recipe_handler(message: Message, state: FSMContext):
    await state.clear()
    wait_message = await message.answer("⏳ Собираю рецепт...")
    try:
        result = await ask_ai(prompt=f"Составь рецепт из: {message.text}.", model=AI_MODEL)
        await wait_message.edit_text(clean_html_tags(result))
    except Exception:
        await wait_message.edit_text("Не удалось составить рецепт.")

@dp.message(F.text == "👤 Профиль")
@dp.message(Command("profile"))
async def profile_handler(message: Message):
    await plan_handler(message)

@dp.message(F.text == "❓ Помощь")
@dp.message(Command("help"))
async def help_handler(message: Message):
    await message.answer("📸 Пришли фото еды — я посчитаю КБЖУ.\n🗣 Или наговори голосом!")

# =========================================================
# УТРЕННЯЯ РАССЫЛКА
# =========================================================
def get_russian_date_str() -> str:
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    months = ["янв.", "февр.", "марта", "апр.", "мая", "июня", "июля", "авг.", "сент.", "окт.", "нояб.", "дек."]
    now = datetime.now()
    return f"{days[now.weekday()]}, {now.day} {months[now.month - 1]}"

async def send_morning_digest():
    if not db:
        return
    logger.info("🌅 Запуск утренней рассылки...")
    users_docs = await asyncio.to_thread(db.collection('users').get)
    date_str = get_russian_date_str()
    now = datetime.now()

    for doc in users_docs:
        user_data = doc.to_dict()
        user_id = user_data.get("user_id") or doc.id
        norm_kcal = user_data.get("calories", 2000)
        norm_p = user_data.get("protein", 100)

        # Расчёт статуса подписки / бесплатного триала
        trial_str = user_data.get("trial_until")
        premium_str = user_data.get("premium_until")
        
        status_text = ""
        is_premium = False
        
        # 1. Если активна платная подписка
        if premium_str:
            try:
                prem_end = datetime.fromisoformat(premium_str)
                if now < prem_end:
                    is_premium = True
                    days_prem = (prem_end.date() - now.date()).days
                    status_text = f"💎 <b>Подписка активна:</b> осталось {days_prem} дн."
            except Exception:
                pass

        # 2. Если действует бесплатный период
        if not is_premium:
            if trial_str:
                try:
                    trial_end = datetime.fromisoformat(trial_str)
                    days_left = (trial_end.date() - now.date()).days
                    
                    if days_left > 1:
                        status_text = f"🎁 <b>Пробный период:</b> осталось {days_left} дн. бесплатно"
                    elif days_left == 1:
                        status_text = "🎁 <b>Пробный период:</b> остался 1 день!"
                    elif days_left == 0:
                        status_text = "⏳ <b>Пробный период:</b> заканчивается сегодня!"
                    else:
                        status_text = "🔒 <b>Пробный период завершён.</b> Выбери подписку для продления доступа."
                except Exception:
                    status_text = ""

        status_block = f"\n{status_text}\n" if status_text else ""

        text = (
            f"Доброе утро! Сегодня {date_str} ☀️\n\n"
            f"План на день: <b>{norm_kcal} ккал</b>, <b>Б {norm_p} г</b>.\n"
            f"{status_block}\n"
            f"<i>Неспешно, но уверенно — маленький шаг сегодня приближает к цели!</i>"
        )
        try:
            await bot.send_message(chat_id=int(user_id), text=text)
            await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"Ошибка утренней рассылки {user_id}: {e}")

@dp.message(Command("test_morning"))
async def test_morning_handler(message: Message):
    await send_morning_digest()

# =========================================================
# WEBHOOK ПЛАТЕЖЕЙ BEPAID
# =========================================================
async def bepaid_webhook_handler(request: web.Request):
    try:
        data = await request.json()
        transaction = data.get("transaction", {})
        if transaction.get("status") == "successful":
            tracking_id = transaction.get("tracking_id", "")
            parts = tracking_id.split("_")
            user_id = parts[1]
            months = int(parts[2]) if len(parts) > 2 else 1
            
            user = await get_user_profile(int(user_id))
            now = datetime.now()
            start_date = now
            if user and user.get("premium_until"):
                dt_prem = datetime.fromisoformat(user.get("premium_until"))
                if dt_prem > now:
                    start_date = dt_prem
            
            new_prem = start_date + timedelta(days=30 * months)
            
            if db:
                await asyncio.to_thread(
                    db.collection('users').document(str(user_id)).set,
                    {"premium_until": new_prem.isoformat()},
                    merge=True
                )
            await bot.send_message(
                chat_id=int(user_id),
                text=f"🎉 <b>Оплата прошла успешно!</b>\n\nПодписка активирована до {new_prem.strftime('%d.%m.%Y')}."
            )
        return web.Response(text="OK", status=200)
    except Exception as e:
        logger.error(f"Ошибка вебхука оплаты: {e}")
        return web.Response(text="Error", status=400)

async def health_handler(request: web.Request):
    return web.json_response({"status": "ok", "service": "food-telegram-bot"})

# =========================================================
# ЗАПУСК
# =========================================================
async def main():
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_post("/webhook/bepaid", bepaid_webhook_handler)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    
    try:
        await site.start()
        logger.info("HTTP-сервер запущен на порту %s", port)
        
        bot_info = await bot.get_me()
        logger.info("Telegram подключен: @%s", bot_info.username)
        
        await set_bot_description(bot)
        
        scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
        scheduler.add_job(send_morning_digest, "cron", hour=9, minute=0)
        scheduler.start()

        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except Exception:
        logger.exception("Ошибка запуска")
        raise
    finally:
        await bot.session.close()
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
