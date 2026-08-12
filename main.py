import asyncio
import base64
import io
import json
import logging
import os
import re
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler

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
        [KeyboardButton(text="📊 Сегодня"), KeyboardButton(text="🥗 Что приготовить")],
        [KeyboardButton(text="⚖️ Вес"), KeyboardButton(text="👤 Профиль")]
    ],
    resize_keyboard=True,
    input_field_placeholder="Пришли фото еды...",
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
    return doc.to_dict() if doc.exists else None

async def save_user_profile(user_id: int, data: dict):
    if db:
        await asyncio.to_thread(db.collection('users').document(str(user_id)).set, data, merge=True)

async def get_today_meals(user_id: int) -> list:
    if not db:
        return []
    doc_id = f"{user_id}_{today_str()}"
    doc = await asyncio.to_thread(db.collection('diaries').document(doc_id).get)
    if doc.exists:
        return doc.to_dict().get('meals', [])
    return []

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
                {"role": "system", "content": "Ты нутрициолог. Отвечай кратко на русском языке."},
                {"role": "user", "content": user_content},
            ],
            temperature=0.2,
        )
        return response.choices[0].message.content or "" if response.choices else ""
    except Exception as e:
        logger.error(f"🔥 Ошибка AI ({used_model}): {e}")
        raise

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

async def send_today(message: Message):
    user = await get_user_profile(message.from_user.id)
    if not user:
        await message.answer("Сначала нажмите /start.")
        return
        
    meals = await get_today_meals(message.from_user.id)
    
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
        
    # Формируем логику перебора / остатка калорий
    if total_kcal > norm_kcal:
        overage_kcal = total_kcal - norm_kcal
        status_text = (
            f"⚠️ Перебор: <b>+{overage_kcal} ккал</b>\n\n"
            f"💬 <i>Сегодня мы немного перебрали норму, но это абсолютно нормально! Однодневный профицит не испортит твой прогресс. Главное — продолжить держать ритм завтра, не урезая рацион 💪</i>"
        )
    else:
        rem_kcal = norm_kcal - total_kcal
        status_text = f"Осталось на сегодня: <b>{rem_kcal} ккал</b>"

    text = (
        f"{meals_text}"
        f"🔥 <b>Калории</b>  <b>{total_kcal}</b> / {norm_kcal} ккал ({pct_kcal}%)\n"
        f"<code>{make_progress_bar(total_kcal, norm_kcal)}</code>\n\n"
        f"🥩 <b>Белки</b>  <b>{total_p}</b> / {norm_p} г\n"
        f"<code>{make_progress_bar(total_p, norm_p)}</code>\n\n"
        f"🥑 <b>Жиры</b>  <b>{total_f}</b> / {norm_f} г\n"
        f"<code>{make_progress_bar(total_f, norm_f)}</code>\n\n"
        f"🍚 <b>Углеводы</b>  <b>{total_c}</b> / {norm_c} г\n"
        f"<code>{make_progress_bar(total_c, norm_c)}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{status_text}"
    )
    await message.answer(text)

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
        
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👨 Мужчина", callback_data="gender_M")],
            [InlineKeyboardButton(text="👩 Женщина", callback_data="gender_F")],
        ]
    )
    await message.answer("Привет! Я «Умная Тарелка» 🥗\n\nДля начала выбери пол:", reply_markup=keyboard)

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
        "Теперь пришли фотографию еды 📸"
    )
    await callback.message.answer("Готово! Пришли фото еды.", reply_markup=main_menu)
    await callback.answer()

# =========================================================
# ВЕС И ДИНАМИКА ПРИРОСТА/ПОХУДЕНИЯ
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
        f"Твой текущий вес в системе: <b>{curr_weight} кг</b>\n\n"
        "Напиши свой новый вес в кг (например: <code>74.5</code>):"
    )

@dp.message(WeightStates.waiting_for_weight)
async def process_weight_update(message: Message, state: FSMContext):
    numbers = re.findall(r"\d+(?:[.,]\d+)?", message.text)
    if not numbers:
        await message.answer("Пожалуйста, напиши вес числом (например: 72.3).")
        return
        
    new_weight = float(numbers[0].replace(",", "."))
    user = await get_user_profile(message.from_user.id)
    old_weight = float(user.get("weight", new_weight))
    goal = user.get("goal", "maintain")
    
    diff = round(new_weight - old_weight, 1)
    
    # Пересчитываем нормы с учетом нового веса
    new_norm = calculate_norm(
        gender=user.get("gender", "M"),
        age=user.get("age", 25),
        height=user.get("height", 170),
        weight=new_weight,
        goal=goal,
        activity=user.get("activity", "low")
    )
    
    # Реакция бота в зависимости от цели
    if goal == "loss":
        if diff < 0:
            praise = f"🎉 <b>Супер результат!</b> Минус <b>{abs(diff)} кг</b>! Ты отличный молодец, продолжаем в том же духе! 🔥"
        elif diff > 0:
            praise = f"⚖️ Вес показывает +<b>{diff} кг</b>. Не переживай! Это может быть обычная задержка воды или отёк. Главное — продолжать держать ритм 💪"
        else:
            praise = "⚖️ Вес остался прежним. Стабильность — тоже шаг вперед, организм адаптируется!"
    elif goal == "gain":
        if diff > 0:
            praise = f"🎉 <b>Отличный прогресс!</b> Плюс <b>{diff} кг</b> в копилку! Масса растёт, так держать! 🏋️‍♂️"
        elif diff < 0:
            praise = f"⚖️ Вес снизился на <b>{abs(diff)} кг</b>. Добавь немного больше сложных углеводов или белков в рацион!"
        else:
            praise = "⚖️ Вес держится на месте. Отличная стабильность!"
    else: # maintain
        if abs(diff) <= 0.5:
            praise = "🎉 <b>Идеально!</b> Вес отлично удерживается в целевой зоне!"
        else:
            praise = f"⚖️ Вес изменился на <b>{diff} кг</b>. Зафиксировали новые данные!"

    # Сохраняем обновленный вес и нормы в БД
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
        f"📊 <b>Данные обновлены:</b>\n"
        f"• Новый вес: <b>{new_weight} кг</b>\n"
        f"• Новая норма: <b>{new_norm['calories']} ккал</b> (Б {new_norm['protein']}г · Ж {new_norm['fat']}г · У {new_norm['carbs']}г)"
    )
    await message.answer(response_text)

# =========================================================
# ФОТО ЕДЫ
# =========================================================
@dp.message(F.photo)
async def photo_handler(message: Message, state: FSMContext):
    user = await get_user_profile(message.from_user.id)
    if not user:
        await message.answer("Сначала пройди короткую настройку: /start")
        return
        
    wait_message = await message.answer("👀 Анализирую фотографию...")
    
    try:
        telegram_file = await bot.get_file(message.photo[-1].file_id)
        buffer = io.BytesIO()
        await bot.download_file(telegram_file.file_path, destination=buffer)
        image_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
        
        result = await ask_ai(
            image_base64=image_base64,
            prompt=(
                "Определи еду на фотографии. Укажи название блюда, ингредиенты "
                "и примерный вес порции. Калории пока не считай."
            ),
            model=AI_VISION_MODEL
        )
        
        await state.update_data(recognized_food=result, image_base64=image_base64)
        clean_result = clean_html_tags(result)
        await wait_message.edit_text(f"{clean_result}\n\nВсё верно?", reply_markup=food_keyboard())
    except Exception as e:
        logger.exception("Ошибка обработки фотографии")
        await wait_message.edit_text("Не удалось обработать фотографию. Проверь настройки AI и попробуй ещё раз.")

@dp.callback_query(F.data == "food_correct")
async def food_correct_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    food_description = data.get("recognized_food", "")
    await callback.message.edit_text("⏳ Рассчитываю калории и БЖУ...")
    
    try:
        result = await ask_ai(
            prompt=(
                "Рассчитай калории и БЖУ блюда.\n\n"
                f"Описание блюда:\n{food_description}\n\n"
                "Верни только JSON без комментариев и markdown блоков:\n"
                "{\n"
                '  "title": "название блюда",\n'
                '  "calories": 0,\n'
                '  "protein": 0,\n'
                '  "fat": 0,\n'
                '  "carbs": 0,\n'
                '  "weight": 0,\n'
                '  "comment": "короткий совет"\n'
                "}"
            ),
            model=AI_MODEL
        )
        food_data = extract_json(result)
        await state.update_data(calculated_food=food_data)
        
        title = clean_html_tags(str(food_data.get("title", "Блюдо")))
        comment = clean_html_tags(str(food_data.get("comment", "")))
        
        text = (
            f"🍽 <b>{title}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🔥 Калории: <b>{food_data.get('calories', 0)} ккал</b>\n"
            f"🥩 Белки: {food_data.get('protein', 0)} г\n"
            f"🥑 Жиры: {food_data.get('fat', 0)} г\n"
            f"🍚 Углеводы: {food_data.get('carbs', 0)} г\n"
            f"⚖️ Вес: {food_data.get('weight', 0)} г\n\n"
            f"💬 {comment}"
        )
        await callback.message.edit_text(text, reply_markup=result_keyboard())
    except Exception:
        logger.exception("Ошибка расчёта еды")
        await callback.message.edit_text("Не удалось рассчитать блюдо. Попробуй ещё раз.")
    
    await callback.answer()

@dp.callback_query(F.data == "food_edit")
async def food_edit_handler(callback: CallbackQuery, state: FSMContext):
    await state.set_state(FoodStates.correcting)
    await callback.message.edit_text("Напиши, что нужно исправить.\n\nНапример: курицы 250 г, а не 150 г.")
    await callback.answer()

@dp.message(FoodStates.correcting)
async def correcting_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    old_description = data.get("recognized_food", "")
    
    result = await ask_ai(
        prompt=(
            "Исправь описание блюда.\n\n"
            f"Старое описание:\n{old_description}\n\n"
            f"Исправление пользователя:\n{message.text}\n\n"
            "Верни краткое новое описание блюда с ингредиентами и весом."
        ),
        model=AI_MODEL
    )
    
    await state.update_data(recognized_food=result)
    await state.set_state(None)
    clean_result = clean_html_tags(result)
    await message.answer(f"{clean_result}\n\nТеперь всё верно?", reply_markup=food_keyboard())

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
    await send_today(callback.message)
    await callback.answer()

@dp.callback_query(F.data == "food_delete")
async def delete_food_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🗑 Запись удалена.")
    await callback.answer()

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
        "🎯 <b>Твоя дневная норма</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🔥 {user.get('calories', 2000)} ккал\n"
        f"🥩 Белки: {user.get('protein', 100)} г\n"
        f"🥑 Жиры: {user.get('fat', 70)} г\n"
        f"🍚 Углеводы: {user.get('carbs', 200)} г"
    )

@dp.message(F.text == "🥗 Что приготовить")
@dp.message(Command("fridge"))
async def fridge_handler(message: Message, state: FSMContext):
    await state.set_state(FoodStates.waiting_for_recipe)
    await message.answer("Напиши продукты через запятую.\n\nНапример: курица, рис, огурцы, яйца.")

@dp.message(FoodStates.waiting_for_recipe)
async def recipe_handler(message: Message, state: FSMContext):
    await state.clear()
    wait_message = await message.answer("⏳ Собираю рецепт...")
    
    try:
        result = await ask_ai(
            prompt=(
                "Составь простой рецепт из продуктов:\n"
                f"{message.text}\n\n"
                "Укажи ингредиенты, приготовление, калории и БЖУ."
            ),
            model=AI_MODEL
        )
        clean_result = clean_html_tags(result)
        await wait_message.edit_text(clean_result)
    except Exception:
        logger.exception("Ошибка создания рецепта")
        await wait_message.edit_text("Не удалось составить рецепт.")

@dp.message(F.text == "👤 Профиль")
@dp.message(Command("profile"))
async def profile_handler(message: Message):
    await plan_handler(message)

@dp.message(F.text == "❓ Помощь")
@dp.message(Command("help"))
async def help_handler(message: Message):
    await message.answer(
        "📸 Пришли фотографию еды — я распознаю блюдо и рассчитаю калории и БЖУ.\n\n"
        "Команды:\n"
        "/start — начать\n"
        "/today — дневник\n"
        "/weight — обновить вес\n"
        "/plan — дневная норма\n"
        "/fridge — рецепт из продуктов"
    )
    @dp.message(Command("test_morning"))
async def test_morning_handler(message: Message):
    if message.from_user.id:
        await send_morning_digest()

# =========================================================
# УТРЕННЯЯ РАССЫЛКА (РАСПИСАНИЕ)
# =========================================================
def get_russian_date_str() -> str:
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    months = ["янв.", "февр.", "марта", "апр.", "мая", "июня", "июля", "авг.", "сент.", "окт.", "нояб.", "дек."]
    now = datetime.now()
    day_name = days[now.weekday()]
    month_name = months[now.month - 1]
    return f"{day_name}, {now.day} {month_name}"

async def send_morning_digest():
    """Отправка утреннего плана и совета каждому пользователю"""
    if not db:
        logger.warning("Firebase не подключен, утренняя рассылка пропущена.")
        return

    logger.info("🌅 Запуск утренней рассылки...")
    users_docs = await asyncio.to_thread(db.collection('users').get)
    date_str = get_russian_date_str()

    for doc in users_docs:
        user_data = doc.to_dict()
        user_id = user_data.get("user_id") or doc.id
        
        norm_kcal = user_data.get("calories", 2000)
        norm_p = user_data.get("protein", 100)
        goal = user_data.get("goal", "maintain")
        weight = user_data.get("weight", "")

        # Запрашиваем у ИИ краткий утренний фокус/совет
        prompt = (
            f"Составь 1-2 коротких мотивационных предложения на утро для человека с целью '{goal}'. "
            f"Посоветуй, с чего лучше начать завтрак, чтобы набрать белок (норма {norm_p}г). "
            "Пиши тепло, без официоза."
        )
        
        try:
            focus_advice = await ask_ai(prompt=prompt, model=AI_MODEL)
            focus_clean = clean_html_tags(focus_advice)
        except Exception:
            focus_clean = f"Начни день с сытного завтрака с высокими белками (~25–30 г белка), чтобы сразу зарядиться энергией!"

        target_weight_str = f" приближает к цели!" if not weight else f" приближает к {weight} кг."

        text = (
            f"Доброе утро! Сегодня {date_str} ☀️\n\n"
            f"План на день: <b>{norm_kcal} ккал</b>, <b>Б {norm_p} г</b>.\n\n"
            f"<b>Фокус на сегодня:</b> {focus_clean}\n\n"
            f"<i>Неспешно, но уверенно — маленький шаг сегодня{target_weight_str}</i>"
        )

        try:
            await bot.send_message(chat_id=int(user_id), text=text)
            await asyncio.sleep(0.1) # Задержка, чтобы Telegram не забанил за спам
        except Exception as e:
            logger.error(f"Не удалось отправить утреннее сообщение пользователю {user_id}: {e}")

# =========================================================
# ЗАПУСК
# =========================================================
async def main():
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    
    try:
        await site.start()
        logger.info("HTTP-сервер запущен на порту %s", port)
        
        bot_info = await bot.get_me()
        logger.info("Telegram подключен: @%s", bot_info.username)
        
        # ⏰ НАСТРОЙКА И ЗАПУСК ПЛАНИРОВЩИКА (в 09:00 по Москве)
        scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
        scheduler.add_job(send_morning_digest, "cron", hour=9, minute=0)
        scheduler.start()
        logger.info("⏰ Планировщик утренней рассылки запущен (09:00 MSK)")

        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Polling запущен")
        
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except Exception:
        logger.exception("Критическая ошибка запуска")
        raise
    finally:
        await bot.session.close()
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
