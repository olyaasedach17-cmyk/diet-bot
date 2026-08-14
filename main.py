import asyncio
import base64
import io
import json
import logging
import os
import re
import random
from datetime import datetime, timedelta

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
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
        [KeyboardButton(text="💬 Спросить нутрициолога")],
        [KeyboardButton(text="⚖️ Вес"), KeyboardButton(text="👤 Профиль")]
    ],
    resize_keyboard=True,
    input_field_placeholder="Пришли фото еды, наговори голосом...",
)

# =========================================================
# СОСТОЯНИЯ (FSM)
# =========================================================
class Onboarding(StatesGroup):
    stats = State()
    custom_allergy = State()

class FoodStates(StatesGroup):
    correcting = State()
    waiting_for_recipe = State()

class WeightStates(StatesGroup):
    waiting_for_weight = State()

class ActivityStates(StatesGroup):
    waiting_for_activity = State()

class TreatStates(StatesGroup):
    waiting_for_treat = State()

class AskStates(StatesGroup):
    waiting_for_question = State()

# =========================================================
# СОВЕТЫ НУТРИЦИОЛОГА ДЛЯ УТРЕННЕЙ РАССЫЛКИ
# =========================================================
NUTRITION_TIPS = [
    "💡 <b>Совет дня:</b> По «правилу тарелки» 50% обеда должны составлять овощи и зелень (клетчатка). Это здоровая микрофлора и долгая сытость!",
    "💡 <b>Совет дня:</b> Качественный белок в каждом приёме пищи защищает от резких скачков сахара и вечерних срывов на сладкое.",
    "💡 <b>Совет дня:</b> Масло гхи или кокосовое — идеальный выбор для жарки. А нерафинированное оливковое оставь для свежих салатов.",
    "💡 <b>Совет дня:</b> Захотелось десерт? Съешь его сразу после сытного обеда — так сахар в крови поднимется плавно и без вреда.",
    "💡 <b>Совет дня:</b> Стакан тёплой воды с утра мягко будит ЖКТ и запускает метаболизм. Не забывай пить воду в течение дня!",
    "💡 <b>Совет дня:</b> Сложные углеводы (гречка, киноа, бурый рис) дают стабильную энергию на 3–4 часа без ощущения тяжести."
]

# =========================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =========================================================
def clean_html_tags(text: str) -> str:
    return re.sub(r'<(?!/?(b|i|code|s|u)\b)[^>]*>', '', text)

def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")

def make_progress_bar(current: int, target: int, length: int = 10) -> str:
    if target <= 0:
        return "▱" * length
    fraction = min(max(current / target, 0.0), 1.0)
    filled_length = int(round(length * fraction))
    return "▰" * filled_length + "▱" * (length - filled_length)

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
    data['diet'] = data.get('diet', 'all')
    data['allergies'] = data.get('allergies', 'Нет')
    data['family_mode'] = data.get('family_mode', 'self')
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
        "🍽 Сфотографируй еду — я определю состав, "
        "рассчитаю КБЖУ и добавлю приём пищи в дневник.\n\n"
        "🎤 Пойму голосовое сообщение\n"
        "🏋️ Составлю программу тренировок\n"
        "🧊 Соберу меню по принципам нутрициологии (и для детей тоже!)\n"
        "💬 Отвечу на любые вопросы про питание\n"
        "🎯 Рассчитаю личную норму КБЖУ"
    )
    try:
        await bot.set_my_description(description)
    except Exception:
        pass

async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="today", description="📊 Дневник за сегодня"),
        BotCommand(command="treat", description="😋 Вкусняшка"),
        BotCommand(command="fridge", description="🥗 Что приготовить"),
        BotCommand(command="workout", description="🏋️ Тренировка"),
        BotCommand(command="ask", description="💬 Спросить нутрициолога"),
        BotCommand(command="weight", description="⚖️ Динамика веса"),
        BotCommand(command="profile", description="👤 Профиль и норма"),
        BotCommand(command="help", description="❓ Помощь"),
    ]
    try:
        await bot.set_my_commands(commands)
    except Exception as e:
        logger.warning(f"Не удалось установить команды: {e}")

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

# =========================================================
# ЖЕЛЕЗОБЕТОННАЯ МАТЕМАТИКА (ЗАЩИТА НОРМЫ И КАЛОРИЙ)
# =========================================================
def calculate_norm(gender: str, age: int, height: float, weight: float, goal: str, activity: str) -> dict:
    if gender == "M":
        bmr = 10 * weight + 6.25 * height - 5 * age + 5
    else:
        bmr = 10 * weight + 6.25 * height - 5 * age - 161
        
    # Защита от экстремально низкого базового обмена
    bmr = max(bmr, 1200.0) 
        
    activity_coefficients = {"low": 1.2, "light": 1.375, "medium": 1.55, "high": 1.725}
    tdee = bmr * activity_coefficients.get(activity, 1.2)

    if goal == "loss":
        # Здоровый дефицит 15%, но СТРОГО не ниже базового обмена + 5%
        calories = max(tdee * 0.85, bmr * 1.05)
    elif goal == "gain":
        calories = tdee * 1.15
    else:
        calories = tdee

    calories = int(calories)
    
    # Идеальное распределение БЖУ (30% Белки / 30% Жиры / 40% Углеводы)
    return {
        "calories": calories,
        "protein": int((calories * 0.30) / 4),
        "fat": int((calories * 0.30) / 9),
        "carbs": int((calories * 0.40) / 4),
    }

def extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("AI не вернул валидный JSON")
    
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        raise ValueError("Ошибка чтения JSON от ИИ")
    
    # Защита от букв и отрицательных чисел от ИИ
    try:
        p = max(0, float(data.get("protein", 0) or 0))
        f = max(0, float(data.get("fat", 0) or 0))
        c = max(0, float(data.get("carbs", 0) or 0))
    except (ValueError, TypeError):
        p, f, c = 0.0, 0.0, 0.0
    
    # ПРИНУДИТЕЛЬНЫЙ программный пересчет калорий (мы не верим калориям от ИИ)
    calc_calories = int((p * 4) + (f * 9) + (c * 4))
    
    data["protein"] = int(p)
    data["fat"] = int(f)
    data["carbs"] = int(c)
    data["calories"] = calc_calories
    
    if "title" not in data or not str(data["title"]).strip():
        data["title"] = "Приём пищи"
    
    return data

# =========================================================
# AI-ОБЕРТКА
# =========================================================
async def ask_ai(prompt: str, image_base64: str | None = None, model: str | None = None) -> str:
    used_model = model or AI_MODEL
    
    system_prompt = (
        "Ты профессиональный, эмпатичный нутрициолог и фитнес-тренер. "
        "Твой подход — забота, поддержка и отсутствие любого осуждения. "
        "НИКОГДА не ругай пользователя за сладкое, фастфуд или срывы. Поддерживай принцип 80/20. "
        "Отвечай кратко, красиво и структурно. "
        "Используй ТОЛЬКО разрешённые HTML теги Telegram: <b>текст</b>, <i>текст</i>, <code>код</code>. "
        "КРИТИЧЕСКИ ВАЖНО: ЗАПРЕЩЕНО использовать таблицы Markdown (символ |) и заголовки Markdown (###)."
    )

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
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=0.3,
        )
        return response.choices[0].message.content or "" if response.choices else ""
    except Exception as e:
        logger.error(f"🔥 Ошибка AI ({used_model}): {e}")
        raise

# =========================================================
# БИЛЛИНГ И КЛАВИАТУРЫ
# =========================================================
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

# =========================================================
# ВЫВОД ДНЕВНИКА
# =========================================================
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
            f"▫️ <b>{title_clean}</b>\n"
            f"   🔥 {meal.get('calories', 0)} ккал | Б {meal.get('protein', 0)}г | Ж {meal.get('fat', 0)}г | У {meal.get('carbs', 0)}г\n\n"
        )
        
    net_kcal = total_kcal - burned_kcal
    if net_kcal > norm_kcal:
        overage_kcal = net_kcal - norm_kcal
        status_text = (
            f"⚠️ <b>Профицит:</b> +{overage_kcal} ккал\n"
            f"<i>Баланс в долгосрочной перспективе важнее одного дня. Всё отлично, продолжай! 💪</i>"
        )
    else:
        rem_kcal = norm_kcal - net_kcal
        status_text = f"✅ <b>Осталось на сегодня:</b> {rem_kcal} ккал"

    burned_str = f" <i>(-{burned_kcal} ккал активностью)</i>" if burned_kcal > 0 else ""

    text = (
        f"📅 <b>ТВОЙ ДЕНЬ В ЦИФРАХ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{meals_text}"
        f"⚡️ <b>Энергия:</b> {total_kcal} / {norm_kcal} ккал ({pct_kcal}%){burned_str}\n"
        f"<code>{make_progress_bar(total_kcal, norm_kcal)}</code>\n\n"
        f"🥩 <b>Белки:</b> {total_p} / {norm_p} г\n"
        f"<code>{make_progress_bar(total_p, norm_p, 8)}</code>\n\n"
        f"🥑 <b>Жиры:</b> {total_f} / {norm_f} г\n"
        f"<code>{make_progress_bar(total_f, norm_f, 8)}</code>\n\n"
        f"🍚 <b>Углеводы:</b> {total_c} / {norm_c} г\n"
        f"<code>{make_progress_bar(total_c, norm_c, 8)}</code>\n\n"
        f"💧 <b>Выпитая вода:</b> {water_ml} мл\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{status_text}"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💧 +250 мл воды", callback_data="add_water_250"),
            InlineKeyboardButton(text="🗑 Удалить последнее", callback_data="delete_last_meal")
        ]
    ])
    await message.answer(text, reply_markup=kb)

# =========================================================
# ГЛАВНЫЕ КОМАНДЫ И КНОПКИ
# =========================================================
@dp.message(Command("admin_broadcast"))
async def admin_broadcast_handler(message: Message):
    text = message.text.replace("/admin_broadcast", "").strip()
    if not text:
        await message.answer("Введите текст после команды: /admin_broadcast Ваш текст")
        return
        
    if not db:
        await message.answer("Ошибка БД")
        return
        
    users_docs = await asyncio.to_thread(db.collection('users').get)
    count = 0
    await message.answer("⏳ Начинаю рассылку...")
    
    for doc in users_docs:
        uid = doc.id
        try:
            await bot.send_message(chat_id=int(uid), text=text)
            count += 1
            await asyncio.sleep(0.1)
        except Exception:
            pass
    await message.answer(f"✅ Рассылка завершена! Отправлено: {count} чел.")

@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    await state.clear()
    user = await get_user_profile(message.from_user.id)
    if user:
        await message.answer("С возвращением! Пришли фото еды 📸", reply_markup=main_menu)
        return

    welcome_text = (
        f"Привет! Это «NutriAi» — твой нутрициолог в телефоне 🥗\n\n"
        "📸 считаю КБЖУ по фото еды\n"
        "📊 веду дневник питания\n"
        "🏋️ подбираю программы тренировок\n"
        "🧊 собираю полезное меню из холодильника\n\n"
        "🎁 <b>Пробный период:</b> 14 дней бесплатно!\n\n"
        "Сначала короткий опрос (7 вопросов), чтобы посчитать <b>твою</b> персональную норму."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Начать", callback_data="start_onb")]
    ])
    
    await message.answer(welcome_text, reply_markup=main_menu)
    await message.answer("Жми кнопку ниже 👇", reply_markup=kb)

@dp.message(F.text == "📊 Сегодня")
@dp.message(Command("today"))
async def today_handler(message: Message, state: FSMContext):
    await state.clear()
    await send_today(message)

@dp.message(F.text == "👤 Профиль")
@dp.message(Command("profile"))
async def profile_handler(message: Message, state: FSMContext):
    await state.clear()
    user = await get_user_profile(message.from_user.id)
    if not user:
        return await message.answer("Сначала нажми /start.")

    diet_titles = {"all": "🍏 Ем всё", "nutri": "🥦 Нутри-подход", "veg": "🥕 Вегетарианец", "allergy": "🚫 Без лактозы и глютена"}
    fam_str = "👨‍👩‍👧‍👦 Готовлю для семьи (без скрытого сахара)" if user.get('family_mode') == 'kids' else "🧍 Только для себя"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛡 Изменить аллергии", callback_data="profile_edit_allergies")],
        [InlineKeyboardButton(text="⚙️ Пересчитать норму (Опрос)", callback_data="profile_recount_norm")]
    ])

    await message.answer(
        "👤 <b>ТВОЙ ПРОФИЛЬ И НОРМА</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🔥 Калории: <b>{user.get('calories', 2000)} ккал</b>\n"
        f"🥩 Белки: <b>{user.get('protein', 100)} г</b>\n"
        f"🥑 Жиры: <b>{user.get('fat', 70)} г</b>\n"
        f"🍚 Углеводы: <b>{user.get('carbs', 200)} г</b>\n\n"
        f"🥗 <b>Питание:</b> {diet_titles.get(user.get('diet'), 'Обычное')}\n"
        f"👶 <b>Режим:</b> {fam_str}\n"
        f"🛡 <b>Аллергии:</b> {user.get('allergies', 'Нет')}",
        reply_markup=kb
    )

@dp.message(F.text == "🎯 Моя норма")
@dp.message(Command("plan"))
async def plan_handler(message: Message, state: FSMContext):
    await state.clear()
    user = await get_user_profile(message.from_user.id)
    if not user:
        return await message.answer("Сначала нажми /start.")
    
    await message.answer(
        "🎯 <b>Твоя дневная норма</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"🔥 {user.get('calories', 2000)} ккал\n"
        f"🥩 Белки: {user.get('protein', 100)} г\n"
        f"🥑 Жиры: {user.get('fat', 70)} г\n"
        f"🍚 Углеводы: {user.get('carbs', 200)} г\n"
        f"🛡 Аллергии: {user.get('allergies', 'Нет')}"
    )

@dp.message(F.text == "😋 Вкусняшка")
@dp.message(Command("treat"))
async def treat_button_handler(message: Message, state: FSMContext):
    await state.clear()
    if not await check_user_access(message.from_user.id): return await send_paywall(message)
    await state.set_state(TreatStates.waiting_for_treat)
    await message.answer("😋 <b>Съел(а) что-то вкусное?</b>\nНапиши текстом или голосом.")

@dp.message(F.text == "🥗 Что приготовить")
@dp.message(Command("fridge"))
async def fridge_handler(message: Message, state: FSMContext):
    await state.clear()
    if not await check_user_access(message.from_user.id): return await send_paywall(message)
    await state.set_state(FoodStates.waiting_for_recipe)
    await message.answer("Напиши продукты через запятую, и я соберу идеальный рецепт:")

@dp.message(F.text == "🏋️ Тренировка")
@dp.message(Command("workout"))
async def workout_menu_handler(message: Message, state: FSMContext):
    await state.clear()
    if not await check_user_access(message.from_user.id): return await send_paywall(message)
    user = await get_user_profile(message.from_user.id)
    fav_workout = user.get("favorite_workout_name") if user else None

    intro = f"🏋️ <b>ТРЕНИРОВКИ</b>\n<i>Заметил(а), что ты часто выбираешь: {fav_workout}</i>" if fav_workout else "🏋️ <b>ТРЕНИРОВКИ И АКТИВНОСТЬ</b>\nВыбери программу:"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Дома · Новичок 🟢", callback_data="gen_workout_home_easy")],
        [InlineKeyboardButton(text="🏠 Дома · Продвинутый (60 мин) 🔴", callback_data="gen_workout_home_hard")],
        [InlineKeyboardButton(text="🏋️ В зале · Новичок 🟢", callback_data="gen_workout_gym_easy")],
        [InlineKeyboardButton(text="🏋️ В зале · Продвинутый (60 мин) 🔴", callback_data="gen_workout_gym_hard")],
        [InlineKeyboardButton(text="👣 Своя активность или Шаги", callback_data="enter_custom_activity")]
    ])
    await message.answer(intro, reply_markup=kb)

@dp.message(F.text == "💬 Спросить нутрициолога")
@dp.message(Command("ask"))
async def ask_nutritionist_handler(message: Message, state: FSMContext):
    await state.clear()
    if not await check_user_access(message.from_user.id): return await send_paywall(message)
    await state.set_state(AskStates.waiting_for_question)
    await message.answer("💬 <b>Я на связи — спрашивай что угодно!</b>\nОтправь вопрос текстом или голосом.")

@dp.message(F.text == "⚖️ Вес")
@dp.message(Command("weight"))
async def weight_handler(message: Message, state: FSMContext):
    await state.clear()
    user = await get_user_profile(message.from_user.id)
    if not user: return await message.answer("Сначала пройди регистрацию: /start")
    await state.set_state(WeightStates.waiting_for_weight)
    await message.answer(f"Твой текущий вес: <b>{user.get('weight', '—')} кг</b>\nНапиши новый вес числом:")

@dp.message(F.text == "❓ Помощь")
@dp.message(Command("help"))
async def help_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("📸 Пришли фото еды — я посчитаю КБЖУ.\n🗣 Или наговори голосом!")

# =========================================================
# ОНБОРДИНГ И НАСТРОЙКИ СЕМЬИ
# =========================================================
@dp.callback_query(F.data == "start_onb")
async def start_onboarding_callback(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👨 Мужчина", callback_data="gender_M")],
        [InlineKeyboardButton(text="👩 Женщина", callback_data="gender_F")],
    ])
    await callback.message.edit_text("<i>Шаг 1 из 7</i>\n\nТвой <b>пол</b>?", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("gender_"))
async def gender_handler(callback: CallbackQuery, state: FSMContext):
    await state.update_data(gender=callback.data.split("_")[1])
    await state.set_state(Onboarding.stats)
    await callback.message.edit_text("<i>Шаг 2 из 7</i>\n\nНапиши через пробел:\n<b>возраст рост вес</b>\n(Например: <code>32 182 92</code>)")
    await callback.answer()

@dp.message(Onboarding.stats)
async def stats_handler(message: Message, state: FSMContext):
    numbers = re.findall(r"\d+(?:[.,]\d+)?", message.text)
    if len(numbers) < 3: 
        return await message.answer("Нужно три значения: возраст, рост и вес.")
        
    await state.update_data(
        age=int(float(numbers[0].replace(",", "."))), 
        height=float(numbers[1].replace(",", ".")), 
        weight=float(numbers[2].replace(",", "."))
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📉 Похудеть", callback_data="goal_loss")],
        [InlineKeyboardButton(text="⚖️ Удержать вес", callback_data="goal_maintain")],
        [InlineKeyboardButton(text="📈 Набрать массу", callback_data="goal_gain")],
    ])
    await message.answer("<i>Шаг 3 из 7</i>\n\nКакая у тебя цель?", reply_markup=keyboard)

@dp.callback_query(F.data.startswith("goal_"))
async def goal_handler(callback: CallbackQuery, state: FSMContext):
    await state.update_data(goal=callback.data.split("_")[1])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛋 Сидячий образ жизни", callback_data="activity_low")],
        [InlineKeyboardButton(text="🚶 Лёгкая активность", callback_data="activity_light")],
        [InlineKeyboardButton(text="🏃 Умеренная активность", callback_data="activity_medium")],
        [InlineKeyboardButton(text="🏋️ Высокая активность", callback_data="activity_high")],
    ])
    await callback.message.edit_text("<i>Шаг 4 из 7</i>\n\nФизическая активность:", reply_markup=keyboard)

@dp.callback_query(F.data.startswith("activity_"))
async def activity_handler(callback: CallbackQuery, state: FSMContext):
    await state.update_data(activity=callback.data.split("_")[1])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🍏 Ем всё", callback_data="diet_all")],
        [InlineKeyboardButton(text="🥦 Нутри-подход (фокус на клетчатку)", callback_data="diet_nutri")],
        [InlineKeyboardButton(text="🥕 Вегетарианец", callback_data="diet_veg")],
        [InlineKeyboardButton(text="🚫 Без лактозы и глютена", callback_data="diet_allergy")],
    ])
    await callback.message.edit_text("<i>Шаг 5 из 7</i>\n\nПредпочтения в питании:", reply_markup=keyboard)

@dp.callback_query(F.data.startswith("diet_"))
async def diet_handler(callback: CallbackQuery, state: FSMContext):
    await state.update_data(diet=callback.data.split("_")[1])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧍 Только для себя", callback_data="fam_self")],
        [InlineKeyboardButton(text="👨‍👩‍👧‍👦 Готовлю на всю семью (дети)", callback_data="fam_kids")],
    ])
    await callback.message.edit_text("<i>Шаг 6 из 7</i>\n\nДля кого составляем рацион?\n<i>С учетом детей мы уберем скрытый сахар и добавим семейные блюда.</i>", reply_markup=keyboard)

@dp.callback_query(F.data.startswith("fam_"))
async def fam_handler(callback: CallbackQuery, state: FSMContext):
    await state.update_data(family_mode=callback.data.split("_")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌰 Орехи", callback_data="allergy_nuts"), InlineKeyboardButton(text="🥛 Лактоза", callback_data="allergy_lactose")],
        [InlineKeyboardButton(text="🌾 Глютен", callback_data="allergy_gluten"), InlineKeyboardButton(text="🐟 Морепродукты", callback_data="allergy_seafood")],
        [InlineKeyboardButton(text="✍️ Написать текстом", callback_data="allergy_custom")],
        [InlineKeyboardButton(text="❌ Нет аллергий", callback_data="allergy_none")],
    ])
    await callback.message.edit_text("<i>Шаг 7 из 7</i>\n\nЕсть ли аллергии?", reply_markup=kb)

@dp.callback_query(F.data.startswith("allergy_"))
async def allergy_callback_handler(callback: CallbackQuery, state: FSMContext):
    allergy_type = callback.data.split("_")[1]
    if allergy_type == "custom":
        await state.set_state(Onboarding.custom_allergy)
        return await callback.message.edit_text("✍️ Напиши текстом, на что аллергия:")
    
    allergies_map = {"nuts": "Орехи", "lactose": "Лактоза", "gluten": "Глютен", "seafood": "Рыба", "none": "Нет"}
    await finish_onboarding(callback.message, state, callback.from_user.id, allergies_map.get(allergy_type, "Нет"), is_callback=True)

@dp.message(Onboarding.custom_allergy)
async def custom_allergy_handler(message: Message, state: FSMContext):
    await finish_onboarding(message, state, message.from_user.id, message.text.strip(), is_callback=False)

async def finish_onboarding(target_message, state: FSMContext, user_id: int, allergy_text: str, is_callback: bool = False):
    data = await state.get_data()
    norm = calculate_norm(data["gender"], data["age"], data["height"], data["weight"], data["goal"], data["activity"])
    
    trial_end = datetime.now() + timedelta(days=14)
    user_data = {
        "user_id": user_id, "gender": data["gender"], "age": data["age"], 
        "height": data["height"], "weight": data["weight"], "goal": data["goal"], 
        "activity": data["activity"], "diet": data.get("diet", "all"), 
        "family_mode": data.get("family_mode", "self"), "allergies": allergy_text,
        "calories": norm["calories"], "protein": norm["protein"], "fat": norm["fat"], "carbs": norm["carbs"],
        "target_weight": 58.0 if data["gender"] == "F" else 75.0,
        "trial_until": trial_end.isoformat(), "premium_until": None, "created_at": datetime.now().isoformat()
    }
    await save_user_profile(user_id, user_data)
    await state.clear()
    
    text = (
        "🎯 <b>Твоя дневная норма рассчитана</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"🔥 {norm['calories']} ккал | Б {norm['protein']}г | Ж {norm['fat']}г | У {norm['carbs']}г\n"
        f"🛡 <b>Аллергии:</b> {allergy_text}\n"
        f"👨‍👩‍👧‍👦 <b>Режим семьи:</b> {'Включен' if data.get('family_mode') == 'kids' else 'Выключен'}\n\n"
        f"🎁 <b>Активировано 14 дней бесплатно!</b>\nТеперь пришли фото еды 📸"
    )
    if is_callback:
        await target_message.edit_text(text)
        await target_message.answer("Готово! Жду фото.", reply_markup=main_menu)
    else:
        await target_message.answer(text, reply_markup=main_menu)

# =========================================================
# РЕДАКТИРОВАНИЕ ПРОФИЛЯ
# =========================================================
@dp.callback_query(F.data == "profile_edit_allergies")
async def profile_edit_allergies_handler(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌰 Орехи", callback_data="update_alg_nuts"), InlineKeyboardButton(text="🥛 Лактоза", callback_data="update_alg_lactose")],
        [InlineKeyboardButton(text="🌾 Глютен", callback_data="update_alg_gluten"), InlineKeyboardButton(text="🐟 Рыба / Морепродукты", callback_data="update_alg_seafood")],
        [InlineKeyboardButton(text="❌ Нет аллергий", callback_data="update_alg_none")],
    ])
    await callback.message.edit_text("🛡 <b>Выбери аллергию:</b>", reply_markup=kb)

@dp.callback_query(F.data.startswith("update_alg_"))
async def update_allergy_callback_handler(callback: CallbackQuery):
    alg_type = callback.data.replace("update_alg_", "")
    amap = {"nuts": "Орехи", "lactose": "Лактоза", "gluten": "Глютен", "seafood": "Рыба", "none": "Нет"}
    await save_user_profile(callback.from_user.id, {"allergies": amap.get(alg_type, "Нет")})
    await callback.message.edit_text(f"✅ Обновлено: {amap.get(alg_type, 'Нет')}")

@dp.callback_query(F.data == "profile_recount_norm")
async def profile_recount_norm_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await start_onboarding_callback(callback)

# =========================================================
# ВЕС (ОБНОВЛЕНИЕ И АВТО-ПЕРЕСЧЕТ)
# =========================================================
@dp.message(WeightStates.waiting_for_weight)
async def process_weight_update(message: Message, state: FSMContext):
    numbers = re.findall(r"\d+(?:[.,]\d+)?", message.text)
    if not numbers: 
        return await message.answer("Напиши вес числом.")
        
    new_weight = float(numbers[0].replace(",", "."))
    user = await get_user_profile(message.from_user.id)
    
    new_norm = calculate_norm(
        gender=user.get("gender", "F"), age=user.get("age", 25), height=user.get("height", 165),
        weight=new_weight, goal=user.get("goal", "loss"), activity=user.get("activity", "low")
    )
    
    await save_user_profile(message.from_user.id, {
        "weight": new_weight, "calories": new_norm["calories"],
        "protein": new_norm["protein"], "fat": new_norm["fat"], "carbs": new_norm["carbs"]
    })
    await state.clear()
    
    await message.answer(
        f"⚖️ Новый вес <b>{new_weight} кг</b> зафиксирован!\n\n"
        f"🎯 <b>Новая норма пересчитана:</b>\n"
        f"{new_norm['calories']} ккал (Б:{new_norm['protein']} Ж:{new_norm['fat']} У:{new_norm['carbs']})"
    )

# =========================================================
# УТРЕННИЙ И ВЕЧЕРНИЙ РАЗБОР
# =========================================================
async def send_morning_digest():
    if not db: return
    users_docs = await asyncio.to_thread(db.collection('users').get)
    for doc in users_docs:
        u = doc.to_dict()
        try:
            await bot.send_message(
                chat_id=int(u.get("user_id", doc.id)),
                text=f"☀️ Доброе утро!\nПлан на день: <b>{u.get('calories', 2000)} ккал</b>\n\n{random.choice(NUTRITION_TIPS)}"
            )
            await asyncio.sleep(0.1)
        except: pass

async def send_evening_digest():
    if not db: return
    users_docs = await asyncio.to_thread(db.collection('users').get)
    for doc in users_docs:
        u = doc.to_dict()
        uid = u.get("user_id") or doc.id
        diary_doc = await asyncio.to_thread(db.collection('diaries').document(f"{uid}_{today_str()}").get)
        
        if not diary_doc.exists or not diary_doc.to_dict().get('meals'):
            continue
            
        d = diary_doc.to_dict()
        meals = d.get('meals', [])
        tk = sum(m.get('calories', 0) for m in meals)
        tp = sum(m.get('protein', 0) for m in meals)
        
        prompt = (
            f"Оцени день: съедено {tk} из {u.get('calories', 2000)} ккал (Белок {tp}г). "
            f"Напиши короткий и теплый вечерний разбор нутрициолога. Мягко похвали, дай 1 микро-совет на завтра. "
            f"Без критики. Используй HTML."
        )
        try:
            review = await ask_ai(prompt=prompt, model=AI_MODEL)
            await bot.send_message(chat_id=int(uid), text=f"🌙 <b>Итоги дня</b>\n━━━━━━━━━\n{clean_html_tags(review)}")
            await asyncio.sleep(0.1)
        except: pass

# =========================================================
# УМНЫЙ ИИ-МАРШРУТИЗАТОР (ОБРАБОТКА ТЕКСТА И ГОЛОСА)
# =========================================================
async def process_smart_input(text: str, message: Message, state: FSMContext, wait_msg: Message):
    try:
        intent = await ask_ai(prompt=f"Текст: \"{text}\". Ответь 1 словом: ACTIVITY, FOOD или QUESTION.", model=AI_MODEL)
        user = await get_user_profile(message.from_user.id) or {}
        fam_ctx = "Учитывай, что блюдо/совет должен подходить для детей (без сахара, цельные продукты)." if user.get('family_mode') == 'kids' else ""

        if "ACTIVITY" in intent:
            res = extract_json(await ask_ai(prompt=f"Вес {user.get('weight', 70)}кг, выполнил: {text}. Верни JSON: {{\"title\":\"\",\"burned_kcal\":0,\"comment\":\"\"}}", model=AI_MODEL))
            await state.update_data(calculated_activity=res)
            await wait_msg.edit_text(f"🏃 <b>{res.get('title')}</b>\n🔥 Расход: {res.get('burned_kcal',0)} ккал\n\nДобавить?", reply_markup=activity_result_keyboard())

        elif "FOOD" in intent:
            res = extract_json(await ask_ai(prompt=f"Съел: {text}. Верни JSON: {{\"title\":\"\",\"protein\":0,\"fat\":0,\"carbs\":0,\"comment\":\"\"}}", model=AI_MODEL))
            await state.update_data(calculated_food=res)
            await wait_msg.edit_text(f"🍽 <b>{res['title']}</b>\n🔥 {res['calories']} ккал (Б:{res['protein']} Ж:{res['fat']} У:{res['carbs']})\n\nВнести?", reply_markup=result_keyboard())

        else:
            ans = await ask_ai(prompt=f"Вопрос: {text}\nАллергии: {user.get('allergies','Нет')}\n{fam_ctx}\nОтветь как нутрициолог (коротко, HTML).", model=AI_MODEL)
            await wait_msg.edit_text(clean_html_tags(ans))

    except Exception as e:
        logger.error(f"Router error: {e}")
        await wait_msg.edit_text("Не удалось разобрать сообщение.")

@dp.message(F.voice)
async def voice_handler(message: Message, state: FSMContext):
    if not await check_user_access(message.from_user.id): return await send_paywall(message)
    wait_msg = await message.answer("Слушаю... 🎧")
    try:
        voice_file = await bot.get_file(message.voice.file_id)
        buffer = io.BytesIO()
        await bot.download_file(voice_file.file_path, destination=buffer)
        buffer.name = "voice.ogg"
        text = (await ai_client.audio.transcriptions.create(model="whisper-1", file=buffer)).text
        await wait_msg.edit_text(f"🗣 <b>Вы сказали:</b> «{text}»\n\n⏳ Думаю...")
        await process_smart_input(text, message, state, wait_msg)
    except: await wait_msg.edit_text("Ошибка аудио.")

@dp.message(F.text)
async def universal_text_handler(message: Message, state: FSMContext):
    if not await check_user_access(message.from_user.id): return await send_paywall(message)
    
    # Игнорируем команды и системные кнопки меню
    if message.text.startswith('/'): return
    known_buttons = [
        "📊 Сегодня", "😋 Вкусняшка", "🥗 Что приготовить", 
        "🏋️ Тренировка", "💬 Спросить нутрициолога", "⚖️ Вес", 
        "👤 Профиль", "🎯 Моя норма", "❓ Помощь"
    ]
    if message.text in known_buttons: return
    if await state.get_state(): return

    wait_msg = await message.answer("🤔 Читаю...")
    await process_smart_input(message.text, message, state, wait_msg)

# =========================================================
# РЕЦЕПТЫ И ФОТО ЕДЫ
# =========================================================
@dp.message(FoodStates.waiting_for_recipe)
async def recipe_handler(message: Message, state: FSMContext):
    await state.clear()
    wait_message = await message.answer("⏳ Собираю рецепт...")
    user = await get_user_profile(message.from_user.id) or {}
    fam_str = "ДЛЯ ВСЕЙ СЕМЬИ (АДАПТИРОВАТЬ ДЛЯ ДЕТЕЙ, СТРОГО БЕЗ САХАРА И КОНЦЕНТРАТОВ)" if user.get('family_mode') == 'kids' else "Обычное взрослое"
    
    try:
        result = await ask_ai(prompt=f"Рецепт из: {message.text}.\nРежим: {fam_str}\nАллергии: {user.get('allergies')}\nHTML теги.", model=AI_MODEL)
        await wait_message.edit_text(clean_html_tags(result))
    except:
        await wait_message.edit_text("Не удалось составить рецепт.")

@dp.message(F.photo)
async def photo_handler(message: Message, state: FSMContext):
    if not await check_user_access(message.from_user.id): return await send_paywall(message)
    wait_message = await message.answer("👀 Анализирую фотографию...")
    try:
        telegram_file = await bot.get_file(message.photo[-1].file_id)
        buffer = io.BytesIO()
        await bot.download_file(telegram_file.file_path, destination=buffer)
        b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
        
        result = await ask_ai(image_base64=b64, prompt="Определи еду на фото, ингредиенты и вес. Калории пока не считай.", model=AI_VISION_MODEL)
        await state.update_data(recognized_food=result, image_base64=b64)
        await wait_message.edit_text(f"{clean_html_tags(result)}\n\nВсё верно?", reply_markup=food_keyboard())
    except:
        await wait_message.edit_text("Не удалось обработать фото.")

@dp.callback_query(F.data == "food_correct")
async def food_correct_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await callback.message.edit_text("⏳ Рассчитываю КБЖУ...")
    try:
        res = await ask_ai(prompt=f"Рассчитай БЖУ:\n{data.get('recognized_food')}\nВерни JSON: {{\"title\":\"\",\"protein\":0,\"fat\":0,\"carbs\":0,\"comment\":\"\"}}", model=AI_MODEL)
        food_data = extract_json(res)
        await state.update_data(calculated_food=food_data)
        
        txt = (
            f"🍽 <b>{food_data['title']}</b>\n━━━━━━━━━\n🔥 <b>{food_data['calories']} ккал</b>\n"
            f"Б: {food_data['protein']}г | Ж: {food_data['fat']}г | У: {food_data['carbs']}г\n\n"
            f"💬 <i>{food_data.get('comment','')}</i>\nВнести в дневник?"
        )
        await callback.message.edit_text(txt, reply_markup=result_keyboard())
    except:
        await callback.message.edit_text("Ошибка расчета.")

@dp.callback_query(F.data == "meal_save")
async def save_meal_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    food = data.get("calculated_food", {})
    await add_meal_to_today(callback.from_user.id, {
        "title": food.get("title", "Еда"), "calories": food.get("calories", 0),
        "protein": food.get("protein", 0), "fat": food.get("fat", 0), "carbs": food.get("carbs", 0)
    })
    await state.clear()
    await callback.message.edit_text("✅ Сохранено в дневник.")
    await send_today(callback.message, user_id=callback.from_user.id)

@dp.callback_query(F.data == "food_delete")
async def delete_food_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🗑 Отменено.")

# =========================================================
# WEBHOOK ПЛАТЕЖЕЙ BEPAID И HEALTH
# =========================================================
async def bepaid_webhook_handler(request: web.Request):
    try:
        data = await request.json()
        if data.get("transaction", {}).get("status") == "successful":
            uid = data["transaction"]["tracking_id"].split("_")[1]
            new_prem = datetime.now() + timedelta(days=30)
            await asyncio.to_thread(db.collection('users').document(str(uid)).set, {"premium_until": new_prem.isoformat()}, merge=True)
            await bot.send_message(chat_id=int(uid), text="🎉 <b>Оплата успешна!</b>")
        return web.Response(text="OK", status=200)
    except: return web.Response(text="Error", status=400)

async def health_handler(request: web.Request): 
    return web.json_response({"status": "ok"})

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
    
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000")))
    
    try:
        await site.start()
        logger.info("HTTP-сервер запущен")
        await set_bot_description(bot)
        await set_bot_commands(bot)
        
        scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
        scheduler.add_job(send_morning_digest, "cron", hour=9, minute=0)
        scheduler.add_job(send_evening_digest, "cron", hour=21, minute=0)
        scheduler.start()

        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except Exception:
        raise
    finally:
        await bot.session.close()
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
