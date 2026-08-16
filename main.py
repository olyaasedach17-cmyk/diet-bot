import asyncio
import base64
import io
import json
import logging
import os
import re
import random
import uuid
from datetime import datetime, timedelta, timezone

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Message,
)
from openai import AsyncOpenAI
import firebase_admin
from firebase_admin import credentials, firestore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# =========================================================
# НАСТРОЙКИ, ЛОГИ И ЧАСОВОЙ ПОЯС (UTC+3 МИНСК / МОСКВА)
# =========================================================
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

LOCAL_TZ = timezone(timedelta(hours=3))

def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)

def today_str() -> str:
    return now_local().strftime("%Y-%m-%d")

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
AI_API_KEY = os.getenv("AI_API_KEY") or os.getenv("POLZA_API_KEY")
AI_BASE_URL = os.getenv("AI_BASE_URL") or os.getenv("POLZA_BASE_URL")

AI_MODEL = os.getenv("AI_MODEL", "openai/gpt-5.6-luna")
AI_VISION_MODEL = os.getenv("AI_VISION_MODEL") or AI_MODEL

BEPAID_SHOP_ID = os.getenv("BEPAID_SHOP_ID", "")
BEPAID_SECRET_KEY = os.getenv("BEPAID_SECRET_KEY", "")

if not BOT_TOKEN: raise RuntimeError("Не найден BOT_TOKEN")
if not AI_API_KEY: raise RuntimeError("Не найден AI_API_KEY")
if not AI_BASE_URL: raise RuntimeError("Не найден AI_BASE_URL")

# =========================================================
# FIREBASE ИНИЦИАЛИЗАЦИЯ
# =========================================================
firebase_json_str = os.getenv("FIREBASE_JSON") or os.getenv("FIREBASE_CREDENTIALS")
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
    logger.warning("⚠️ FIREBASE_JSON не найден.")
    db = None

# =========================================================
# AI-КЛИЕНТ И TELEGRAM
# =========================================================
ai_client = AsyncOpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL.rstrip("/"), timeout=90, max_retries=2)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Сегодня"), KeyboardButton(text="😋 Вкусняшка")],
        [KeyboardButton(text="🥗 Что приготовить"), KeyboardButton(text="🏋️ Тренировка")],
        [KeyboardButton(text="💬 Спросить нутрициолога")],
        [KeyboardButton(text="⚖️ Вес"), KeyboardButton(text="👤 Профиль"), KeyboardButton(text="🎯 Моя норма")],
        [KeyboardButton(text="🍽 Мои блюда")]
    ],
    resize_keyboard=True,
    input_field_placeholder="Пришли фото еды, наговори голосом...",
)

# =========================================================
# СОСТОЯНИЯ (FSM)
# =========================================================
class Onboarding(StatesGroup):
    stats = State()
    target_weight = State()
    custom_allergy = State()

class FoodStates(StatesGroup):
    correcting = State()
    waiting_for_recipe = State()

class WorkoutStates(StatesGroup):
    active = State()
    
class MyMealsStates(StatesGroup):
    waiting_for_new_grams = State()
    waiting_for_new_composition = State()

class WeightStates(StatesGroup): waiting_for_weight = State()
class ActivityStates(StatesGroup): waiting_for_activity = State()
class TreatStates(StatesGroup): waiting_for_treat = State()
class AskStates(StatesGroup): waiting_for_question = State()

NUTRITION_TIPS = [
    "💡 <b>Совет дня:</b> По «правилу тарелки» 50% обеда должны составлять овощи и зелень (клетчатка). Это здоровая микрофлора и долгая сытость!",
    "💡 <b>Совет дня:</b> Качественный белок в каждом приёме пищи защищает от резких скачков сахара и вечерних срывов на сладкое."
]

EQUIPMENT_NAMES = {
    "bodyweight": "🧘 Собственный вес (без инвентаря)",
    "bands": "🎗 Фитнес-резинки",
    "dumbbells": "🏋️ Гантели / Гиря",
    "all": "🌟 Полный набор (гантели + резинки)"
}

# =========================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И ПРОГРЕСС-БАР (ВАРИАНТ 20)
# =========================================================
def clean_html_tags(text: str) -> str:
    return re.sub(r'<(?!/?(b|i|code|s|u|a)\b)[^>]*>', '', text)

def make_progress_bar(current: int, target: int, active_char: str = "🟢", inactive_char: str = "⚪", length: int = 7) -> str:
    """Визуальная шкала прогресса из тематических эмодзи."""
    if target <= 0:
        return inactive_char * length
    
    fraction = min(max(current / target, 0.0), 1.0)
    filled = int(round(length * fraction))
    return (active_char * filled) + (inactive_char * (length - filled))

async def get_user_profile(user_id: int) -> dict | None:
    if not db: return None
    doc = await asyncio.to_thread(db.collection('users').document(str(user_id)).get)
    if not doc.exists: return None
        
    data = doc.to_dict()
    data['calories'] = int(data.get('calories') or data.get('norm') or 2000)
    data['protein'] = int(data.get('protein') or data.get('p') or 100)
    data['fat'] = int(data.get('fat') or data.get('f') or 70)
    data['carbs'] = int(data.get('carbs') or data.get('c') or 200)
    data['diet'] = data.get('diet', 'all')
    data['allergies'] = data.get('allergies', 'Нет')
    data['family_mode'] = data.get('family_mode', 'self')
    data['home_equipment'] = data.get('home_equipment', 'bodyweight')
    data['target_weight'] = float(data.get('target_weight', data.get('weight', 60.0)))
    return data

async def save_user_profile(user_id: int, data: dict):
    if db: await asyncio.to_thread(db.collection('users').document(str(user_id)).set, data, merge=True)

async def check_user_access(user_id: int) -> bool:
    return True
    user = await get_user_profile(user_id)
    if not user: return False
        
    now = now_local()
    
    def parse_date(d_val):
        if not d_val: return None
        if isinstance(d_val, str):
            try: 
                # Читаем дату
                dt = datetime.fromisoformat(d_val.replace('Z', '+00:00'))
                # 🌟 ВОТ СПАСЕНИЕ ДЛЯ КРИСТИНЫ 🌟
                # Если дата "голая" (как в базе), добавляем ей часовой пояс!
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=now.tzinfo)
                return dt
            except Exception: return None
        return None

    try:
        premium_date = parse_date(user.get("premium_until"))
        if premium_date and premium_date > now: return True
            
        trial_date = parse_date(user.get("trial_until"))
        if trial_date and trial_date > now: return True
    except Exception as e:
        logger.error(f"Ошибка сравнения дат: {e}")
        return False
        
    return False
async def send_paywall(message: Message):
    text = (
        "🔒 <b>Твой бесплатный период завершился.</b>\n\n"
        "Чтобы продолжить считать КБЖУ по фото, пользоваться рецептами и вести дневник, выбери подписку:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 месяц — 15 BYN", callback_data="buy_1_month")],
        [InlineKeyboardButton(text="3 месяца — 29 BYN 🔥 (Скидка 35%)", callback_data="buy_3_months")],
        [InlineKeyboardButton(text="6 месяцев — 49 BYN 💎 (Скидка 45%)", callback_data="buy_6_months")],
    ])
    await message.answer(text, reply_markup=kb)
@dp.callback_query(F.data.startswith("buy_"))
async def buy_subscription_handler(callback: CallbackQuery):
    await callback.message.edit_text("⏳ Генерирую безопасную ссылку для оплаты...")
    
    # Определяем тариф и цену (не доверяем юзеру, берем жестко из кода)
    tariffs = {"buy_1_month": (1, 15.0), "buy_3_months": (3, 29.0), "buy_6_months": (6, 49.0)}
    months, price = tariffs.get(callback.data, (1, 15.0))
    
    url = await create_bepaid_bill(callback.from_user.id, price, months)
    
    if url:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить подписку", url=url)]
        ])
        await callback.message.edit_text(
            f"Оформление подписки на <b>{months} мес.</b>\nК оплате: <b>{price} BYN</b>\n\n"
            "<i>После оплаты доступ активируется автоматически.</i>", 
            reply_markup=kb
        )
    else:
        await callback.message.edit_text("❌ Ошибка создания платежа. Попробуйте позже.")
# === КОНЕЦ ВСТАВКИ ===

async def set_bot_description(bot_instance: Bot):
    try: await bot_instance.set_my_description("NutriAi — твой нутрициолог в кармане.")
    except Exception: pass

async def set_bot_commands(bot_instance: Bot):
    commands = [
        BotCommand(command="today", description="📊 Дневник за сегодня"),
        BotCommand(command="treat", description="😋 Вкусняшка"),
        BotCommand(command="fridge", description="🥗 Что приготовить"),
        BotCommand(command="workout", description="🏋️ Тренировка"),
        BotCommand(command="ask", description="💬 Спросить нутрициолога"),
        BotCommand(command="weight", description="⚖️ Динамика веса"),
        BotCommand(command="profile", description="👤 Профиль"),
        BotCommand(command="plan", description="🎯 Моя норма"),
        BotCommand(command="help", description="❓ Помощь"),
    ]
    try: await bot_instance.set_my_commands(commands)
    except Exception: pass

async def get_today_meals(user_id: int) -> list:
    if not db: return []
    doc = await asyncio.to_thread(db.collection('diaries').document(f"{user_id}_{today_str()}").get)
    return doc.to_dict().get('meals', []) if doc.exists else []

async def add_meal_to_today(user_id: int, meal_data: dict):
    if not db: return
    doc_ref = db.collection('diaries').document(f"{user_id}_{today_str()}")
    doc = await asyncio.to_thread(doc_ref.get)
    meals = doc.to_dict().get('meals', []) if doc.exists else []
    meals.append(meal_data)
    await asyncio.to_thread(doc_ref.set, {'meals': meals}, merge=True)

# =========================================================
# РАСЧЁТЫ И ИИ
# =========================================================
def calculate_norm(gender: str, age: int, height: float, weight: float, goal: str, activity: str) -> dict:
    bmr = 10 * weight + 6.25 * height - 5 * age + (5 if gender == "M" else -161)
    bmr = max(bmr, 1200.0) 
    
    act_coefs = {"low": 1.2, "light": 1.375, "medium": 1.55, "high": 1.725}
    tdee = bmr * act_coefs.get(activity, 1.2)

    if goal == "loss": calories = max(tdee * 0.85, bmr * 1.05)
    elif goal == "gain": calories = tdee * 1.15
    else: calories = tdee

    calories = int(calories)
    return {
        "calories": calories,
        "protein": int((calories * 0.30) / 4),
        "fat": int((calories * 0.30) / 9),
        "carbs": int((calories * 0.40) / 4),
    }

def extract_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match: raise ValueError("AI не вернул валидный JSON")
    try: data = json.loads(match.group(0))
    except json.JSONDecodeError: raise ValueError("Ошибка чтения JSON от ИИ")
    
    try: p, f, c = max(0, float(data.get("protein") or 0)), max(0, float(data.get("fat") or 0)), max(0, float(data.get("carbs") or 0))
    except Exception: p, f, c = 0.0, 0.0, 0.0
    
    data["protein"], data["fat"], data["carbs"] = int(p), int(f), int(c)
    data["calories"] = int((p * 4) + (f * 9) + (c * 4))
    if not data.get("title") or not str(data["title"]).strip(): data["title"] = "Приём пищи"
    
    # НОВОЕ: Гарантируем, что поле ingredients всегда существует и является списком
    # (Даже если ИИ прислал старый формат без ингредиентов, бот подставит пустой список [])
    data["ingredients"] = data.get("ingredients") or []
    
    return data
def calculate_saved_dish_portion(dish: dict, new_weight_g: float) -> dict:
    # 👇 ВОТ ТУТ СЛЕВА ДОЛЖНЫ БЫТЬ ПРОБЕЛЫ (ОТСТУПЫ) 👇
    """Математически пересчитывает КБЖУ и состав блюда пропорционально новому весу."""
    original_weight = sum(ing.get('weight_g', 0) for ing in dish.get('ingredients', []))
    if original_weight <= 0:
        original_weight = 100.0 
        
    ratio = new_weight_g / original_weight
    
    new_dish = dish.copy()
    new_dish['calories'] = int(dish.get('calories', 0) * ratio)
    new_dish['protein'] = int(dish.get('protein', 0) * ratio)
    new_dish['fat'] = int(dish.get('fat', 0) * ratio)
    new_dish['carbs'] = int(dish.get('carbs', 0) * ratio)
    
    new_ingredients = []
    for ing in dish.get('ingredients', []):
        new_ing = ing.copy()
        new_ing['weight_g'] = int(ing.get('weight_g', 0) * ratio)
        new_ing['calories'] = int(ing.get('calories', 0) * ratio)
        new_ing['protein'] = int(ing.get('protein', 0) * ratio)
        new_ing['fat'] = int(ing.get('fat', 0) * ratio)
        new_ing['carbs'] = int(ing.get('carbs', 0) * ratio)
        new_ingredients.append(new_ing)
        
    new_dish['ingredients'] = new_ingredients
    return new_dish

async def ask_ai(prompt: str, image_base64: str | None = None, model: str | None = None) -> str:
    used_model = model or AI_MODEL
    sys = (
        "Ты эмпатичный нутрициолог NutriAi. Твой подход — забота и поддержка. НИКОГДА не ругай за срывы. "
        "Отвечай кратко, красиво. Используй ТОЛЬКО разрешённые HTML теги Telegram (<b>, <i>, <code>). "
        "КРИТИЧЕСКИ ВАЖНО: ЗАПРЕЩЕНО использовать таблицы Markdown (|) и заголовки Markdown (###)."
    )
    user_content = [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}", "detail": "low"}}] if image_base64 else prompt
        
    try:
        resp = await ai_client.chat.completions.create(
            model=used_model, messages=[{"role": "system", "content": sys}, {"role": "user", "content": user_content}], temperature=0.3
        )
        return resp.choices[0].message.content or ""
    except Exception as e:
        logger.error(f"🔥 Ошибка AI: {e}")
        raise

# =========================================================
# БИЛЛИНГ И КЛАВИАТУРЫ
# =========================================================
async def create_bepaid_bill(user_id: int, amount_byn: float, months: int) -> str | None:
    if not BEPAID_SHOP_ID or not BEPAID_SECRET_KEY: return None
    
    order_id = f"sub_{user_id}_{months}_{int(now_local().timestamp())}_{uuid.uuid4().hex[:6]}"
    
    payload = {
        "request": {
            "amount": int(amount_byn * 100), 
            "currency": "BYN", 
            "description": f"Подписка NutriAi на {months} мес.",
            "notification_url": "https://diet-bot-zqpn.onrender.com/webhook/bepaid",
            "tracking_id": order_id,
        }
    }
    
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post("https://checkout.bepaid.by/v2/redirect_biller/bills", json=payload, auth=aiohttp.BasicAuth(BEPAID_SHOP_ID, BEPAID_SECRET_KEY)) as r:
                if r.status in (200, 201):
                    data = await r.json()
                    url = data.get("checkout", {}).get("redirect_url")
                    
                    if url and db:
                        # Берем UTM-метки юзера, чтобы связать платеж с источником
                        user = await get_user_profile(user_id) or {}
                        
                        doc_ref = db.collection('payments').document(order_id)
                        await asyncio.to_thread(doc_ref.set, {
                            "order_id": order_id,
                            "user_id": user_id,
                            "amount": amount_byn,
                            "currency": "BYN",
                            "tariff": months,
                            "status": "pending",
                            "payment_provider": "bepaid",
                            "created_at": now_local().isoformat(),
                            "utm_source": user.get("utm_source", "organic"),
                            "utm_campaign": user.get("utm_campaign", "")
                        })
                    return url
    except Exception as e:
        logger.error(f"Ошибка создания счета bePaid: {e}")
    return None

def food_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Верно", callback_data="food_correct")],
        [InlineKeyboardButton(text="✏️ Поправить", callback_data="food_edit"), InlineKeyboardButton(text="❌ Удалить", callback_data="food_delete")],
    ])

def result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 В дневник", callback_data="meal_save"), InlineKeyboardButton(text="❤️ Запомнить", callback_data="food_remember")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="food_delete")],
    ])

def activity_result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏃 Сохранить активность", callback_data="activity_save")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="food_delete")]
    ])

def get_workout_location_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🏠 Дома", callback_data="workout_loc_home"),
            InlineKeyboardButton(text="🏋️ В зале", callback_data="workout_loc_gym")
        ],
        [InlineKeyboardButton(text="👣 Своя активность / Шаги", callback_data="enter_custom_activity")]
    ])

# =========================================================
# ВЫВОД ДНЕВНИКА (ТОЧНОЕ ВРЕМЯ UTC+3 + ВАРИАНТ 20)
# =========================================================
async def send_today(message: Message, user_id: int | None = None):
    target_id = user_id or message.from_user.id
    user = await get_user_profile(target_id)
    if not user: return await message.answer("Сначала нажмите /start.")
        
    meals = await get_today_meals(target_id)
    doc_id = f"{target_id}_{today_str()}"
    doc_data = {}
    if db:
        doc = await asyncio.to_thread(db.collection('diaries').document(doc_id).get)
        if doc.exists: doc_data = doc.to_dict()
            
    water_ml, burned_kcal = doc_data.get('water', 0), doc_data.get('burned_kcal', 0)

    total_kcal = sum(m.get('calories', 0) for m in meals)
    total_p = sum(m.get('protein', 0) for m in meals)
    total_f = sum(m.get('fat', 0) for m in meals)
    total_c = sum(m.get('carbs', 0) for m in meals)
    
    norm_kcal = user.get('calories', 2000)
    norm_p = user.get('protein', 100)
    norm_f = user.get('fat', 70)
    norm_c = user.get('carbs', 200)
    
    if not meals:
        meals_text = "<i>Пока пусто. Пришли фото еды — я всё посчитаю 📸</i>\n\n"
    else:
        meals_text = ""
        for meal in meals:
            title_clean = clean_html_tags(str(meal.get('title', 'Блюдо')))
            time_str = "🍽"
            try: 
                if 'created_at' in meal:
                    dt = datetime.fromisoformat(meal['created_at'])
                    time_str = f"⏰ {dt.strftime('%H:%M')}"
            except Exception: pass
            meals_text += f"{time_str} | <b>{title_clean}</b>\n   {meal.get('calories', 0)} ккал • Б:{meal.get('protein', 0)} Ж:{meal.get('fat', 0)} У:{meal.get('carbs', 0)}\n\n"
        
    net_kcal = total_kcal - burned_kcal
    if net_kcal > norm_kcal: 
        status_text = f"⚠️ <b>Превышение:</b> +{net_kcal - norm_kcal} ккал\n<i>Один день профицита не страшен! Завтра возвращаемся к норме 💪</i>"
    else: 
        status_text = f"✅ <b>Остаток на день:</b> {norm_kcal - net_kcal} ккал"

    burned_str = f" <i>(-{burned_kcal} ккал активностью)</i>" if burned_kcal > 0 else ""

    text = (
        f"📋 <b>ТВОЙ ДНЕВНИК</b>\n\n{meals_text}━━━━━━━━━━━━━━━━━━━━\n"
        f"🔥 <b>Энергия:</b> {total_kcal} из {norm_kcal} ккал{burned_str}\n"
        f"{make_progress_bar(total_kcal, norm_kcal, '⚡', '⚪')}\n\n"
        f"🥩 <b>Белки:</b> {total_p} / {norm_p} г\n"
        f"{make_progress_bar(total_p, norm_p, '🥩', '⚪')}\n\n"
        f"🥑 <b>Жиры:</b> {total_f} / {norm_f} г\n"
        f"{make_progress_bar(total_f, norm_f, '🥑', '⚪')}\n\n"
        f"🍚 <b>Углеводы:</b> {total_c} / {norm_c} г\n"
        f"{make_progress_bar(total_c, norm_c, '🍚', '⚪')}\n\n"
        f"💧 <b>Вода:</b> {water_ml} мл\n━━━━━━━━━━━━━━━━━━━━\n{status_text}"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💧 +250 мл воды", callback_data="add_water_250"), InlineKeyboardButton(text="🗑 Удалить последнее", callback_data="delete_last_meal")]
    ])
    await message.answer(text, reply_markup=kb)

# =========================================================
# ГЛАВНЫЕ КОМАНДЫ
# =========================================================
@dp.message(Command("admin_broadcast"))
async def admin_broadcast_handler(message: Message):
    text = message.text.replace("/admin_broadcast", "").strip()
    if not text: return await message.answer("Введите текст: /admin_broadcast Ваш текст")
    if not db: return await message.answer("Ошибка БД")
        
    users_docs = await asyncio.to_thread(db.collection('users').get)
    count = 0
    await message.answer("⏳ Начинаю рассылку...")
    
    for doc in users_docs:
        try:
            await bot.send_message(chat_id=int(doc.id), text=text)
            count += 1
            await asyncio.sleep(0.05)
        except Exception: pass
    await message.answer(f"✅ Рассылка завершена! Отправлено: {count} чел.")

@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    await state.clear()
    
    # Безопасно достаем текст и параметры (например: /start fb_ad_campaign1)
    text = getattr(message, 'text', None) or ""
    args = text.split(maxsplit=1)
    start_param = args[1] if len(args) > 1 else ""
    
    user = await get_user_profile(message.from_user.id)
    
    # Если юзер НОВЫЙ, сохраняем параметры в стейт для онбординга
    if not user:
        # Примитивный парсинг: если параметры переданы через подчеркивание (fb_cpc_promo)
        parts = start_param.split('_')
        await state.update_data(
            start_parameter=start_param,
            utm_source=parts[0] if len(parts) > 0 else "organic",
            utm_medium=parts[1] if len(parts) > 1 else "",
            utm_campaign=parts[2] if len(parts) > 2 else ""
        )
    
    if user: 
        return await message.answer("С возвращением! Пришли фото еды 📸", reply_markup=main_menu)
        
    welcome_text = (
        "Привет! Это «NutriAi» — твой нутрициолог в телефоне 🥗\n\n"
        "📸 считаю КБЖУ по фото еды\n📊 веду дневник питания\n🏋️ подбираю тренировки\n🧊 собираю меню\n\n"
        "🎁 <b>14 дней бесплатно!</b>\n\nДавай рассчитаем твою норму:"
    )
    await message.answer(welcome_text, reply_markup=main_menu)
    await message.answer("Жми кнопку ниже 👇", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚀 Начать", callback_data="start_onb")]]))

@dp.message(F.text == "📊 Сегодня")
@dp.message(Command("today"))
async def today_handler(message: Message, state: FSMContext):
    await state.clear()
    if not await check_user_access(message.from_user.id): return await send_paywall(message)
    await send_today(message)
    
@dp.callback_query(F.data == "analyze_today")
async def analyze_today_handler(callback: CallbackQuery):
    user = await get_user_profile(callback.from_user.id)
    meals = await get_today_meals(callback.from_user.id)
    
    if not meals:
        return await callback.answer("Твой дневник пока пуст! Запиши хотя бы один прием пищи.", show_alert=True)
        
    wait_msg = await callback.message.edit_text("⏳ <i>Изучаю твой рацион и готовлю рекомендации...</i>")
    
    total_kcal = sum(m.get('calories', 0) for m in meals)
    total_p = sum(m.get('protein', 0) for m in meals)
    total_f = sum(m.get('fat', 0) for m in meals)
    total_c = sum(m.get('carbs', 0) for m in meals)
    
    prompt = (
        f"Пользователь: цель {user.get('goal')}, вес {user.get('weight')} кг.\n"
        f"Норма: {user.get('calories')} ккал (Б:{user.get('protein')} Ж:{user.get('fat')} У:{user.get('carbs')}).\n"
        f"Съедено: {total_kcal} ккал (Б:{total_p} Ж:{total_f} У:{total_c}).\n\n"
        "Выступи в роли заботливого наставника-нутрициолога. Человек НОВИЧОК и не знает, как правильно питаться для своей цели.\n"
        "Сделай ПОЛНЫЙ анализ его дня по всем показателям (калории, белки, жиры, углеводы).\n"
        "ГЛАВНОЕ: \n"
        "1. Объясни простым языком, ЧТО именно пошло не так (если есть сильный перебор или недобор по ЛЮБОМУ из макронутриентов) и ПОЧЕМУ это важно для организма. "
        "2. ДАЙ КОНКРЕТНЫЙ СОВЕТ: какие 2-3 простых продукта съесть сегодня на ужин или добавить в рацион завтра, чтобы выровнять баланс.\n"
        "Пиши очень по-доброму, поддерживающе, мотивируй человека не сдаваться и не используй сложных медицинских терминов."
    )
    
    try:
        analysis = await ask_ai(prompt=prompt)
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Закрыть совет", callback_data="delete_msg")]
        ])
        
        await wait_msg.edit_text(f"📊 <b>Анализ дня от нутрициолога:</b>\n\n{analysis}", reply_markup=kb)
    except Exception as e:
        await wait_msg.edit_text("Не удалось проанализировать день. Попробуй позже.")

@dp.callback_query(F.data == "delete_msg")
async def delete_msg_handler(callback: CallbackQuery):
    await callback.message.delete()

@dp.message(F.text == "👤 Профиль")
@dp.message(Command("profile"))
async def profile_handler(message: Message, state: FSMContext):
    await state.clear()
    user = await get_user_profile(message.from_user.id)
    if not user: return await message.answer("Сначала нажми /start.")

    diet_titles = {"all": "🍏 Ем всё", "nutri": "🥦 Нутри-подход", "veg": "🥕 Вегетарианец", "allergy": "🚫 Без лактозы и глютена"}
    fam_str = "👨‍👩‍👧‍👦 Готовлю для семьи (без сахара)" if user.get('family_mode') == 'kids' else "🧍 Только для себя"
    equip_str = EQUIPMENT_NAMES.get(user.get("home_equipment", "bodyweight"), "Свой вес")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❤️ Моя база любимых блюд", callback_data="show_favorite_foods")],
        [InlineKeyboardButton(text="👨‍👩‍👧‍👦 Режим семьи: изменить", callback_data="toggle_family_mode")],
        [InlineKeyboardButton(text="🛡 Изменить аллергии", callback_data="profile_edit_allergies")],
        [InlineKeyboardButton(text="⚙️ Пересчитать норму (Опрос)", callback_data="profile_recount_norm")]
    ])

    await message.answer(
        "👤 <b>ТВОЙ ПРОФИЛЬ</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 Целевой вес: <b>{user.get('target_weight', '—')} кг</b>\n"
        f"🥗 Питание: {diet_titles.get(user.get('diet'), 'Обычное')}\n"
        f"📦 Домашний инвентарь: <b>{equip_str}</b>\n"
        f"👶 Режим: {fam_str}\n🛡 Аллергии: {user.get('allergies', 'Нет')}",
        reply_markup=kb
    )
@dp.callback_query(F.data == "show_favorite_foods")
async def show_favorite_foods_handler(callback: CallbackQuery):
    user = await get_user_profile(callback.from_user.id)
    favs = user.get("favorite_foods", []) if user else []
    
    if not favs:
        return await callback.answer("Твоя база блюд пока пуста. Нажимай '❤️ Запомнить' при сохранении еды!", show_alert=True)
    
    text = "❤️ <b>ТВОЯ БАЗА ЛЮБИМЫХ БЛЮД</b>\n<i>Я подглядываю сюда, когда ты пишешь мне еду текстом или голосом.</i>\n\n"
    for i, f in enumerate(reversed(favs), 1):
        text += f"{i}. <b>{f.get('title', 'Блюдо')}</b> — {f.get('calories', 0)} ккал (Б:{f.get('protein', 0)} Ж:{f.get('fat', 0)} У:{f.get('carbs', 0)})\n"
        if i >= 15: # Показываем только последние 15, чтобы сообщение не было слишком длинным
            break
            
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Назад в профиль", callback_data="back_to_profile")]
    ]))

@dp.callback_query(F.data == "back_to_profile")
async def back_to_profile_handler(callback: CallbackQuery, state: FSMContext):
    # Возвращаем пользователя обратно в профиль
    callback.message.from_user = callback.from_user
    await profile_handler(callback.message, state)
    # Удаляем старое сообщение с базой, чтобы не мусорить
    try: await callback.message.delete()
    except Exception: pass

@dp.callback_query(F.data == "toggle_family_mode")
async def toggle_family_mode_handler(callback: CallbackQuery):
    user = await get_user_profile(callback.from_user.id)
    if not user: return await callback.answer("Профиль не найден")
    new_mode = "kids" if user.get("family_mode", "self") == "self" else "self"
    await save_user_profile(callback.from_user.id, {"family_mode": new_mode})
    mode_text = "ВКЛЮЧЕН 👨‍👩‍👧‍👦\n\nТеперь в рецептах и советах я буду убирать скрытый сахар!" if new_mode == "kids" else "ВЫКЛЮЧЕН 🧍\n\nМеню и советы теперь рассчитываются только для тебя."
    await callback.message.edit_text(f"✅ <b>Режим семьи {mode_text}</b>")
    await callback.answer()

@dp.message(F.text == "🎯 Моя норма")
@dp.message(Command("plan"))
async def plan_handler(message: Message, state: FSMContext):
    await state.clear()
    user = await get_user_profile(message.from_user.id)
    if not user: return await message.answer("Сначала нажми /start.")
    
    weight, target = float(user.get("weight", 60)), float(user.get("target_weight", 60))
    goal = user.get("goal", "maintain")
    
    date_str, goal_text = "", "⚖️ Поддержание веса"
    if goal == "loss" and weight > target:
        goal_text = "📉 Снижение веса"
        weeks = (weight - target) / 0.6
        date_str = f"🗓 <b>Прогноз цели:</b> к {(now_local() + timedelta(weeks=weeks)).strftime('%d.%m.%Y')} (~{int(weeks)} нед.)\n"
    elif goal == "gain" and target > weight:
        goal_text = "📈 Набор массы"
        weeks = (target - weight) / 0.4
        date_str = f"🗓 <b>Прогноз цели:</b> к {(now_local() + timedelta(weeks=weeks)).strftime('%d.%m.%Y')} (~{int(weeks)} нед.)\n"

    # Добавляем новую крутую кнопку!
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧠 Почему именно такие цифры?", callback_data="explain_norm")]
    ])

    await message.answer(
        "🎯 <b>ТВОЯ ДНЕВНАЯ НОРМА</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"Текущая цель: <b>{goal_text}</b>\n\n"
        f"🔥 Калории: <b>{user.get('calories', 2000)} ккал</b>\n"
        f"🥩 Белки: {user.get('protein', 100)} г\n🥑 Жиры: {user.get('fat', 70)} г\n🍚 Углеводы: {user.get('carbs', 200)} г\n\n"
        f"{date_str}🛡 Ограничения: {user.get('allergies', 'Нет')}\n━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Именно этот баланс позволит тебе достичь результата комфортно и без срывов! ✨</i>",
        reply_markup=kb
    )

# Обработчик нажатия на кнопку с объяснением
@dp.callback_query(F.data == "explain_norm")
async def explain_norm_handler(callback: CallbackQuery):
    user = await get_user_profile(callback.from_user.id)
    if not user: return await callback.answer("Профиль не найден")

    wait_msg = await callback.message.edit_text("⏳ <i>Готовлю подробный разбор твоей нормы...</i>")

    gender_text = "мужчины" if user.get("gender") == "M" else "женщины"
    goal = user.get("goal", "loss")

    if goal == "gain":
        goal_context = (
            "Цель пользователя: НАБОР МАССЫ. "
            "Мягко объясни, почему набор качественного веса (мышц, а не просто жира) требует времени и плавного профицита калорий. "
            "Расскажи, почему мы не делаем огромный профицит (чтобы не заплыть жиром) и почему важно двигаться к цели постепенно."
        )
    else:
        goal_context = (
            "Цель пользователя: ПОХУДЕНИЕ или УДЕРЖАНИЕ. "
            "Мягко объясни механику здорового похудения: почему плавный и комфортный дефицит калорий работает лучше, чем жесткие голодовки. "
            "Расскажи, как правильный темп защищает от срывов, эффекта йо-йо (когда вес возвращается) и бережет здоровье."
        )

    prompt = (
        f"Выступи в роли заботливого профи-нутрициолога. Пользователь ({gender_text}) "
        f"спрашивает, почему его норма БЖУ именно такая ({user.get('protein')}г белков, {user.get('fat')}г жиров, {user.get('carbs')}г углеводов).\n\n"
        "Твоя задача — понятно и логично объяснить:\n"
        "1. Зачем нужно именно столько белка (сохранение мышц, сытость).\n"
        "2. Зачем нужны жиры (гормональный фон, здоровье кожи/волос, почему их нельзя урезать).\n"
        "3. Зачем нужны углеводы (энергия, работа мозга).\n"
        f"4. {goal_context}\n\n"
        "Пиши структурно, приветливо, как наставник. Используй HTML теги (<b>, <i>). ЗАПРЕЩЕНО использовать Markdown (* или #)."
    )

    try:
        explanation = await ask_ai(prompt=prompt, model=AI_MODEL)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Закрыть разбор", callback_data="delete_msg")]
        ])
        await wait_msg.edit_text(f"🧠 <b>Разбор твоей нормы от нутрициолога:</b>\n\n{explanation}", reply_markup=kb)
    except Exception as e:
        await wait_msg.edit_text("Не удалось сгенерировать ответ. Попробуй позже.")

@dp.message(F.text == "😋 Вкусняшка")
@dp.message(Command("treat"))
async def treat_button_handler(message: Message, state: FSMContext):
    await state.clear()
    if not await check_user_access(message.from_user.id): return await send_paywall(message)
    await state.set_state(TreatStates.waiting_for_treat)
    await message.answer("😋 <b>Съел(а) что-то вкусное?</b>\nНапиши, что это было.\n<i>Я рассчитаю КБЖУ без чувства вины — баловать себя полезно для души! ✨</i>")

@dp.message(TreatStates.waiting_for_treat)
async def process_treat_input(message: Message, state: FSMContext):
    await state.set_state(None) 
    wait_msg = await message.answer("⏳ Считаю КБЖУ вкусняшки...")
    user = await get_user_profile(message.from_user.id)
    
    prompt = (
        f"Пользователь съел лакомство: \"{message.text}\". Цель: '{user.get('goal', 'loss')}'.\nРассчитай примерный КБЖУ.\n"
        "ВАЖНО: Ни в коем случае не ругай пользователя за сахар! Поддержи правило 80/20.\nВерни строго JSON:\n"
        '{"title": "название", "calories": 0, "protein": 0, "fat": 0, "carbs": 0, "comment": "Теплая фраза поддержки (1 предл.)"}'
    )
    try:
        food_data = extract_json(await ask_ai(prompt=prompt, model=AI_MODEL))
        title = clean_html_tags(str(food_data.get("title", "Вкусняшка")))
        if not title.startswith("😋"): food_data["title"] = f"😋 {title}"
        await state.update_data(calculated_food=food_data)
        
        text = (
            f"🍽 <b>{food_data['title']}</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            f"🔥 Калории: <b>{food_data.get('calories', 0)} ккал</b>\n"
            f"🥩 Белки: {food_data.get('protein', 0)} г | 🥑 Жиры: {food_data.get('fat', 0)} г | 🍚 Угл: {food_data.get('carbs', 0)} г\n\n"
            f"💬 <i>{clean_html_tags(str(food_data.get('comment', 'Приятного аппетита!')))}</i>\n\nВнести эту вкусняшку в дневник?"
        )
        await wait_msg.edit_text(text, reply_markup=result_keyboard())
    except Exception: await wait_msg.edit_text("Не удалось рассчитать лакомство. Попробуй описать точнее.")

@dp.message(F.text == "🥗 Что приготовить")
@dp.message(Command("fridge"))
async def fridge_handler(message: Message, state: FSMContext):
    await state.clear()
    if not await check_user_access(message.from_user.id): return await send_paywall(message)
    await state.set_state(FoodStates.waiting_for_recipe)
    await message.answer("Напиши продукты через запятую, и я соберу идеальный рецепт:")
@dp.message(F.text == "🍽 Мои блюда")
async def my_meals_list_handler(message: Message):
    if not await check_user_access(message.from_user.id): return await send_paywall(message)
    if not db: return await message.answer("Ошибка подключения к базе данных.")
    
    # Достаем блюда из новой коллекции пользователя
    dishes_ref = db.collection('users').document(str(message.from_user.id)).collection('saved_dishes')
    docs = await asyncio.to_thread(dishes_ref.limit(30).get)
    
    if not docs:
        return await message.answer("У тебя пока нет сохранённых блюд.\n\nНажимай кнопку <b>«❤️ Запомнить»</b> при добавлении еды, и она появится здесь!")
        
    kb_buttons = []
    for doc in docs:
        d = doc.to_dict()
        kb_buttons.append([InlineKeyboardButton(text=f"🍽 {d.get('title', 'Блюдо')}", callback_data=f"my_meal_view_{doc.id}")])
        
    await message.answer("🍽 <b>ТВОИ СОХРАНЁННЫЕ БЛЮДА</b>\nВыбери блюдо из списка:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons))
@dp.callback_query(F.data.startswith("my_meal_view_"))
async def my_meal_view_handler(callback: CallbackQuery, state: FSMContext):
    dish_id = callback.data.replace("my_meal_view_", "")
    dish_ref = db.collection('users').document(str(callback.from_user.id)).collection('saved_dishes').document(dish_id)
    doc = await asyncio.to_thread(dish_ref.get)
    
    if not doc.exists:
        return await callback.answer("Блюдо не найдено", show_alert=True)
        
    dish = doc.to_dict()
    dish['id'] = dish_id
    await state.update_data(current_viewed_dish=dish)  # Сохраняем эталон в память
    
    text = f"🍽 <b>{dish.get('title')}</b>\n━━━━━━━━━━━━━━━━━━━━\n"
    text += f"🔥 {dish.get('calories')} ккал | Б: {dish.get('protein')}г | Ж: {dish.get('fat')}г | У: {dish.get('carbs')}г\n\n"
    
    ingredients = dish.get('ingredients', [])
    if ingredients:
        text += "<b>Ингредиенты:</b>\n"
        for ing in ingredients:
            text += f"• {ing.get('name', 'Ингредиент')} — {ing.get('weight_g', 0)} г\n"
            
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Съесть (в дневник)", callback_data=f"my_meal_eat_{dish_id}")],
        [InlineKeyboardButton(text="⚖️ Изменить граммовку", callback_data=f"my_meal_edit_{dish_id}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"my_meal_del_{dish_id}")],
        [InlineKeyboardButton(text="↩️ К списку блюд", callback_data="my_meals_back")]
    ])
    
    await callback.message.edit_text(text, reply_markup=kb)

@dp.callback_query(F.data == "my_meals_back")
async def my_meals_back_handler(callback: CallbackQuery):
    callback.message.from_user = callback.from_user
    await my_meals_list_handler(callback.message)
    try: await callback.message.delete()
    except Exception: pass

@dp.callback_query(F.data.startswith("my_meal_eat_"))
async def my_meal_eat_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    dish = data.get("current_viewed_dish")
    if not dish: return await callback.answer("Ошибка: блюдо не найдено в памяти")
    
    # Сохраняем в дневник через старую проверенную функцию (никакого дублирования!)
    await add_meal_to_today(callback.from_user.id, {
        "title": dish.get("title", "Еда"), 
        "calories": dish.get("calories", 0), 
        "protein": dish.get("protein", 0), 
        "fat": dish.get("fat", 0), 
        "carbs": dish.get("carbs", 0), 
        "created_at": now_local().isoformat()
    })
    await callback.message.edit_text(f"✅ <b>{dish.get('title')}</b> добавлено в дневник!")
    await send_today(callback.message, user_id=callback.from_user.id)

@dp.callback_query(F.data.startswith("my_meal_edit_"))
async def my_meal_edit_handler(callback: CallbackQuery, state: FSMContext):
    await state.set_state(MyMealsStates.waiting_for_new_grams)
    await callback.message.edit_text("⚖️ <b>Введи новый общий вес блюда (в граммах):</b>\nНапример: <i>250</i>\n\nЯ математически пересчитаю КБЖУ и состав.")

@dp.message(MyMealsStates.waiting_for_new_grams)
async def my_meal_new_grams_message(message: Message, state: FSMContext):
    data = await state.get_data()
    dish = data.get("current_viewed_dish")
    if not dish: 
        await state.set_state(None)
        return await message.answer("Ошибка: блюдо не найдено")
        
    nums = re.findall(r"\d+(?:[.,]\d+)?", message.text)
    if not nums:
        return await message.answer("Пожалуйста, напиши вес числом (например: 250).")
        
    new_weight = float(nums[0].replace(",", "."))
    await state.set_state(None)
    
    # 🌟 ТА САМАЯ МАТЕМАТИКА 🌟
    new_dish = calculate_saved_dish_portion(dish, new_weight)
    await state.update_data(current_viewed_dish=new_dish) # Запоминаем для кнопки "Съесть"
    
    text = f"⚖️ <b>ПЕРЕСЧИТАНО НА {int(new_weight)} г</b>\n━━━━━━━━━━━━━━━━━━━━\n"
    text += f"🍽 <b>{new_dish.get('title')}</b>\n"
    text += f"🔥 {new_dish.get('calories')} ккал | Б: {new_dish.get('protein')}г | Ж: {new_dish.get('fat')}г | У: {new_dish.get('carbs')}г\n\n"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Съесть новую порцию", callback_data=f"my_meal_eat_{dish.get('id')}")],
        [InlineKeyboardButton(text="↩️ Отмена", callback_data=f"my_meal_view_{dish.get('id')}")]
    ])
    await message.answer(text, reply_markup=kb)

@dp.callback_query(F.data.startswith("my_meal_del_"))
async def my_meal_delete_handler(callback: CallbackQuery):
    dish_id = callback.data.replace("my_meal_del_", "")
    if db:
        dish_ref = db.collection('users').document(str(callback.from_user.id)).collection('saved_dishes').document(dish_id)
        await asyncio.to_thread(dish_ref.delete)
        
    await callback.answer("🗑 Блюдо удалено", show_alert=True)
    # Возвращаемся к списку
    callback.message.from_user = callback.from_user
    await my_meals_list_handler(callback.message)
    try: await callback.message.delete()
    except Exception: pass

# =========================================================
# ТРЕНИРОВКИ (ОПЦИЯ А + ВЫБОР ИНВЕНТАРЯ)
# =========================================================
@dp.message(F.text == "🏋️ Тренировка")
@dp.message(Command("workout"))
async def workout_menu_handler(message: Message, state: FSMContext):
    await state.clear()
    if not await check_user_access(message.from_user.id): return await send_paywall(message)
    text = "🏋️ <b>ТРЕНИРОВКИ И АКТИВНОСТЬ</b>\n\nГде ты планируешь тренироваться сегодня?"
    await message.answer(text, reply_markup=get_workout_location_kb())

@dp.callback_query(F.data == "workout_menu_back")
async def workout_back_callback(callback: CallbackQuery):
    text = "🏋️ <b>ТРЕНИРОВКИ И АКТИВНОСТЬ</b>\n\nГде ты планируешь тренироваться сегодня?"
    await callback.message.edit_text(text, reply_markup=get_workout_location_kb())

@dp.callback_query(F.data == "choose_equipment_menu")
async def choose_equipment_menu_callback(callback: CallbackQuery):
    text = "📦 <b>Какой инвентарь у тебя есть дома?</b>\nВыбери подходящий вариант:"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧘 Только коврик (без инвентаря)", callback_data="set_equip_bodyweight")],
        [InlineKeyboardButton(text="🎗 Фитнес-резинки", callback_data="set_equip_bands")],
        [InlineKeyboardButton(text="🏋️ Гантели / Гиря", callback_data="set_equip_dumbbells")],
        [InlineKeyboardButton(text="🌟 Полный набор (гантели + резинки)", callback_data="set_equip_all")],
        [InlineKeyboardButton(text="↩️ Назад к тренировкам", callback_data="workout_loc_home")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)

@dp.callback_query(F.data.startswith("set_equip_"))
async def set_equipment_callback(callback: CallbackQuery):
    equip = callback.data.replace("set_equip_", "")
    user_id = callback.from_user.id
    if db:
        await asyncio.to_thread(db.collection('users').document(str(user_id)).set, {"home_equipment": equip}, merge=True)
    await callback.answer(f"Инвентарь сохранён: {EQUIPMENT_NAMES.get(equip, 'Свой вес')}")
    callback.data = "workout_loc_home"
    await workout_location_callback(callback)

@dp.callback_query(F.data.startswith("workout_loc_"))
async def workout_location_callback(callback: CallbackQuery):
    loc = callback.data.replace("workout_loc_", "")
    user = await get_user_profile(callback.from_user.id)
    gender = user.get("gender", "F") if user else "F"
    user_equip = user.get("home_equipment", "bodyweight") if user else "bodyweight"
    
    buttons = []
    if loc == "home":
        equip_title = EQUIPMENT_NAMES.get(user_equip, "🧘 Собственный вес")
        loc_header = f"🏠 <b>Домашняя тренировка</b>\n📦 Твой инвентарь: <b>{equip_title}</b>\n\nВыбери фокус-зону занятия:"
        if gender == "F":
            buttons = [
                [InlineKeyboardButton(text="🍑 Ягодицы и бёдра", callback_data="gen_workout_home_glutes")],
                [InlineKeyboardButton(text="🧘‍♀️ Здоровая спина и осанка", callback_data="gen_workout_home_back")],
                [InlineKeyboardButton(text="👙 Плоский живот и талия", callback_data="gen_workout_home_abs")],
                [InlineKeyboardButton(text="🔥 Экспресс-жиросжигание (15 мин)", callback_data="gen_workout_home_full")]
            ]
        else:
            buttons = [
                [InlineKeyboardButton(text="💪 Отжимания, грудь и трицепс", callback_data="gen_workout_home_chest")],
                [InlineKeyboardButton(text="🧱 Рельефный пресс и кор", callback_data="gen_workout_home_abs")],
                [InlineKeyboardButton(text="🦵 Взрывные ноги и выносливость", callback_data="gen_workout_home_legs")],
                [InlineKeyboardButton(text="🔥 Фулбоди комплекс", callback_data="gen_workout_home_full")]
            ]
        buttons.append([InlineKeyboardButton(text="⚙️ Изменить инвентарь дома", callback_data="choose_equipment_menu")])
    else:
        loc_header = "🏋️ <b>Тренировка в тренажёрном зале</b>\n\nВыбери фокус-зону занятия:"
        if gender == "F":
            buttons = [
                [InlineKeyboardButton(text="🍑 Ягодицы и ноги (тренажёры)", callback_data="gen_workout_gym_glutes")],
                [InlineKeyboardButton(text="✨ Спина, плечи и осанка", callback_data="gen_workout_gym_back")],
                [InlineKeyboardButton(text="👙 Пресс и функционал", callback_data="gen_workout_gym_abs")],
                [InlineKeyboardButton(text="🏋️ Рельефное тело (Фулбоди)", callback_data="gen_workout_gym_full")]
            ]
        else:
            buttons = [
                [InlineKeyboardButton(text="🛡 Широкая спина и бицепс", callback_data="gen_workout_gym_back")],
                [InlineKeyboardButton(text="💪 Мощная грудь и трицепс", callback_data="gen_workout_gym_chest")],
                [InlineKeyboardButton(text="🦵 Мощные ноги и плечи", callback_data="gen_workout_gym_legs")],
                [InlineKeyboardButton(text="🏋️ Силовая база (Всё тело)", callback_data="gen_workout_gym_full")]
            ]
            
    buttons.append([InlineKeyboardButton(text="↩️ Назад к выбору места", callback_data="workout_menu_back")])
    await callback.message.edit_text(loc_header, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data == "enter_custom_activity")
async def enter_activity_callback(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ActivityStates.waiting_for_activity)
    await callback.message.edit_text("👣 <b>Введи свою активность:</b>\nНапример: <i>«Прошла 12 000 шагов»</i> или <i>«Плавание 45 минут»</i>.")

# Добавляем стейт в начало файла (если еще не добавляла)
class WorkoutStates(StatesGroup):
    active = State()

# === ЭТОТ БЛОК ВСТАВЛЯЕМ ВМЕСТО СТАРОЙ generate_workout_callback ===

# 1. Когда выбрали зону, спрашиваем время!
@dp.callback_query(F.data.startswith("gen_workout_"))
async def ask_workout_time_callback(callback: CallbackQuery):
    # Достаем, что выбрал пользователь (например, home и glutes)
    p = callback.data.replace("gen_workout_", "").split("_")
    loc_type, focus_type = p[0], p[1]
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡️ 15 минут", callback_data=f"start_w_{loc_type}_{focus_type}_15")],
        [InlineKeyboardButton(text="⏱ 30 минут", callback_data=f"start_w_{loc_type}_{focus_type}_30")],
        [InlineKeyboardButton(text="🔥 45 минут", callback_data=f"start_w_{loc_type}_{focus_type}_45")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data=f"workout_loc_{loc_type}")]
    ])
    await callback.message.edit_text("⏱ <b>Сколько времени у тебя есть на эту тренировку?</b>", reply_markup=kb)

# 2. Генерируем пошаговую тренировку
@dp.callback_query(F.data.startswith("start_w_"))
async def generate_step_workout(callback: CallbackQuery, state: FSMContext):
    p = callback.data.replace("start_w_", "").split("_")
    loc_type, focus_type, minutes = p[0], p[1], p[2]
    
    user = await get_user_profile(callback.from_user.id)
    gender_str = "мужчины" if user.get("gender") == "M" else "девушки"
    user_goal = user.get("goal", "loss")
    user_equip = user.get("home_equipment", "bodyweight")
    
    equip_desc_map = {
        "bodyweight": "собственный вес тела и коврик (БЕЗ инвентаря)",
        "bands": "фитнес-резинки (эспандеры) и коврик",
        "dumbbells": "гантели (или гири) и коврик",
        "all": "гантели, фитнес-резинки и коврик"
    }
    
    if loc_type == "home":
        equip_text = equip_desc_map.get(user_equip, "собственный вес")
        loc_desc = f"дома, инвентарь: {equip_text}"
    else:
        loc_desc = "в тренажёрном зале (с использованием тренажёров и свободных весов)"
    
    focus_map = {
        "glutes": "ягодицы и ноги (акцент на форму)",
        "back": "спина и осанка (укрепление корсета)",
        "abs": "пресс, кор и талия",
        "chest": "грудь и руки",
        "legs": "ноги и выносливость",
        "full": "всё тело (комплексная тренировка)"
    }
    focus_desc = focus_map.get(focus_type, "всё тело")
    
    wait_msg = await callback.message.edit_text(f"⏳ <i>Составляю идеальный план на {minutes} мин: {focus_desc}...</i>")
    
    prompt = (
        f"Ты топ-фитнес тренер. Составь эффективную тренировку {loc_desc} для {gender_str}. "
        f"Цель клиента: '{user_goal}'. ФОКУС: {focus_desc}.\n"
        f"Продолжительность: ровно {minutes} минут.\n"
        "ОЧЕНЬ ВАЖНО: Раздели КАЖДОЕ упражнение (включая разминку и заминку) символами ||| \n\n"
        "Формат каждой карточки:\n"
        "Название (сколько раз/секунд делать)\n"
        "Подробная техника (как выполнять, как дышать, куда смотреть). Пиши бодро и с эмодзи.\n\n"
        "Никакого лишнего текста, только карточки упражнений, разделенные |||"
    )
    
    try:
        res = await ask_ai(prompt=prompt) 
        exercises = [clean_html_tags(e.strip()) for e in res.split("|||") if len(e.strip()) > 10]
        
        if not exercises:
            return await wait_msg.edit_text("Ой, что-то пошло не так. Попробуй еще раз!")

        await state.set_state(WorkoutStates.active)
        await state.update_data(exercises=exercises, current_index=0, total_kcal=int(minutes)*6)
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Выполнил(а)", callback_data="workout_next")],
            [InlineKeyboardButton(text="❌ Закрыть", callback_data="workout_stop")]
        ])
        
        await wait_msg.edit_text(f"🔥 <b>Тренировка: {focus_map.get(focus_type, 'Всё тело')} ({minutes} мин)</b>\n\nШаг 1 из {len(exercises)}:\n\n{exercises[0]}", reply_markup=kb)
    except Exception as e:
        await wait_msg.edit_text("Ошибка генерации. Попробуй позже.")

# === ЭТОТ БЛОК ВСТАВЛЯЕМ ВМЕСТО СТАРОЙ done_workout_callback ===

@dp.callback_query(F.data == "workout_next", WorkoutStates.active)
async def next_exercise_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    exercises = data.get("exercises", [])
    current_index = data.get("current_index", 0) + 1
    
    if current_index >= len(exercises):
        kcal = data.get("total_kcal", 150)
        await state.clear()
        
        # Записываем в дневник и профиль
        doc_id = f"{callback.from_user.id}_{today_str()}"
        if db:
            await save_user_profile(callback.from_user.id, {"favorite_workout_name": "Недавняя Тренировка"})
            doc_ref = db.collection('diaries').document(doc_id)
            doc = await asyncio.to_thread(doc_ref.get)
            await asyncio.to_thread(doc_ref.set, {'burned_kcal': (doc.to_dict().get('burned_kcal', 0) if doc.exists else 0) + kcal}, merge=True)
            
        await callback.message.edit_text(f"🏆 <b>Тренировка завершена!</b>\nТы супер! Сожжено примерно {kcal} ккал (зачтено в дневник).")
        return await send_today(callback.message, user_id=callback.from_user.id)
    
    await state.update_data(current_index=current_index)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Выполнил(а)", callback_data="workout_next")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="workout_stop")]
    ])
    await callback.message.edit_text(f"Шаг {current_index + 1} из {len(exercises)}:\n\n{exercises[current_index]}", reply_markup=kb)

@dp.callback_query(F.data == "workout_stop", WorkoutStates.active)
async def stop_exercise_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🛑 Тренировка прервана. Ничего страшного, продолжим в следующий раз!")

@dp.message(ActivityStates.waiting_for_activity)
async def process_custom_activity(message: Message, state: FSMContext):
    await state.set_state(None)
    wait_msg = await message.answer("⏳ Рассчитываю расход...")
    user = await get_user_profile(message.from_user.id)
    prompt = (
        f"Пользователь ({user.get('weight', 70)} кг) выполнил: \"{message.text}\". (1000 шагов = ~35 ккал).\n"
        "Рассчитай примерный расход. Верни JSON:\n"
        '{"title": "название", "burned_kcal": 0, "comment": "похвала"}'
    )
    try:
        act_data = extract_json(await ask_ai(prompt=prompt, model=AI_MODEL))
        await state.update_data(calculated_activity=act_data)
        burned, title, cmt = int(act_data.get("burned_kcal", 150)), clean_html_tags(str(act_data.get("title", "Активность"))), clean_html_tags(str(act_data.get("comment", "Отличная работа!")))
        text = f"🏃 <b>{title}</b>\n━━━━━━━━━━━━━━━━━━━━\n🔥 Расход: <b>{burned} ккал</b>\n\n💬 <i>{cmt}</i>\n\nДобавить в дневник?"
        await wait_msg.edit_text(text, reply_markup=activity_result_keyboard())
    except Exception: await wait_msg.edit_text("Не удалось рассчитать активность.")

@dp.callback_query(F.data == "activity_save")
async def save_activity_handler(callback: CallbackQuery, state: FSMContext):
    act_data = (await state.get_data()).get("calculated_activity")
    if not act_data: return await callback.message.edit_text("❌ Данные устарели.")
    burned, doc_id = int(act_data.get("burned_kcal", 0)), f"{callback.from_user.id}_{today_str()}"
    
    if db and burned > 0:
        doc_ref = db.collection('diaries').document(doc_id)
        doc = await asyncio.to_thread(doc_ref.get)
        await asyncio.to_thread(doc_ref.set, {'burned_kcal': (doc.to_dict().get('burned_kcal', 0) if doc.exists else 0) + burned}, merge=True)
    await state.clear()
    await callback.message.edit_text(f"✅ <b>Сожжено {burned} ккал! Зачтено в дневник.</b>")
    await send_today(callback.message, user_id=callback.from_user.id)

@dp.callback_query(F.data.startswith("done_workout_"))
async def done_workout_callback(callback: CallbackQuery):
    p = callback.data.split("_")
    burned = int(p[2])
    doc_id = f"{callback.from_user.id}_{today_str()}"
    if db:
        await save_user_profile(callback.from_user.id, {"favorite_workout_name": "Недавняя Тренировка"})
        doc_ref = db.collection('diaries').document(doc_id)
        doc = await asyncio.to_thread(doc_ref.get)
        await asyncio.to_thread(doc_ref.set, {'burned_kcal': (doc.to_dict().get('burned_kcal', 0) if doc.exists else 0) + burned}, merge=True)
    await callback.message.edit_text(f"{callback.message.text}\n\n✅ <b>Сожжено {burned} ккал! Зачтено в дневник.</b>")
    await send_today(callback.message, user_id=callback.from_user.id)

# =========================================================
# ВОПРОС НУТРИЦИОЛОГУ / ВЕС / ПОМОЩЬ
# =========================================================
@dp.message(F.text == "💬 Спросить нутрициолога")
@dp.message(Command("ask"))
async def ask_nutritionist_handler(message: Message, state: FSMContext):
    await state.clear()
    if not await check_user_access(message.from_user.id): return await send_paywall(message)
    await state.set_state(AskStates.waiting_for_question)
    await message.answer("💬 <b>Я на связи — спрашивай что угодно!</b>\nОтправь вопрос текстом или голосом.")

@dp.message(AskStates.waiting_for_question)
async def process_nutritionist_question(message: Message, state: FSMContext):
    await state.set_state(None)
    wait_msg = await message.answer("🤔 Анализирую твой вопрос и дневник...")
    user = await get_user_profile(message.from_user.id) or {}
    meals = await get_today_meals(message.from_user.id)
    fam_ctx = "Совет должен подходить для семьи с детьми (без добавленного сахара)." if user.get('family_mode') == 'kids' else ""
    
    # Новый, строгий промпт, который заставляет ИИ отвечать на вопрос
    prompt = (
        f"Пользователь задал ВОПРОС НУТРИЦИОЛОГУ: \"{message.text}\".\n\n"
        f"ТВОЯ ГЛАВНАЯ ЗАДАЧА: Дать четкий, экспертный и заботливый ответ ИМЕННО на этот вопрос.\n"
        f"Дополнительный контекст пользователя (используй ТОЛЬКО если это уместно для ответа):\n"
        f"- Цель: {user.get('goal', 'loss')}, Вес: {user.get('weight', 70)} кг\n"
        f"- Аллергии: {user.get('allergies', 'Нет')}\n"
        f"- Съедено сегодня: {', '.join([m.get('title', '') for m in meals]) if meals else 'Пока ничего'}.\n"
        f"{fam_ctx}\n\n"
        "Правила ответа: Будь кратким. Используй HTML теги (<b>, <i>). ЗАПРЕЩЕНО использовать списки с решётками (###) или markdown-звездочками (**)."
    )
    
    try: 
        answer = await ask_ai(prompt=prompt, model=AI_MODEL)
        await wait_msg.edit_text(clean_html_tags(answer))
    except Exception: 
        await wait_msg.edit_text("Не удалось получить ответ от нутрициолога. Попробуй переформулировать вопрос.")
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
    text = (
        "💡 <b>КАК ПОЛЬЗОВАТЬСЯ NUTRI AI</b>\n\n"
        "1. 📸 <b>Фото или скриншот еды:</b> просто пришли снимок — я определю состав и рассчитаю КБЖУ.\n"
        "2. 🗣 <b>Голосовые:</b> наговори то, что съел(а), или задай вопрос нутрициологу.\n"
        "3. 💧 <b>Вода:</b> жми «+250 мл воды» под дневником «📊 Сегодня».\n"
        "4. 🏋️ <b>Тренировки:</b> выбери «Дома» или «В зале», укажи инвентарь и получи план!\n"
        "5. 🥗 <b>Холодильник:</b> перечисли продукты, и я составлю вкусный ПП-рецепт."
    )
    await message.answer(text)

# =========================================================
# ОНБОРДИНГ
# =========================================================
@dp.callback_query(F.data == "start_onb")
async def start_onboarding_callback(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👨 Мужчина", callback_data="gender_M")], [InlineKeyboardButton(text="👩 Женщина", callback_data="gender_F")]])
    await callback.message.edit_text("Твой <b>пол</b>?", reply_markup=kb)

@dp.callback_query(F.data.startswith("gender_"))
async def gender_handler(callback: CallbackQuery, state: FSMContext):
    await state.update_data(gender=callback.data.split("_")[1])
    await state.set_state(Onboarding.stats)
    await callback.message.edit_text("Напиши через пробел:\n<b>возраст рост вес</b>\n(Например: <code>32 165 63</code>)")

@dp.message(Onboarding.stats)
async def stats_handler(message: Message, state: FSMContext):
    nums = re.findall(r"\d+(?:[.,]\d+)?", message.text)
    if len(nums) < 3: return await message.answer("Нужно три значения: возраст, рост и вес.")
    await state.update_data(age=int(float(nums[0].replace(",", "."))), height=float(nums[1].replace(",", ".")), weight=float(nums[2].replace(",", ".")))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📉 Похудеть", callback_data="goal_loss")],
        [InlineKeyboardButton(text="⚖️ Удержать вес", callback_data="goal_maintain")],
        [InlineKeyboardButton(text="📈 Набрать массу", callback_data="goal_gain")],
    ])
    await message.answer("Какая у тебя цель?", reply_markup=kb)

@dp.callback_query(F.data.startswith("goal_"))
async def goal_handler(callback: CallbackQuery, state: FSMContext):
    goal = callback.data.split("_")[1]
    await state.update_data(goal=goal)
    if goal == "maintain":
        await state.update_data(target_weight=(await state.get_data()).get("weight", 60.0))
        return await show_activity_step(callback.message, edit=True)
    await state.set_state(Onboarding.target_weight)
    await callback.message.edit_text("🎯 <b>Желаемый вес</b>\n\nК какому весу ты стремишься? Напиши число в кг (например: <code>58</code>):")

@dp.message(Onboarding.target_weight)
async def target_weight_handler(message: Message, state: FSMContext):
    nums = re.findall(r"\d+(?:[.,]\d+)?", message.text)
    if not nums: return await message.answer("Пожалуйста, напиши желаемый вес числом (например: 58):")
    await state.update_data(target_weight=float(nums[0].replace(",", ".")))
    await state.set_state(None)
    await show_activity_step(message, edit=False)

async def show_activity_step(target_msg, edit: bool = False):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛋 Сидячий образ жизни", callback_data="activity_low")],
        [InlineKeyboardButton(text="🚶 Лёгкая активность", callback_data="activity_light")],
        [InlineKeyboardButton(text="🏃 Умеренная активность", callback_data="activity_medium")],
        [InlineKeyboardButton(text="🏋️ Высокая активность", callback_data="activity_high")],
    ])
    text = "Выбери уровень физической активности:"
    if edit: await target_msg.edit_text(text, reply_markup=kb)
    else: await target_msg.answer(text, reply_markup=kb)

@dp.callback_query(F.data.startswith("activity_"))
async def activity_handler(callback: CallbackQuery, state: FSMContext):
    await state.update_data(activity=callback.data.split("_")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🍏 Ем всё", callback_data="diet_all")],
        [InlineKeyboardButton(text="🥦 Нутри-подход (клетчатка)", callback_data="diet_nutri")],
        [InlineKeyboardButton(text="🥕 Вегетарианец", callback_data="diet_veg")],
        [InlineKeyboardButton(text="🚫 Без лактозы и глютена", callback_data="diet_allergy")],
    ])
    await callback.message.edit_text("Предпочтения в питании:", reply_markup=kb)

@dp.callback_query(F.data.startswith("diet_"))
async def diet_handler(callback: CallbackQuery, state: FSMContext):
    await state.update_data(diet=callback.data.split("_")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧍 Только для себя", callback_data="fam_self")],
        [InlineKeyboardButton(text="👨‍👩‍👧‍👦 Готовлю на всю семью (дети)", callback_data="fam_kids")],
    ])
    await callback.message.edit_text("Для кого составляем рацион?\n<i>С учетом детей мы уберем скрытый сахар.</i>", reply_markup=kb)

@dp.callback_query(F.data.startswith("fam_"))
async def fam_handler(callback: CallbackQuery, state: FSMContext):
    await state.update_data(family_mode=callback.data.split("_")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌰 Орехи", callback_data="allergy_nuts"), InlineKeyboardButton(text="🥛 Лактоза", callback_data="allergy_lactose")],
        [InlineKeyboardButton(text="🌾 Глютен", callback_data="allergy_gluten"), InlineKeyboardButton(text="🐟 Морепродукты", callback_data="allergy_seafood")],
        [InlineKeyboardButton(text="✍️ Написать текстом", callback_data="allergy_custom")],
        [InlineKeyboardButton(text="❌ Нет аллергий", callback_data="allergy_none")],
    ])
    await callback.message.edit_text("Есть ли аллергии?", reply_markup=kb)

@dp.callback_query(F.data.startswith("allergy_"))
async def allergy_callback_handler(callback: CallbackQuery, state: FSMContext):
    alg = callback.data.split("_")[1]
    if alg == "custom":
        await state.set_state(Onboarding.custom_allergy)
        return await callback.message.edit_text("✍️ Напиши текстом, на что аллергия:")
    await finish_onboarding(callback.message, state, callback.from_user.id, {"nuts": "Орехи", "lactose": "Лактоза", "gluten": "Глютен", "seafood": "Рыба", "none": "Нет"}.get(alg, "Нет"), True)

@dp.message(Onboarding.custom_allergy)
async def custom_allergy_handler(message: Message, state: FSMContext):
    await finish_onboarding(message, state, message.from_user.id, message.text.strip(), False)

async def finish_onboarding(target_msg, state: FSMContext, user_id: int, allergy_text: str, is_cb: bool):
    d = await state.get_data()
    norm = calculate_norm(d["gender"], d["age"], d["height"], d["weight"], d["goal"], d["activity"])
    tw = float(d.get("target_weight", d.get("weight", 60.0)))
    
    eu = await get_user_profile(user_id)
    is_new = eu is None or not eu.get("trial_until")
    now = now_local()
    
    if is_new:
        tu, pu, ca = (now + timedelta(days=14)).isoformat(), None, now.isoformat()
    else:
        tu, pu, ca = eu.get("trial_until"), eu.get("premium_until"), eu.get("created_at", now.isoformat())

    ud = {
        "user_id": user_id, "gender": d["gender"], "age": d["age"], "height": d["height"], "weight": d["weight"], 
        "goal": d["goal"], "activity": d["activity"], "diet": d.get("diet", "all"), "family_mode": d.get("family_mode", "self"), 
        "allergies": allergy_text, "home_equipment": "bodyweight", "calories": norm["calories"], "protein": norm["protein"], "fat": norm["fat"], "carbs": norm["carbs"],
        "target_weight": tw, "trial_until": tu, "premium_until": pu, "created_at": ca,
        "first_touch_at": now_local().isoformat(),
        "start_parameter": d.get("start_parameter", ""),
        "utm_source": d.get("utm_source", "organic"),
        "utm_medium": d.get("utm_medium", ""),
        "utm_campaign": d.get("utm_campaign", "")
    }       
    await save_user_profile(user_id, ud)
    await state.clear()
    
    bonus = "🎁 <b>Активировано 30 дней бесплатно!</b>" if is_new else "✅ <b>Норма успешно обновлена!</b> 🥗"
    
    # 1. Расчет сроков достижения цели
    weight, target = d["weight"], tw
    goal = d["goal"]
    date_str, goal_text, weeks_str = "", "⚖️ Поддержание веса", ""
    weeks = 0
    if goal == "loss" and weight > target:
        goal_text = "📉 Снижение веса"
        weeks = (weight - target) / 0.6
        date_str = (now_local() + timedelta(weeks=weeks)).strftime('%d.%m.%Y')
        weeks_str = f"~{int(weeks)} нед."
    elif goal == "gain" and target > weight:
        goal_text = "📈 Набор массы"
        weeks = (target - weight) / 0.4
        date_str = (now_local() + timedelta(weeks=weeks)).strftime('%d.%m.%Y')
        weeks_str = f"~{int(weeks)} нед."

    text = (
        "🎯 <b>Твоя норма рассчитана!</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 Цель: {tw} кг ({goal_text})\n"
        f"🔥 {norm['calories']} ккал | Б {norm['protein']}г | Ж {norm['fat']}г | У {norm['carbs']}г\n"
        f"🛡 Аллергии: {allergy_text}\n👨‍👩‍👧‍👦 Режим семьи: {'Включен' if d.get('family_mode') == 'kids' else 'Выключен'}\n\n{bonus}"
    )
    
    # 2. Мгновенно выдаем сухие цифры
    if is_cb:
        await target_msg.edit_text(text)
    else: 
        await target_msg.answer(text)

    # 3. ИИ начинает печатать лекцию и прогноз
    wait_msg = await target_msg.answer("⏳ <i>Анализирую твою цель и пишу персональный прогноз...</i>")
    await bot.send_chat_action(chat_id=target_msg.chat.id, action="typing")

    gender_text = "мужчины" if d["gender"] == "M" else "женщины"
    
    if goal == "gain" and weeks > 0:
        goal_context = (
            f"Цель: НАБОР МАССЫ до {target} кг. Расчетное время: {weeks_str} (примерно к {date_str}). "
            "Объясни, почему набор качественного веса (мышц, а не просто жира) требует времени и плавного профицита калорий. "
            "Расскажи, почему мы не делаем огромный профицит и почему постепенный темп — самый здоровый."
        )
    elif goal == "loss" and weeks > 0:
        goal_context = (
            f"Цель: ПОХУДЕНИЕ до {target} кг. Расчетное время: {weeks_str} (примерно к {date_str}). "
            "Объясни механику здорового похудения: почему плавный дефицит калорий работает лучше, чем жесткие голодовки. "
            "Расскажи, как такой темп защищает от срывов, сохраняет качество тела и бережет здоровье."
        )
    else:
        goal_context = "Цель: УДЕРЖАНИЕ ВЕСА. Объясни, почему сбалансированное питание поможет сохранить форму, энергию и здоровье на долгие годы."

    prompt = (
        f"Выступи в роли заботливого профи-нутрициолога. Пользователь ({gender_text}) "
        f"получил свою норму: {norm['protein']}г белков, {norm['fat']}г жиров, {norm['carbs']}г углеводов.\n\n"
        "Твоя задача — понятно, поддерживающе и логично объяснить:\n"
        "1. Зачем нужно именно столько белка (сохранение мышц, сытость).\n"
        "2. Зачем нужны жиры (гормональный фон, здоровье кожи/волос, почему их нельзя урезать).\n"
        "3. Зачем нужны углеводы (энергия, работа мозга).\n"
        f"4. Прокомментируй сроки: {goal_context}\n\n"
        "Пиши структурно, как наставник. Используй HTML теги (<b>, <i>). ЗАПРЕЩЕНО использовать Markdown (* или #)."
    )

    try:
        explanation = await ask_ai(prompt=prompt, model=AI_MODEL)
        await wait_msg.edit_text(f"🧠 <b>Почему такие цифры и сроки:</b>\n\n{explanation}")
    except Exception:
        await wait_msg.delete()

    # 4. Выводим клавиатуру и призыв к действию
    if is_new:
        await target_msg.answer("Всё готово! Жду твоё первое фото еды 📸", reply_markup=main_menu)
    else:
        await target_msg.answer("Твой профиль и норма успешно обновлены! 🥗", reply_markup=main_menu)

# =========================================================
# РЕДАКТИРОВАНИЕ И ТРЕКЕРЫ
# =========================================================
@dp.callback_query(F.data == "profile_edit_allergies")
async def profile_edit_allergies_handler(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌰 Орехи", callback_data="update_alg_nuts"), InlineKeyboardButton(text="🥛 Лактоза", callback_data="update_alg_lactose")],
        [InlineKeyboardButton(text="🌾 Глютен", callback_data="update_alg_gluten"), InlineKeyboardButton(text="❌ Нет", callback_data="update_alg_none")],
    ])
    await callback.message.edit_text("🛡 <b>Выбери аллергию:</b>", reply_markup=kb)

@dp.callback_query(F.data.startswith("update_alg_"))
async def update_allergy_callback_handler(callback: CallbackQuery):
    alg = {"nuts": "Орехи", "lactose": "Лактоза", "gluten": "Глютен", "none": "Нет"}.get(callback.data.replace("update_alg_", ""), "Нет")
    await save_user_profile(callback.from_user.id, {"allergies": alg})
    await callback.message.edit_text(f"✅ Обновлено: {alg}")

@dp.callback_query(F.data == "profile_recount_norm")
async def profile_recount_norm_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await start_onboarding_callback(callback)

@dp.callback_query(F.data == "add_water_250")
async def add_water_handler(callback: CallbackQuery):
    doc_id = f"{callback.from_user.id}_{today_str()}"
    new_w = 250
    if db:
        doc_ref = db.collection('diaries').document(doc_id)
        doc = await asyncio.to_thread(doc_ref.get)
        new_w = (doc.to_dict().get('water', 0) if doc.exists else 0) + 250
        await asyncio.to_thread(doc_ref.set, {'water': new_w}, merge=True)
    await callback.answer(f"💧 Добавлено 250 мл! Всего: {new_w} мл")
    await send_today(callback.message, user_id=callback.from_user.id)

@dp.callback_query(F.data == "delete_last_meal")
async def delete_last_meal_callback(callback: CallbackQuery):
    doc_id = f"{callback.from_user.id}_{today_str()}"
    if db:
        doc_ref = db.collection('diaries').document(doc_id)
        doc = await asyncio.to_thread(doc_ref.get)
        if doc.exists:
            meals = doc.to_dict().get('meals', [])
            if meals:
                rm = meals.pop()
                await asyncio.to_thread(doc_ref.set, {'meals': meals}, merge=True)
                await callback.answer(f"🗑 Удалено: {rm.get('title', 'Блюдо')}")
                return await send_today(callback.message, user_id=callback.from_user.id)
    await callback.answer("В дневнике пусто")

@dp.message(WeightStates.waiting_for_weight)
async def process_weight_update(message: Message, state: FSMContext):
    nums = re.findall(r"\d+(?:[.,]\d+)?", message.text)
    if not nums: return await message.answer("Напиши вес числом.")
    nw = float(nums[0].replace(",", "."))
    u = await get_user_profile(message.from_user.id)
    nn = calculate_norm(u.get("gender", "F"), u.get("age", 25), u.get("height", 165), nw, u.get("goal", "loss"), u.get("activity", "low"))
    await save_user_profile(message.from_user.id, {"weight": nw, "calories": nn["calories"], "protein": nn["protein"], "fat": nn["fat"], "carbs": nn["carbs"]})
    await state.clear()
    await message.answer(f"⚖️ Новый вес <b>{nw} кг</b> зафиксирован!\n🎯 <b>Новая норма:</b> {nn['calories']} ккал (Б:{nn['protein']} Ж:{nn['fat']} У:{nn['carbs']})")

# =========================================================
# УТРЕННИЙ И ВЕЧЕРНИЙ РАЗБОР
# =========================================================
async def send_morning_digest():
    if not db: return
    for doc in (await asyncio.to_thread(db.collection('users').get)):
        try: await bot.send_message(chat_id=int(doc.id), text=f"☀️ Доброе утро!\nПлан на день: <b>{doc.to_dict().get('calories', 2000)} ккал</b>\n\n{random.choice(NUTRITION_TIPS)}")
        except Exception: pass

async def send_evening_digest():
    if not db: return
    for doc in (await asyncio.to_thread(db.collection('users').get)):
        uid = doc.id
        u = doc.to_dict()
        diary = await asyncio.to_thread(db.collection('diaries').document(f"{uid}_{today_str()}").get)
        if not diary.exists or not diary.to_dict().get('meals'): continue
        m = diary.to_dict().get('meals', [])
        prompt = f"Оцени день: съедено {sum(x.get('calories',0) for x in m)}/{u.get('calories', 2000)} ккал. Напиши теплый разбор. Используй HTML."
        try: await bot.send_message(chat_id=int(uid), text=f"🌙 <b>Итоги дня</b>\n━━━━━━━━━\n{clean_html_tags(await ask_ai(prompt=prompt, model=AI_MODEL))}")
        except Exception: pass

# =========================================================
# УМНЫЙ ИИ (ГОЛОС И ТЕКСТ) — ЖИВОЕ МЫШЛЕНИЕ
# =========================================================
async def process_smart_input(text: str, message: Message, state: FSMContext, wait_msg: Message):
    try:
        await bot.send_chat_action(chat_id=message.chat.id, action="typing")
        intent = await ask_ai(prompt=f"Текст: \"{text}\". Ответь 1 словом: ACTIVITY, FOOD или QUESTION.", model=AI_MODEL)
        user = await get_user_profile(message.from_user.id) or {}
        fam_ctx = "Учитывай детей (без сахара)." if user.get('family_mode') == 'kids' else ""

        if "ACTIVITY" in intent:
            await wait_msg.edit_text("🏃‍♂️ <i>Считаю потраченные калории...</i>")
            await bot.send_chat_action(chat_id=message.chat.id, action="typing")
            res = extract_json(await ask_ai(prompt=f"Вес {user.get('weight', 70)}кг, выполнил: {text}. Верни JSON: {{\"title\":\"\",\"burned_kcal\":0,\"comment\":\"\"}}", model=AI_MODEL))
            await state.update_data(calculated_activity=res)
            await wait_msg.edit_text(f"🏃 <b>{res.get('title')}</b>\n🔥 Расход: {res.get('burned_kcal',0)} ккал\n\nДобавить?", reply_markup=activity_result_keyboard())

        elif "FOOD" in intent:
            await wait_msg.edit_text("🧠 <i>Рассчитываю КБЖУ...</i>")
            await bot.send_chat_action(chat_id=message.chat.id, action="typing")
            
            fav_str = ""
            if user.get("favorite_foods"): 
                fav_items = "\n".join([f"- {f['title']}: {f['calories']} ккал (Б:{f['protein']}, Ж:{f['fat']}, У:{f['carbs']})" for f in user.get("favorite_foods")])
                fav_str = (
                    f"ВНИМАНИЕ! БАЗА ЛЮБИМЫХ БЛЮД ПОЛЬЗОВАТЕЛЯ:\n{fav_items}\n"
                    "ПРАВИЛО 1: Если съедено блюдо ИЗ ЭТОГО СПИСКА — СТРОГО бери эти цифры! Если указан другой вес, пересчитай КБЖУ пропорционально.\n"
                    "ПРАВИЛО 2: Если съеденного блюда НЕТ в этом списке — считай его как обычно.\n\n"
                )
            
            # ИЗМЕНЕНО: Запрашиваем расширенный JSON с массивом ingredients
            prompt_json = '{"title":"","calories":0,"protein":0,"fat":0,"carbs":0,"ingredients":[{"name":"название","weight_g":0,"calories":0,"protein":0,"fat":0,"carbs":0}],"comment":""}'
            res = extract_json(await ask_ai(prompt=f"{fav_str}Съедено: \"{text}\".\nРассчитай БЖУ. Разбей на ингредиенты, если возможно. JSON: {prompt_json}", model=AI_MODEL))
            
            # Сохраняем исходный текст пользователя
            res["original_description"] = text 
            await state.update_data(calculated_food=res)
            
            await wait_msg.edit_text(f"🍽 <b>{res['title']}</b>\n🔥 {res['calories']} ккал (Б:{res['protein']} Ж:{res['fat']} У:{res['carbs']})\n\nВнести?", reply_markup=result_keyboard())
        else:
            await wait_msg.edit_text("📚 <i>Ищу ответ в базе знаний...</i>")
            await bot.send_chat_action(chat_id=message.chat.id, action="typing")
            ans = await ask_ai(prompt=f"Вопрос: {text}\nАллергии: {user.get('allergies','Нет')}\n{fam_ctx}\nОтветь как нутрициолог (коротко, HTML).", model=AI_MODEL)
            await wait_msg.edit_text(clean_html_tags(ans))

    except Exception: await wait_msg.edit_text("Не удалось разобрать сообщение.")

@dp.message(F.voice)
async def voice_handler(message: Message, state: FSMContext):
    if not await check_user_access(message.from_user.id): return await send_paywall(message)
    wait_msg = await message.answer("Слушаю... 🎧")
    try:
        buffer = io.BytesIO()
        await bot.download_file((await bot.get_file(message.voice.file_id)).file_path, destination=buffer)
        buffer.name = "v.ogg"
        text = (await ai_client.audio.transcriptions.create(model="whisper-1", file=buffer)).text
        await wait_msg.edit_text(f"🗣 <b>Ты сказал(а):</b> «{text}»\n\n⏳ Думаю...")
        await process_smart_input(text, message, state, wait_msg)
    except Exception: await wait_msg.edit_text("Ошибка аудио.")

# =========================================================
# РЕЦЕПТЫ И ФОТО / СКРИНШОТЫ ЕДЫ
# =========================================================
@dp.message(FoodStates.waiting_for_recipe)
async def recipe_handler(message: Message, state: FSMContext):
    await state.clear()
    wait_msg = await message.answer("⏳ Собираю рецепт...")
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    u = await get_user_profile(message.from_user.id) or {}
    try: await wait_msg.edit_text(clean_html_tags(await ask_ai(prompt=f"Рецепт из: {message.text}. Цель: {u.get('goal')}. Режим: {u.get('family_mode')}. HTML.", model=AI_MODEL)))
    except Exception: await wait_msg.edit_text("Ошибка рецепта.")

@dp.message(F.photo)
async def photo_handler(message: Message, state: FSMContext):
    if not await check_user_access(message.from_user.id): return await send_paywall(message)
    wait_msg = await message.answer("👀 <i>Смотрю на фотографию...</i>")
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    
    try:
        buffer = io.BytesIO()
        await bot.download_file((await bot.get_file(message.photo[-1].file_id)).file_path, destination=buffer)
        b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
        
        await asyncio.sleep(1)
        await wait_msg.edit_text("🔍 <i>Распознаю ингредиенты и граммовки...</i>")
        await bot.send_chat_action(chat_id=message.chat.id, action="typing")
        
        prompt = (
            "Действуй как заботливый нутрициолог. Проанализируй еду на фото.\n"
            "Формат ответа СТРОГО такой:\n"
            "🍽 Продукт/Блюдо: [Название]\n"
            "⚖️ Масса: [Примерный вес] г\n"
            "🔥 КБЖУ: [К] ккал | Б: [Б]г | Ж: [Ж]г | У: [У]г\n\n"
            "ВАЖНОЕ ПРАВИЛО: В конце добавь 1-2 предложения полезного совета или оценки баланса блюда (например, похвали за клетчатку/белок или предупреди о скрытых жирах/сахаре). "
            "НИКАКИХ ПРИВЕТСТВИЙ (не пиши 'Привет', 'Я изучила фото'). Сразу начинай со строки '🍽 Продукт/Блюдо:'."
        )
        
        res = await ask_ai(image_base64=b64, prompt=prompt, model=AI_VISION_MODEL)
        await state.update_data(recognized_food=res, image_base64=b64)
        await wait_msg.edit_text(f"{clean_html_tags(res)}\n\nВсё верно?", reply_markup=food_keyboard())
    except Exception: 
        await wait_msg.edit_text("Не удалось распознать фото.")
@dp.callback_query(F.data == "food_correct")
async def food_correct_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await callback.message.edit_text("⏳ Рассчитываю КБЖУ...")
    await bot.send_chat_action(chat_id=callback.message.chat.id, action="typing")
    u = await get_user_profile(callback.from_user.id) or {}
    fav_str = f"ВНИМАНИЕ! БАЗА:\n" + "\n".join([f"- {f['title']}: {f['calories']} ккал" for f in u.get("favorite_foods", [])]) + "\n" if u.get("favorite_foods") else ""
    try:
        # ИЗМЕНЕНО: Запрашиваем расширенный JSON с массивом ingredients
        prompt_json = '{"title":"","calories":0,"protein":0,"fat":0,"carbs":0,"ingredients":[{"name":"название","weight_g":0,"calories":0,"protein":0,"fat":0,"carbs":0}],"comment":""}'
        res = extract_json(await ask_ai(prompt=f"{fav_str}Рассчитай БЖУ:\n{data.get('recognized_food')}\nРазбей на ингредиенты. JSON: {prompt_json}", model=AI_MODEL))
        
        # Для фото исходным описанием будет то, что ИИ увидел на картинке
        res["original_description"] = data.get('recognized_food', '')
        await state.update_data(calculated_food=res)
        
        txt = f"🍽 <b>{res['title']}</b>\n━━━━━━━━━\n🔥 <b>{res['calories']} ккал</b>\nБ: {res['protein']}г | Ж: {res['fat']}г | У: {res['carbs']}г\n\n💬 <i>{res.get('comment','')}</i>\nВнести в дневник?"
        await callback.message.edit_text(txt, reply_markup=result_keyboard())
    except Exception: 
        await callback.message.edit_text("Ошибка расчета.")

@dp.callback_query(F.data == "food_edit")
async def food_edit_handler(callback: CallbackQuery, state: FSMContext):
    await state.set_state(FoodStates.correcting)
    await callback.message.edit_text("Напиши, что исправить (например: курицы 250г, а не 150г):")

@dp.message(FoodStates.correcting)
async def correcting_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    prompt = (
        f"Старое описание блюда: {data.get('recognized_food')}\n"
        f"Пользователь просит исправить: {message.text}\n\n"
        "Выдай НОВОЕ описание блюда, учитывая исправления. "
        "КРИТИЧЕСКИ ВАЖНО: НИКАКИХ ПРИВЕТСТВИЙ! Не пиши 'Привет', 'Конечно' и т.д. "
        "Начинай сразу с описания блюда и КБЖУ. Используй HTML-теги."
    )
    res = await ask_ai(prompt=prompt, model=AI_MODEL)
    await state.update_data(recognized_food=res)
    await state.set_state(None)
    await message.answer(f"{clean_html_tags(res)}\n\nТеперь всё верно?", reply_markup=food_keyboard())
    
@dp.callback_query(F.data == "meal_save")
async def save_meal_handler(callback: CallbackQuery, state: FSMContext):
    food = (await state.get_data()).get("calculated_food", {})
    await add_meal_to_today(callback.from_user.id, {
        "title": food.get("title", "Еда"), 
        "calories": food.get("calories", 0), 
        "protein": food.get("protein", 0), 
        "fat": food.get("fat", 0), 
        "carbs": food.get("carbs", 0), 
        "created_at": now_local().isoformat()
    })
    await state.clear()
    await callback.message.edit_text("✅ Сохранено в дневник.")
    await send_today(callback.message, user_id=callback.from_user.id)

@dp.callback_query(F.data == "food_remember")
async def food_remember_handler(callback: CallbackQuery, state: FSMContext):
    food = (await state.get_data()).get("calculated_food", {})
    if not food: return await callback.answer("Ошибка данных.")
    
    if db:
        try:
            doc_ref = db.collection('users').document(str(callback.from_user.id))
            doc = await asyncio.to_thread(doc_ref.get)
            
            # 1. ОБРАТНАЯ СОВМЕСТИМОСТЬ: оставляем старый favorite_foods
            favs = doc.to_dict().get('favorite_foods', []) if doc.exists else []
            favs.append({
                "title": food.get("title", "Еда"), 
                "calories": food.get("calories", 0), 
                "protein": food.get("protein", 0), 
                "fat": food.get("fat", 0), 
                "carbs": food.get("carbs", 0)
            })
            await asyncio.to_thread(doc_ref.set, {'favorite_foods': favs[-20:]}, merge=True)
            
            # 2. НОВАЯ ЛОГИКА: сохраняем полный шаблон блюда в подколлекцию
            dish_id = str(uuid.uuid4())
            dish_data = {
                "id": dish_id,
                "title": food.get("title", "Еда"),
                "calories": food.get("calories", 0),
                "protein": food.get("protein", 0),
                "fat": food.get("fat", 0),
                "carbs": food.get("carbs", 0),
                "ingredients": food.get("ingredients", []),
                "original_description": food.get("original_description", ""),
                "created_at": now_local().isoformat()
            }
            dishes_ref = doc_ref.collection('saved_dishes').document(dish_id)
            await asyncio.to_thread(dishes_ref.set, dish_data)
            
        except Exception as e:
            logger.error(f"Ошибка БД при сохранении блюда: {e}")
            return await callback.answer("Временная ошибка базы данных. Попробуйте позже.", show_alert=True)
            
    await callback.answer(f"❤️ {food.get('title')} сохранено в твою базу!")
    await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Сохранить в дневник", callback_data="meal_save")], 
        [InlineKeyboardButton(text="🗑 Удалить", callback_data="food_delete")]
    ]))
@dp.callback_query(F.data == "food_delete")
async def delete_food_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🗑 Отменено.")

# =========================================================
# УНИВЕРСАЛЬНЫЙ ТЕКСТ (ВСЕГДА ВНИЗУ!)
# =========================================================
@dp.message(F.text)
async def universal_text_handler(message: Message, state: FSMContext):
    if not await check_user_access(message.from_user.id): return await send_paywall(message)
    if message.text.startswith('/'): return
    if message.text in ["📊 Сегодня", "😋 Вкусняшка", "🥗 Что приготовить", "🏋️ Тренировка", "💬 Спросить нутрициолога", "⚖️ Вес", "👤 Профиль", "🎯 Моя норма", "❓ Помощь"]: return
    if await state.get_state(): return
    wait_msg = await message.answer("🤔 Читаю...")
    await process_smart_input(message.text, message, state, wait_msg)

# =========================================================
# ЗАПУСК СЕРВЕРА И БОТА
# =========================================================
async def health_handler(request: web.Request): 
    return web.json_response({"status": "ok"})

async def bepaid_webhook_handler(request: web.Request):
    try:
        data = await request.json()
        transaction = data.get("transaction", {})
        
        order_id = transaction.get("tracking_id")
        status = transaction.get("status")
        uid = transaction.get("uid") 
        amount_paid = transaction.get("amount", 0) / 100.0  # У bePaid копейки
        currency_paid = transaction.get("currency")

        if not order_id or not db:
            return web.Response(text="OK", status=200)

        doc_ref = db.collection('payments').document(str(order_id))
        doc = await asyncio.to_thread(doc_ref.get)
        if not doc.exists: return web.Response(text="OK", status=200)

        order_data = doc.to_dict()
        
        # Защита от дублей (Идемпотентность)
        if order_data.get("status") == "paid":
            return web.Response(text="OK", status=200)

        # Строгая проверка суммы и валюты (предотвращает подмену данных)
        expected_amount = order_data.get("amount", 0.0)
        expected_currency = order_data.get("currency", "BYN")
        
        if status == "successful":
            if amount_paid < expected_amount or currency_paid != expected_currency:
                logger.error(f"Фрод! Несовпадение суммы: ожидалось {expected_amount} {expected_currency}, получено {amount_paid} {currency_paid}")
                return web.Response(text="OK", status=200)

            # Сохраняем расширенные данные (paid_at, transaction_id)
            await asyncio.to_thread(doc_ref.update, {
                "status": "paid",
                "paid_at": now_local().isoformat(),
                "transaction_id": uid
            })

            # ... (здесь остается твой старый код продления подписки premium_until и уведомления бота) ...

        elif status in ["failed", "incomplete", "error"]:
            await asyncio.to_thread(doc_ref.update, {
                "status": "failed",
                "failed_at": now_local().isoformat(),
                "transaction_id": uid,
                "error_message": transaction.get("message", "")
            })

        return web.Response(text="OK", status=200)
    except Exception as e:
        logger.error(f"Критическая ошибка Webhook: {e}")
        return web.Response(text="OK", status=200)

async def main():
    # 1. ЗАПУСКАЕМ ВЕБ-СЕРВЕР (ЧТОБЫ RENDER НЕ РУГАЛСЯ)
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_post("/webhook/bepaid", bepaid_webhook_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Render передает свой порт через переменную окружения PORT
    port = int(os.getenv("PORT", "10000"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info(f"🌐 Веб-сервер успешно запущен на порту {port}")
    
    # 2. НАСТРАИВАЕМ БОТА
    await set_bot_description(bot)
    await set_bot_commands(bot)
    
    # 3. ЗАПУСКАЕМ ПЛАНИРОВЩИК (УТРО/ВЕЧЕР)
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(send_morning_digest, "cron", hour=9, minute=0)
    scheduler.add_job(send_evening_digest, "cron", hour=21, minute=0)
    scheduler.start()

    # 4. ЗАПУСКАЕМ САМОГО БОТА
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
