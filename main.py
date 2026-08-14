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
    ReplyKeyboardMarkup,
    Message,
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

if not BOT_TOKEN: raise RuntimeError("Не найден BOT_TOKEN")
if not AI_API_KEY: raise RuntimeError("Не найден AI_API_KEY")
if not AI_BASE_URL: raise RuntimeError("Не найден AI_BASE_URL")

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
    logger.warning("⚠️ FIREBASE_JSON не найден.")
    db = None

# =========================================================
# AI-КЛИЕНТ И TELEGRAM
# =========================================================
ai_client = AsyncOpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL.rstrip("/"), timeout=90, max_retries=2)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Сегодня"), KeyboardButton(text="😋 Вкусняшка")],
        [KeyboardButton(text="🥗 Что приготовить"), KeyboardButton(text="🏋️ Тренировка")],
        [KeyboardButton(text="💬 Спросить нутрициолога")],
        [KeyboardButton(text="⚖️ Вес"), KeyboardButton(text="👤 Профиль"), KeyboardButton(text="🎯 Моя норма")]
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

class WeightStates(StatesGroup): waiting_for_weight = State()
class ActivityStates(StatesGroup): waiting_for_activity = State()
class TreatStates(StatesGroup): waiting_for_treat = State()
class AskStates(StatesGroup): waiting_for_question = State()

NUTRITION_TIPS = [
    "💡 <b>Совет дня:</b> По «правилу тарелки» 50% обеда должны составлять овощи и зелень (клетчатка). Это здоровая микрофлора и долгая сытость!",
    "💡 <b>Совет дня:</b> Качественный белок в каждом приёме пищи защищает от резких скачков сахара и вечерних срывов на сладкое."
]

# =========================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (РЕДИЗАЙН APPLE RINGS)
# =========================================================
def clean_html_tags(text: str) -> str:
    return re.sub(r'<(?!/?(b|i|code|s|u)\b)[^>]*>', '', text)

def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")

def make_progress_bar(current: int, target: int, active_char="🟢", inactive_char="⚪", length: int = 7) -> str:
    """Генерирует стильный прогресс-бар из эмодзи-кружочков."""
    if target <= 0: return inactive_char * length
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
    data['target_weight'] = float(data.get('target_weight', data.get('weight', 60.0)))
    return data

async def save_user_profile(user_id: int, data: dict):
    if db: await asyncio.to_thread(db.collection('users').document(str(user_id)).set, data, merge=True)

async def check_user_access(user_id: int) -> bool:
    user = await get_user_profile(user_id)
    if not user: return False
    now = datetime.now()
    
    try:
        if user.get("premium_until") and now < datetime.fromisoformat(user.get("premium_until")): return True
    except: pass

    trial_str = user.get("trial_until")
    if not trial_str:
        await save_user_profile(user_id, {"trial_until": (now + timedelta(days=14)).isoformat()})
        return True
        
    try:
        if now < datetime.fromisoformat(trial_str): return True
    except: pass
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
    try: await bot.set_my_description("NutriAi — твой нутрициолог в кармане.")
    except: pass

async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="today", description="📊 Дневник за сегодня"),
        BotCommand(command="treat", description="😋 Вкусняшка"),
        BotCommand(command="fridge", description="🥗 Что приготовить"),
        BotCommand(command="workout", description="🏋️ Тренировка"),
        BotCommand(command="ask", description="💬 Спросить нутрициолога"),
        BotCommand(command="weight", description="⚖️ Динамика веса"),
        BotCommand(command="profile", description="👤 Профиль"),
        BotCommand(command="plan", description="🎯 Моя норма"),
    ]
    try: await bot.set_my_commands(commands)
    except: pass

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
# ЖЕЛЕЗОБЕТОННАЯ МАТЕМАТИКА
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
    
    try:
        p, f, c = max(0, float(data.get("protein") or 0)), max(0, float(data.get("fat") or 0)), max(0, float(data.get("carbs") or 0))
    except: p, f, c = 0.0, 0.0, 0.0
    
    data["protein"], data["fat"], data["carbs"] = int(p), int(f), int(c)
    data["calories"] = int((p * 4) + (f * 9) + (c * 4))
    if not data.get("title") or not str(data["title"]).strip(): data["title"] = "Приём пищи"
    return data

async def ask_ai(prompt: str, image_base64: str | None = None, model: str | None = None) -> str:
    used_model = model or AI_MODEL
    sys = (
        "Ты эмпатичный нутрициолог. Твой подход — забота и поддержка. НИКОГДА не ругай за срывы. "
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
    payload = {
        "request": {
            "amount": int(amount_byn * 100), "currency": "BYN", "description": f"Подписка NutriAi на {months} мес.",
            "notification_url": "https://diet-bot-zqpn.onrender.com/webhook/bepaid",
            "tracking_id": f"sub_{user_id}_{months}_{int(datetime.now().timestamp())}",
        }
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post("https://checkout.bepaid.by/v2/redirect_biller/bills", json=payload, auth=aiohttp.BasicAuth(BEPAID_SHOP_ID, BEPAID_SECRET_KEY)) as r:
                if r.status in (200, 201): return (await r.json()).get("checkout", {}).get("redirect_url")
    except: pass
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

# =========================================================
# ВЫВОД ДНЕВНИКА (НОВЫЙ ПРЕМИУМ ДИЗАЙН)
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
    total_p, total_f, total_c = sum(m.get('protein', 0) for m in meals), sum(m.get('fat', 0) for m in meals), sum(m.get('carbs', 0) for m in meals)
    norm_kcal, norm_p, norm_f, norm_c = user.get('calories', 2000), user.get('protein', 100), user.get('fat', 70), user.get('carbs', 200)
    
    if not meals:
        meals_text = "<i>Пока пусто. Пришли фото еды — я всё посчитаю 📸</i>\n\n"
    else:
        meals_text = ""
        for meal in meals:
            title_clean = clean_html_tags(str(meal.get('title', 'Блюдо')))
            time_str = "🍽"
            try: 
                if 'created_at' in meal: time_str = f"⏰ {datetime.fromisoformat(meal['created_at']).strftime('%H:%M')}"
            except: pass
            meals_text += f"{time_str} | <b>{title_clean}</b>\n↳ {meal.get('calories', 0)} ккал • Б:{meal.get('protein', 0)} Ж:{meal.get('fat', 0)} У:{meal.get('carbs', 0)}\n\n"
        
    net_kcal = total_kcal - burned_kcal
    if net_kcal > norm_kcal: status_text = f"⚠️ <b>Превышение:</b> +{net_kcal - norm_kcal} ккал\n<i>Один день профицита не страшен! Завтра возвращаемся к норме 💪</i>"
    else: status_text = f"✅ <b>Остаток на день:</b> {norm_kcal - net_kcal} ккал"

    burned_str = f" <i>(сожжено {burned_kcal} ккал)</i>" if burned_kcal > 0 else ""

    text = (
        f"📋 <b>ТВОЙ ДНЕВНИК</b>\n\n{meals_text}━━━━━━━━━━━━━━━━━━━━\n"
        f"🔥 <b>Энергия:</b> {total_kcal} из {norm_kcal} {burned_str}\n{make_progress_bar(total_kcal, norm_kcal, '🟠', '⚪')}\n\n"
        f"🥩 <b>Белки:</b> {total_p} / {norm_p} г\n{make_progress_bar(total_p, norm_p, '🔴', '⚪')}\n\n"
        f"🥑 <b>Жиры:</b> {total_f} / {norm_f} г\n{make_progress_bar(total_f, norm_f, '🟡', '⚪')}\n\n"
        f"🍚 <b>Углеводы:</b> {total_c} / {norm_c} г\n{make_progress_bar(total_c, norm_c, '🟢', '⚪')}\n\n"
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
            await asyncio.sleep(0.1)
        except: pass
    await message.answer(f"✅ Рассылка завершена! Отправлено: {count} чел.")

@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    await state.clear()
    user = await get_user_profile(message.from_user.id)
    if user: return await message.answer("С возвращением! Пришли фото еды 📸", reply_markup=main_menu)

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
    await send_today(message)

@dp.message(F.text == "👤 Профиль")
@dp.message(Command("profile"))
async def profile_handler(message: Message, state: FSMContext):
    await state.clear()
    user = await get_user_profile(message.from_user.id)
    if not user: return await message.answer("Сначала нажми /start.")

    diet_titles = {"all": "🍏 Ем всё", "nutri": "🥦 Нутри-подход", "veg": "🥕 Вегетарианец", "allergy": "🚫 Без лактозы и глютена"}
    fam_str = "👨‍👩‍👧‍👦 Готовлю для семьи (без скрытого сахара)" if user.get('family_mode') == 'kids' else "🧍 Только для себя"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👨‍👩‍👧‍👦 Режим семьи: изменить", callback_data="toggle_family_mode")],
        [InlineKeyboardButton(text="🛡 Изменить аллергии", callback_data="profile_edit_allergies")],
        [InlineKeyboardButton(text="⚙️ Пересчитать норму (Опрос)", callback_data="profile_recount_norm")]
    ])

    await message.answer(
        "👤 <b>ТВОЙ ПРОФИЛЬ</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 Целевой вес: <b>{user.get('target_weight', '—')} кг</b>\n"
        f"🥗 Питание: {diet_titles.get(user.get('diet'), 'Обычное')}\n"
        f"👶 Режим: {fam_str}\n🛡 Аллергии: {user.get('allergies', 'Нет')}",
        reply_markup=kb
    )

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
        date_str = f"🗓 <b>Прогноз цели:</b> к {(datetime.now() + timedelta(weeks=weeks)).strftime('%d.%m.%Y')} (~{int(weeks)} нед.)\n"
    elif goal == "gain" and target > weight:
        goal_text = "📈 Набор массы"
        weeks = (target - weight) / 0.4
        date_str = f"🗓 <b>Прогноз цели:</b> к {(datetime.now() + timedelta(weeks=weeks)).strftime('%d.%m.%Y')} (~{int(weeks)} нед.)\n"

    await message.answer(
        "🎯 <b>ТВОЯ ДНЕВНАЯ НОРМА</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"Текущая цель: <b>{goal_text}</b>\n\n"
        f"🔥 Калории: <b>{user.get('calories', 2000)} ккал</b>\n"
        f"🥩 Белки: {user.get('protein', 100)} г\n🥑 Жиры: {user.get('fat', 70)} г\n🍚 Углеводы: {user.get('carbs', 200)} г\n\n"
        f"{date_str}🛡 Ограничения: {user.get('allergies', 'Нет')}\n━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Именно этот баланс позволит тебе достичь результата комфортно и без срывов! ✨</i>"
    )

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
    except: await wait_msg.edit_text("Не удалось рассчитать лакомство. Попробуй описать точнее.")

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
    fav_w = user.get("favorite_workout_name") if user else None
    gender = user.get("gender", "F") if user else "F" # По умолчанию женские, если пол не найден
    
    intro = f"🏋️ <b>ТРЕНИРОВКИ</b>\n<i>Часто выбираешь: {fav_w}</i>" if fav_w else "🏋️ <b>ТРЕНИРОВКИ И АКТИВНОСТЬ</b>\nВыбери фокус-зону на сегодня:"
    
    # Формируем кнопки в зависимости от пола!
    if gender == "M":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛡 Широкая спина и плечи", callback_data="gen_workout_gym_back")],
            [InlineKeyboardButton(text="💪 Мощные руки и грудь", callback_data="gen_workout_gym_arms")],
            [InlineKeyboardButton(text="🧱 Рельефный пресс и кор", callback_data="gen_workout_home_abs")],
            [InlineKeyboardButton(text="🏋️ База (Всё тело)", callback_data="gen_workout_gym_full")],
            [InlineKeyboardButton(text="👣 Своя активность / Шаги", callback_data="enter_custom_activity")]
        ])
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🍑 Ягодицы и бёдра", callback_data="gen_workout_home_glutes")],
            [InlineKeyboardButton(text="🧘‍♀️ Здоровая спина и осанка", callback_data="gen_workout_home_back")],
            [InlineKeyboardButton(text="👙 Плоский живот и талия", callback_data="gen_workout_home_abs")],
            [InlineKeyboardButton(text="🔥 Жиросжигание (Всё тело)", callback_data="gen_workout_home_full")],
            [InlineKeyboardButton(text="👣 Своя активность / Шаги", callback_data="enter_custom_activity")]
        ])
        
    await message.answer(intro, reply_markup=kb)
@dp.callback_query(F.data == "enter_custom_activity")
async def enter_activity_callback(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ActivityStates.waiting_for_activity)
    await callback.message.edit_text("👣 <b>Введи свою активность:</b>\nНапример: <i>«Прошла 12 000 шагов»</i> или <i>«Плавание 45 минут»</i>.")

@dp.callback_query(F.data.startswith("gen_workout_"))
async def generate_workout_callback(callback: CallbackQuery):
    p = callback.data.replace("gen_workout_", "").split("_")
    # p[0] - loc (home/gym), p[1] - focus (glutes, back, arms, abs, full)
    
    loc = "дома (с ковриком и легким весом)" if p[0] == "home" else "в тренажёрном зале"
    
    # Переводим фокус на понятный нейросети язык
    focus_map = {
        "glutes": "ягодицы и ноги (акцент на низ)",
        "back": "здоровая спина и осанка (укрепление мышечного корсета)",
        "arms": "руки, плечи и грудь (верх тела)",
        "abs": "пресс, кор и талия",
        "full": "всё тело (комплексная жиросжигающая тренировка)"
    }
    focus_str = focus_map.get(p[1], "общеукрепляющая")
    
    user = await get_user_profile(callback.from_user.id)
    gender_str = "мужчины" if user.get("gender") == "M" else "девушки"
    
    await callback.message.edit_text(f"⏳ Подбираю программу на {focus_str}...")
    
    prompt = (
        f"Составь тренировку {loc} для {gender_str}. Цель пользователя: '{user.get('goal', 'loss')}'. "
        f"ФОКУС-ЗОНА: {focus_str}.\n"
        "Тренировка должна занимать около 30-40 минут. "
        "Обязательно добавь к каждому упражнению краткое описание техники (1 предложение).\n"
        "НЕ ИСПОЛЬЗУЙ таблицы (|) и заголовки (###). Только HTML теги (<b>, <i>).\n"
        "Верни В КОНЦЕ строку: ESTIMATED_KCAL:[число]"
    )
    try:
        raw_resp = await ask_ai(prompt=prompt, model=AI_MODEL)
        kcal_m = re.search(r"ESTIMATED_KCAL:(\d+)", raw_resp)
        est_kcal = int(kcal_m.group(1)) if kcal_m else 250
        clean_t = clean_html_tags(re.sub(r"ESTIMATED_KCAL:\d+", "", raw_resp).strip())
        
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"✅ Выполнил(а) (+{est_kcal} ккал)", callback_data=f"done_workout_{est_kcal}")]])
        await callback.message.edit_text(clean_t, reply_markup=kb)
    except: await callback.message.edit_text("Не удалось составить тренировку.")
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
    except: await wait_msg.edit_text("Не удалось рассчитать активность.")

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
    fam_ctx = "Совет должен подходить для семьи с детьми (без сахара)." if user.get('family_mode') == 'kids' else ""
    prompt = (
        f"Пользователь спросил: \"{message.text}\".\nКонтекст: Цель {user.get('goal', 'loss')}, Вес {user.get('weight', 70)} кг, "
        f"Аллергии: {user.get('allergies', 'Нет')}. Съедено сегодня: {', '.join([m.get('title', '') for m in meals])}.\n{fam_ctx}\n"
        "Дай заботливый ответ нутрициолога. Используй HTML теги. Без списков с решётками (###)."
    )
    try: await wait_msg.edit_text(clean_html_tags(await ask_ai(prompt=prompt, model=AI_MODEL)))
    except: await wait_msg.edit_text("Не удалось получить ответ.")

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
    now = datetime.now()
    
    if is_new:
        tu, pu, ca = (now + timedelta(days=14)).isoformat(), None, now.isoformat()
    else:
        tu, pu, ca = eu.get("trial_until"), eu.get("premium_until"), eu.get("created_at", now.isoformat())

    ud = {
        "user_id": user_id, "gender": d["gender"], "age": d["age"], "height": d["height"], "weight": d["weight"], 
        "goal": d["goal"], "activity": d["activity"], "diet": d.get("diet", "all"), "family_mode": d.get("family_mode", "self"), 
        "allergies": allergy_text, "calories": norm["calories"], "protein": norm["protein"], "fat": norm["fat"], "carbs": norm["carbs"],
        "target_weight": tw, "trial_until": tu, "premium_until": pu, "created_at": ca
    }
    await save_user_profile(user_id, ud)
    await state.clear()
    
    bonus = "🎁 <b>Активировано 14 дней бесплатно!</b>\nТеперь пришли фото еды 📸" if is_new else "✅ <b>Норма успешно обновлена!</b> 🥗"
    text = (
        "🎯 <b>Твоя норма рассчитана!</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 Цель: {tw} кг\n🔥 {norm['calories']} ккал | Б {norm['protein']}г | Ж {norm['fat']}г | У {norm['carbs']}г\n"
        f"🛡 Аллергии: {allergy_text}\n👨‍👩‍👧‍👦 Режим семьи: {'Включен' if d.get('family_mode') == 'kids' else 'Выключен'}\n\n{bonus}"
    )
    
    if is_cb:
        await target_msg.edit_text(text)
        if is_new: await target_msg.answer("Готово! Жду фото.", reply_markup=main_menu)
    else: await target_msg.answer(text, reply_markup=main_menu)

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
        except: pass

async def send_evening_digest():
    if not db: return
    for doc in (await asyncio.to_thread(db.collection('users').get)):
        u, uid = doc.to_dict(), doc.id
        diary = await asyncio.to_thread(db.collection('diaries').document(f"{uid}_{today_str()}").get)
        if not diary.exists or not diary.to_dict().get('meals'): continue
        m = diary.to_dict().get('meals', [])
        prompt = f"Оцени день: съедено {sum(x.get('calories',0) for x in m)}/{u.get('calories', 2000)} ккал. Напиши теплый разбор. Используй HTML."
        try: await bot.send_message(chat_id=int(uid), text=f"🌙 <b>Итоги дня</b>\n━━━━━━━━━\n{clean_html_tags(await ask_ai(prompt=prompt, model=AI_MODEL))}")
        except: pass

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
            if user.get("favorite_foods"): fav_str = f"ВНИМАНИЕ! БАЗА ЛЮБИМЫХ БЛЮД:\n" + "\n".join([f"- {f['title']}: {f['calories']} ккал (Б:{f['protein']})" for f in user.get("favorite_foods")]) + "\nЕсли похоже на список, СТРОГО бери цифры оттуда!\n\n"
            res = extract_json(await ask_ai(prompt=f"{fav_str}Съедено: \"{text}\".\nРассчитай БЖУ. JSON: {{\"title\":\"\",\"calories\":0,\"protein\":0,\"fat\":0,\"carbs\":0,\"comment\":\"\"}}", model=AI_MODEL))
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
    except: await wait_msg.edit_text("Ошибка аудио.")

# =========================================================
# РЕЦЕПТЫ И ФОТО ЕДЫ — ЖИВОЕ МЫШЛЕНИЕ
# =========================================================
@dp.message(FoodStates.waiting_for_recipe)
async def recipe_handler(message: Message, state: FSMContext):
    await state.clear()
    wait_msg = await message.answer("⏳ Собираю рецепт...")
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    u = await get_user_profile(message.from_user.id) or {}
    try: await wait_msg.edit_text(clean_html_tags(await ask_ai(prompt=f"Рецепт из: {message.text}. Цель: {u.get('goal')}. Режим: {u.get('family_mode')}. HTML.", model=AI_MODEL)))
    except: await wait_msg.edit_text("Ошибка рецепта.")

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
            "Изучи изображение:\n1. ФОТО ЕДЫ — определи блюдо, ингредиенты, примерный вес в граммах.\n"
            "2. СКРИНШОТ — прочитай текст, извлеки название, порцию и КБЖУ.\nОпиши кратко. Калории пока не суммируй."
        )
        res = await ask_ai(image_base64=b64, prompt=prompt, model=AI_VISION_MODEL)
        await state.update_data(recognized_food=res, image_base64=b64)
        await wait_msg.edit_text(f"{clean_html_tags(res)}\n\nВсё верно?", reply_markup=food_keyboard())
    except: await wait_msg.edit_text("Не удалось распознать фото.")

@dp.callback_query(F.data == "food_correct")
async def food_correct_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await callback.message.edit_text("⏳ Рассчитываю КБЖУ...")
    await bot.send_chat_action(chat_id=callback.message.chat.id, action="typing")
    u = await get_user_profile(callback.from_user.id) or {}
    fav_str = f"ВНИМАНИЕ! БАЗА:\n" + "\n".join([f"- {f['title']}: {f['calories']} ккал" for f in u.get("favorite_foods", [])]) + "\n" if u.get("favorite_foods") else ""
    try:
        food_data = extract_json(await ask_ai(prompt=f"{fav_str}Рассчитай БЖУ:\n{data.get('recognized_food')}\nJSON: {{\"title\":\"\",\"protein\":0,\"fat\":0,\"carbs\":0,\"comment\":\"\"}}", model=AI_MODEL))
        await state.update_data(calculated_food=food_data)
        txt = f"🍽 <b>{food_data['title']}</b>\n━━━━━━━━━\n🔥 <b>{food_data['calories']} ккал</b>\nБ: {food_data['protein']}г | Ж: {food_data['fat']}г | У: {food_data['carbs']}г\n\n💬 <i>{food_data.get('comment','')}</i>\nВнести в дневник?"
        await callback.message.edit_text(txt, reply_markup=result_keyboard())
    except: await callback.message.edit_text("Ошибка расчета.")

@dp.callback_query(F.data == "food_edit")
async def food_edit_handler(callback: CallbackQuery, state: FSMContext):
    await state.set_state(FoodStates.correcting)
    await callback.message.edit_text("Напиши, что исправить (например: курицы 250г, а не 150г):")

@dp.message(FoodStates.correcting)
async def correcting_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    res = await ask_ai(prompt=f"Старое: {data.get('recognized_food')}\nИсправление: {message.text}\nВыдай новое.", model=AI_MODEL)
    await state.update_data(recognized_food=res)
    await state.set_state(None)
    await message.answer(f"{clean_html_tags(res)}\n\nТеперь всё верно?", reply_markup=food_keyboard())

@dp.callback_query(F.data == "meal_save")
async def save_meal_handler(callback: CallbackQuery, state: FSMContext):
    food = (await state.get_data()).get("calculated_food", {})
    await add_meal_to_today(callback.from_user.id, {"title": food.get("title", "Еда"), "calories": food.get("calories", 0), "protein": food.get("protein", 0), "fat": food.get("fat", 0), "carbs": food.get("carbs", 0), "created_at": datetime.now().isoformat()})
    await state.clear()
    await callback.message.edit_text("✅ Сохранено в дневник.")
    await send_today(callback.message, user_id=callback.from_user.id)

@dp.callback_query(F.data == "food_remember")
async def food_remember_handler(callback: CallbackQuery, state: FSMContext):
    food = (await state.get_data()).get("calculated_food", {})
    if not food: return await callback.answer("Ошибка данных.")
    if db:
        doc_ref = db.collection('users').document(str(callback.from_user.id))
        doc = await asyncio.to_thread(doc_ref.get)
        favs = doc.to_dict().get('favorite_foods', []) if doc.exists else []
        favs.append({"title": food.get("title", "Еда"), "calories": food.get("calories", 0), "protein": food.get("protein", 0), "fat": food.get("fat", 0), "carbs": food.get("carbs", 0)})
        await asyncio.to_thread(doc_ref.set, {'favorite_foods': favs[-20:]}, merge=True)
    await callback.answer(f"❤️ {food.get('title')} сохранено в твою базу!")
    await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📊 Сохранить в дневник", callback_data="meal_save")], [InlineKeyboardButton(text="🗑 Удалить", callback_data="food_delete")]]))

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
# ЗАПУСК СЕРВЕРА
# =========================================================
async def health_handler(request: web.Request): return web.json_response({"status": "ok"})
async def bepaid_webhook_handler(request: web.Request): return web.Response(text="OK", status=200)

async def main():
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_post("/webhook/bepaid", bepaid_webhook_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000"))).start()
    
    await set_bot_description(bot)
    await set_bot_commands(bot)
    
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(send_morning_digest, "cron", hour=9, minute=0)
    scheduler.add_job(send_evening_digest, "cron", hour=21, minute=0)
    scheduler.start()

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
