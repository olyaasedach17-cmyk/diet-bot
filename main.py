import asyncio
import base64
import html
import io
import json
import logging
import os
import re
import sqlite3
from datetime import datetime

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

# =========================================================
# НАСТРОЙКИ
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
AI_MODEL = os.getenv("AI_MODEL")

if not BOT_TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN или TELEGRAM_BOT_TOKEN")

if not AI_API_KEY:
    raise RuntimeError("Не найден AI_API_KEY или POLZA_API_KEY")

if not AI_BASE_URL:
    raise RuntimeError("Не найден AI_BASE_URL или POLZA_BASE_URL")

if not AI_MODEL:
    raise RuntimeError("Не найден AI_MODEL. Укажите точное название модели из Polza.")

logger.info("AI_BASE_URL: %s", AI_BASE_URL)
logger.info("AI_MODEL: %s", AI_MODEL)

# =========================================================
# БАЗА ДАННЫХ SQLITE
# =========================================================
DB_PATH = os.getenv("DB_PATH", "food_bot.db")

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        gender TEXT,
        age INTEGER,
        height REAL,
        weight REAL,
        goal TEXT,
        activity TEXT,
        calories INTEGER DEFAULT 2000,
        protein INTEGER DEFAULT 100,
        fat INTEGER DEFAULT 70,
        carbs INTEGER DEFAULT 200,
        created_at TEXT
    )
""")

db.execute("""
    CREATE TABLE IF NOT EXISTS meals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        meal_date TEXT NOT NULL,
        title TEXT NOT NULL,
        calories INTEGER DEFAULT 0,
        protein INTEGER DEFAULT 0,
        fat INTEGER DEFAULT 0,
        carbs INTEGER DEFAULT 0,
        created_at TEXT
    )
""")
db.commit()

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
# TELEGRAM
# =========================================================
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

dp = Dispatcher()

main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Сегодня"), KeyboardButton(text="🥗 Что приготовить")],
        [KeyboardButton(text="🎯 Моя норма"), KeyboardButton(text="❓ Помощь")]
    ],
    resize_keyboard=True,
    input_field_placeholder="Пришли фото еды",
)

# =========================================================
# СОСТОЯНИЯ
# =========================================================
class Onboarding(StatesGroup):
    stats = State()

class FoodStates(StatesGroup):
    correcting = State()
    waiting_for_recipe = State()

# =========================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =========================================================
def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")

def get_user(user_id: int):
    row = db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row) if row else None

def calculate_norm(gender: str, age: int, height: float, weight: float, goal: str, activity: str) -> dict:
    if gender == "M":
        bmr = 10 * weight + 6.25 * height - 5 * age + 5
    else:
        bmr = 10 * weight + 6.25 * height - 5 * age - 161
        
    activity_coefficients = {
        "low": 1.2,
        "light": 1.375,
        "medium": 1.55,
        "high": 1.725,
    }

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

def get_day_totals(user_id: int) -> dict:
    row = db.execute("""
        SELECT COALESCE(SUM(calories), 0) AS calories,
               COALESCE(SUM(protein), 0) AS protein,
               COALESCE(SUM(fat), 0) AS fat,
               COALESCE(SUM(carbs), 0) AS carbs
        FROM meals WHERE user_id = ? AND meal_date = ?
    """, (user_id, today())).fetchone()
    
    return dict(row)

def extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^json\s|\sCode$", "", text, flags=re.IGNORECASE)
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("AI не вернул JSON")
    return json.loads(match.group(0))

async def ask_ai(prompt: str, image_base64: str | None = None) -> str:
    if image_base64:
        user_content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}},
        ]
    else:
        user_content = prompt
        
    try:
        response = await ai_client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": "Ты помощник по питанию. Отвечай на русском языке."},
                {"role": "user", "content": user_content},
            ],
        )
        if not response.choices:
            return ""
        return response.choices[0].message.content or ""
    except Exception:
        logger.exception("Ошибка обращения к AI")
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
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала нажмите /start.")
        return
        
    totals = get_day_totals(message.from_user.id)
    meals = db.execute("SELECT * FROM meals WHERE user_id = ? AND meal_date = ? ORDER BY id", (message.from_user.id, today())).fetchall()
    
    meals_text = ""
    for meal in meals:
        meals_text += (
            f"🍽 <b>{html.escape(meal['title'])}</b>\n"
            f"🔥 {meal['calories']} ккал\n"
            f"Б {meal['protein']} г · Ж {meal['fat']} г · У {meal['carbs']} г\n\n"
        )
        
    if not meals_text:
        meals_text = "Пока ничего не записано.\n\n"
        
    remaining = max(user["calories"] - totals["calories"], 0)
    text = (
        "📊 <b>ДНЕВНИК ЗА СЕГОДНЯ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{meals_text}"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🔥 Съедено: <b>{totals['calories']} ккал</b>\n"
        f"🎯 Норма: <b>{user['calories']} ккал</b>\n"
        f"Осталось: <b>{remaining} ккал</b>\n\n"
        f"🥩 Белки: {totals['protein']} / {user['protein']} г\n"
        f"🥑 Жиры: {totals['fat']} / {user['fat']} г\n"
        f"🍚 Углеводы: {totals['carbs']} / {user['carbs']} г"
    )
    await message.answer(text)

# =========================================================
# ОНБОРДИНГ
# =========================================================
@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    await state.clear()
    user = get_user(message.from_user.id)
    if user:
        await message.answer("С возвращением! Пришли фото еды 📸", reply_markup=main_menu)
        return
        
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👨 Мужчина", callback_data="gender_M")],
            [InlineKeyboardButton(text="👩 Женщина", callback_data="gender_F")],
        ]
    )
    await message.answer("Привет! Я помогу вести дневник питания 🥗\n\nДля начала выбери пол:", reply_markup=keyboard)

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
    
    db.execute("""
        INSERT OR REPLACE INTO users (
            user_id, gender, age, height, weight, goal, activity,
            calories, protein, fat, carbs, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        callback.from_user.id, data["gender"], data["age"], data["height"], data["weight"],
        data["goal"], activity, norm["calories"], norm["protein"], norm["fat"], norm["carbs"],
        datetime.now().isoformat(),
    ))
    db.commit()
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
    await callback.message.answer("Готово! Я буду считать рацион и вести дневник.", reply_markup=main_menu)
    await callback.answer()

# =========================================================
# ФОТО ЕДЫ
# =========================================================
@dp.message(F.photo)
async def photo_handler(message: Message, state: FSMContext):
    if not get_user(message.from_user.id):
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
        )
        
        await state.update_data(recognized_food=result, image_base64=image_base64)
        safe_result = html.escape(result)
        await wait_message.edit_text(f"{safe_result}\n\nВсё верно?", reply_markup=food_keyboard())
    except Exception:
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
                "Верни только JSON без комментариев:\n"
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
        )
        food_data = extract_json(result)
        await state.update_data(calculated_food=food_data)
        
        title = html.escape(str(food_data.get("title", "Блюдо")))
        comment = html.escape(str(food_data.get("comment", "")))
        
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
        await callback.message.edit_text("Не удалось рассчитать блюдо. Попробуй ещё раз или проверь AI_MODEL.")
    
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
    )
    
    await state.update_data(recognized_food=result)
    await state.set_state(None)
    await message.answer(f"{html.escape(result)}\n\nТеперь всё верно?", reply_markup=food_keyboard())

@dp.callback_query(F.data == "meal_save")
async def save_meal_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    food = data.get("calculated_food", {})
    
    title = str(food.get("title", "Приём пищи"))
    calories = int(food.get("calories", 0) or 0)
    protein = int(food.get("protein", 0) or 0)
    fat = int(food.get("fat", 0) or 0)
    carbs = int(food.get("carbs", 0) or 0)
    
    db.execute("""
        INSERT INTO meals (user_id, meal_date, title, calories, protein, fat, carbs, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        callback.from_user.id, today(), title, calories, protein, fat, carbs, datetime.now().isoformat()
    ))
    db.commit()
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
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала нажми /start.")
        return
        
    await message.answer(
        "🎯 <b>Твоя дневная норма</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🔥 {user['calories']} ккал\n"
        f"🥩 Белки: {user['protein']} г\n"
        f"🥑 Жиры: {user['fat']} г\n"
        f"🍚 Углеводы: {user['carbs']} г"
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
        )
        await wait_message.edit_text(html.escape(result))
    except Exception:
        logger.exception("Ошибка создания рецепта")
        await wait_message.edit_text("Не удалось составить рецепт.")

@dp.message(F.text == "❓ Помощь")
@dp.message(Command("help"))
async def help_handler(message: Message):
    await message.answer(
        "📸 Пришли фотографию еды — я распознаю блюдо и рассчитаю калории и БЖУ.\n\n"
        "Команды:\n"
        "/start — начать\n"
        "/today — дневник\n"
        "/plan — дневная норма\n"
        "/fridge — рецепт из продуктов"
    )

# =========================================================
# HEALTH CHECK ДЛЯ RENDER
# =========================================================
async def health_handler(request: web.Request):
    return web.json_response({
        "status": "ok",
        "service": "food-telegram-bot",
    })

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
        
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Polling запущен")
        
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except Exception:
        logger.exception("Критическая ошибка запуска")
        raise
    finally:
        await bot.session.close()
        await runner.cleanup()
        db.close()

if __name__ == "__main__":
    asyncio.run(main())
