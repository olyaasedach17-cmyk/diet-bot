import asyncio
import os
import base64
from dotenv import load_dotenv
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from openai import AsyncOpenAI

load_dotenv()
TOKEN = os.getenv('BOT_TOKEN')
client = AsyncOpenAI(api_key=os.getenv('AI_API_KEY'), base_url=os.getenv('AI_BASE_URL'))

bot = Bot(token=TOKEN)
dp = Dispatcher()

class BotStates(StatesGroup):
    waiting_for_status = State()
    waiting_for_user_params = State()
    waiting_for_menu_ingredients = State()

main_menu = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="🍎 Составить меню")]],
    resize_keyboard=True,
    input_field_placeholder="Отправь фото еды или нажми кнопку..."
)

status_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="🥩 Это сырые продукты", callback_data="status_raw"),
        InlineKeyboardButton(text="🍳 Это готовое блюдо", callback_data="status_cooked")
    ]
])

SYSTEM_PROMPT = """Ты — точный и эмпатичный ИИ-нутрициолог. 
ЕСЛИ СЧИТАЕШЬ ФОТО: Тебе передадут статус (Сырое/Готовое). Учитывай уварку/ужарку (крупы тяжелеют в 3 раза, мясо теряет 20-30%). Выдавай список продуктов с весом и ИТОГО КБЖУ.
ЕСЛИ ПИШЕШЬ МЕНЮ: Пользователь передаст свои параметры (вес, рост, возраст) и список продуктов. 
1. Сначала рассчитай его суточную норму калорий.
2. Затем составь меню на эту норму из указанных продуктов (рецепты до 15 мин). 
3. Если просят вписать сладости — вписывай. Пиши граммовки и итоговое КБЖУ."""

async def ask_ai(image_base64=None, text_prompt=None, context=""):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if image_base64:
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": f"Контекст: {context}. Оцени КБЖУ этой тарелки."},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
            ]
        })
    elif text_prompt:
        messages.append({"role": "user", "content": f"Запрос: {text_prompt}"})

    response = await client.chat.completions.create(
        model="gpt-4o", messages=messages, temperature=0.4 
    )
    return response.choices[0].message.content

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer("Привет! 👋\n\nПришли мне фото еды, чтобы узнать КБЖУ, или нажми кнопку внизу, чтобы составить рацион из того, что есть в холодильнике!", reply_markup=main_menu)

@dp.message(F.photo)
async def handle_photo(message: Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    file = await bot.get_file(photo_id)
    downloaded_file = await bot.download_file(file.file_path)
    image_base64 = base64.b64encode(downloaded_file.read()).decode('utf-8')
    await state.update_data(saved_photo=image_base64)
    await state.set_state(BotStates.waiting_for_status)
    await message.answer("Супер! Уточни только один момент:", reply_markup=status_keyboard)

@dp.callback_query(BotStates.waiting_for_status, F.data.in_(["status_raw", "status_cooked"]))
async def process_photo_status(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.edit_text("⏳ Считаю калории с учетом твоего выбора...")
    user_data = await state.get_data()
    image_base64 = user_data.get("saved_photo")
    status_text = "ЭТО СЫРЫЕ ПРОДУКТЫ ДО ГОТОВКИ" if callback.data == "status_raw" else "ЭТО УЖЕ ГОТОВОЕ БЛЮДО"
    try:
        ai_response = await ask_ai(image_base64=image_base64, context=status_text)
        await callback.message.edit_text(ai_response)
    except Exception as e:
        await callback.message.edit_text(f"Ошибка ИИ: {e}")
    finally:
        await state.clear()

@dp.message(F.text == "🍎 Составить меню")
async def handle_menu_btn(message: Message, state: FSMContext):
    await message.answer("Отличная идея! 🍲\n\nЧтобы я подобрал идеальные порции, мне нужно знать твою норму калорий. Напиши свой **вес, рост и возраст**.")
    await state.set_state(BotStates.waiting_for_user_params)

@dp.message(BotStates.waiting_for_user_params)
async def process_user_params(message: Message, state: FSMContext):
    await state.update_data(user_params=message.text)
    await message.answer("Принято! 📝\n\nА теперь напиши, какие продукты у тебя есть в холодильнике? И какую вкусняшку мне обязательно вписать в рацион?")
    await state.set_state(BotStates.waiting_for_menu_ingredients)

@dp.message(BotStates.waiting_for_menu_ingredients)
async def process_menu_generation(message: Message, state: FSMContext):
    msg = await message.answer("🍳 Считаю норму и придумываю вкусное меню...")
    user_data = await state.get_data()
    params = user_data.get("user_params")
    ingredients = message.text
    full_prompt = f"Параметры пользователя: {params}. Продукты и пожелания: {ingredients}. Рассчитай норму и составь меню."
    try:
        ai_response = await ask_ai(text_prompt=full_prompt)
        await msg.edit_text(ai_response)
    except Exception as e:
        await msg.edit_text(f"Ошибка ИИ: {e}")
    finally:
        await state.clear()

# --- СЕРВЕРНАЯ ЧАСТЬ ДЛЯ RENDER ---
async def health_check(request):
    return web.Response(text="Bot is running!")

async def main():
    # Запускаем сайт-обманку
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    
    # Запускаем бота
    print("Бот запущен на сервере!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())