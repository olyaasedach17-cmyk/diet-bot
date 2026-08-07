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
    waiting_for_goal = State()
    waiting_for_activity = State()
    waiting_for_menu_ingredients = State()
    waiting_for_extra_permission = State()

main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🍎 Составить меню")],
        [KeyboardButton(text="❌ Сбросить шаг / Начать заново")]
    ],
    resize_keyboard=True,
    input_field_placeholder="Отправь фото или выбери действие..."
)

status_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="🥩 Это сырые продукты", callback_data="status_raw"),
        InlineKeyboardButton(text="🍳 Это готовое блюдо", callback_data="status_cooked")
    ]
])

goal_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📉 Похудение", callback_data="goal_loss")],
    [InlineKeyboardButton(text="⚖️ Поддержание веса", callback_data="goal_maintain")],
    [InlineKeyboardButton(text="📈 Набор массы", callback_data="goal_gain")]
])

activity_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🛋 Низкая (сидячая работа, мало шагов)", callback_data="act_low")],
    [InlineKeyboardButton(text="🚶 Средняя (1-3 легких тренировки)", callback_data="act_med")],
    [InlineKeyboardButton(text="🏃 Высокая (спорт 3+ раз в неделю)", callback_data="act_high")]
])

extra_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🧑‍🍳 Добавь базу (масло, лук, специи)", callback_data="extra_yes")],
    [InlineKeyboardButton(text="🛑 СТРОГО только из моего списка", callback_data="extra_no")]
])

# --- ПРОМПТ С НОВЫМИ ПРАВИЛАМИ ДЛЯ ФОТО ---
SYSTEM_PROMPT = """Ты — профессиональный шеф-повар и точный ИИ-нутрициолог. 

ЕСЛИ СЧИТАЕШЬ ФОТО: 
1. Внимательно читай "Комментарий пользователя к фото". Если человек сам написал вес (например, "300г фарша", "сковорода 28см") — ВЕРЬ ЧЕЛОВЕКУ БЕЗОГОВОРОЧНО и строй расчеты строго на его цифрах!
2. Если человек не указал вес, ищи на фото предметы для масштаба (вилка, рука, край стола, размер конфорки), чтобы понять реальный размер.
3. Если масштаб неясен (еда снята слишком близко) — БУДЬ КОНСЕРВАТИВЕН, не завышай вес! Считай порции как в стандартном ресторане (гарнир ~150-200г, мясо ~120-150г).
4. Напиши, что видишь, укажи итоговый вес и рассчитай КБЖУ с учетом уварки/ужарки (крупы в 3 раза тяжелее, мясо теряет 25%).

ЕСЛИ ПИШЕШЬ МЕНЮ: 
Пользователь даст параметры, цель, активность и список продуктов.
1. РАСЧЕТ НОРМЫ: Рассчитай калории по Миффлину-Сан Жеору. Похудение = дефицит 20%, набор = профицит 15%. Напиши ЭТУ ЦИФРУ в начале.
2. БЛЮДА: Превращай продукты в полноценные блюда.
3. ФОРМАТ: Название блюда, Ингредиенты в граммах, Краткий рецепт, КБЖУ блюда.
4. ВКУСНЯШКА: Обязательно впиши вкусняшку, если просили.
5. ДОБАВОЧНЫЕ ПРОДУКТЫ: Если человек просит готовить СТРОГО из его списка — не добавляй масло, соль и овощи. Если разрешает базу — добавляй.
Итоговая сумма калорий должна строго совпадать с твоим расчетом из пункта 1."""

async def ask_ai(image_base64=None, text_prompt=None, context=""):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if image_base64:
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": f"Контекст: {context}. Изучи еду на фото, оцени её вес (учитывая комментарий, если он есть) и рассчитай КБЖУ."},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
            ]
        })
    elif text_prompt:
        messages.append({"role": "user", "content": f"Запрос на меню: {text_prompt}"})

    response = await client.chat.completions.create(
        model="gpt-4o", messages=messages, temperature=0.3 
    )
    return response.choices[0].message.content


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Привет! 👋\n\nПришли мне фото еды (можно добавить подпись с весом), чтобы узнать КБЖУ, или нажми кнопку внизу, чтобы составить меню!", reply_markup=main_menu)

@dp.message(F.text == "❌ Сбросить шаг / Начать заново")
async def cancel_action(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🔄 Все текущие действия отменены. Можешь отправить новое фото еды или начать составление меню заново!", reply_markup=main_menu)

# --- ИЗМЕНЕНИЯ ЗДЕСЬ: ЗАХВАТЫВАЕМ ТЕКСТ ПОДПИСИ К ФОТО ---
@dp.message(F.photo)
async def handle_photo(message: Message, state: FSMContext):
    await state.clear()
    photo_id = message.photo[-1].file_id
    file = await bot.get_file(photo_id)
    downloaded_file = await bot.download_file(file.file_path)
    image_base64 = base64.b64encode(downloaded_file.read()).decode('utf-8')
    
    # Сохраняем в память и фото, и текст под ним (если текста нет - будет пустая строка)
    photo_caption = message.caption or ""
    await state.update_data(saved_photo=image_base64, photo_caption=photo_caption)
    
    await state.set_state(BotStates.waiting_for_status)
    await message.answer("Супер! Уточни только один момент:", reply_markup=status_keyboard)

@dp.callback_query(BotStates.waiting_for_status, F.data.in_(["status_raw", "status_cooked"]))
async def process_photo_status(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.edit_text("⏳ Изучаю фото и считаю калории...")
    
    user_data = await state.get_data()
    image_base64 = user_data.get("saved_photo")
    photo_caption = user_data.get("photo_caption", "") # Достаем подпись из памяти
    
    status_text = "ЭТО СЫРЫЕ ПРОДУКТЫ ДО ГОТОВКИ" if callback.data == "status_raw" else "ЭТО УЖЕ ГОТОВОЕ БЛЮДО"
    
    # Если была подпись, добавляем её к инструкции для ИИ
    if photo_caption:
        status_text += f". Комментарий пользователя к фото: {photo_caption}"
        
    try:
        ai_response = await ask_ai(image_base64=image_base64, context=status_text)
        await callback.message.edit_text(ai_response)
    except Exception as e:
        await callback.message.edit_text(f"Ошибка ИИ: {e}")
    finally:
        await state.clear()

@dp.message(F.text == "🍎 Составить меню")
async def handle_menu_btn(message: Message, state: FSMContext):
    await message.answer("Отличная идея! 🍲\n\nНапиши свои базовые параметры: **вес, рост и возраст** (например: 65, 170, 25).")
    await state.set_state(BotStates.waiting_for_user_params)

@dp.message(BotStates.waiting_for_user_params)
async def process_user_params(message: Message, state: FSMContext):
    await state.update_data(user_params=message.text)
    await message.answer("Принято! 🎯 Теперь выбери свою цель:", reply_markup=goal_keyboard)
    await state.set_state(BotStates.waiting_for_goal)

@dp.callback_query(BotStates.waiting_for_goal, F.data.startswith("goal_"))
async def process_goal(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    goals = {"goal_loss": "Похудение", "goal_maintain": "Поддержание", "goal_gain": "Набор массы"}
    selected_goal = goals[callback.data]
    await state.update_data(user_goal=selected_goal)
    await callback.message.answer(f"Цель: {selected_goal}. 🏃‍♀️ Оцени свой уровень активности:", reply_markup=activity_keyboard)
    await state.set_state(BotStates.waiting_for_activity)

@dp.callback_query(BotStates.waiting_for_activity, F.data.startswith("act_"))
async def process_activity(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    activities = {"act_low": "Низкая", "act_med": "Средняя", "act_high": "Высокая"}
    selected_activity = activities[callback.data]
    await state.update_data(user_activity=selected_activity)
    await callback.message.answer(f"Активность: {selected_activity}. 📝\n\nНапиши, какие продукты у тебя есть и какую вкусняшку вписать в рацион?")
    await state.set_state(BotStates.waiting_for_menu_ingredients)

@dp.message(BotStates.waiting_for_menu_ingredients)
async def process_menu_ingredients(message: Message, state: FSMContext):
    await state.update_data(user_ingredients=message.text)
    await message.answer(
        "Отлично! Последний вопрос 🧑‍🍳:\n\n"
        "Мне использовать **строго** только эти продукты, или я могу добавить базовые ингредиенты из «виртуального холодильника» (масло, соль, лук, морковь), чтобы блюдо получилось вкуснее?",
        reply_markup=extra_keyboard
    )
    await state.set_state(BotStates.waiting_for_extra_permission)

@dp.callback_query(BotStates.waiting_for_extra_permission, F.data.startswith("extra_"))
async def process_extra_permission(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.edit_text("🍳 Считаю точную норму и придумываю меню...")
    
    user_data = await state.get_data()
    params = user_data.get("user_params")
    goal = user_data.get("user_goal")
    activity = user_data.get("user_activity")
    ingredients = user_data.get("user_ingredients")
    
    if callback.data == "extra_yes":
        extra_instruction = "РАЗРЕШЕНО добавлять базовые продукты (масло, специи, овощи для вкуса)."
    else:
        extra_instruction = "СТРОГО ЗАПРЕЩЕНО добавлять любые продукты, которых нет в списке. Готовь только из того, что перечислено."
        
    full_prompt = f"Параметры: {params}. Цель: {goal}. Активность: {activity}. Продукты: {ingredients}. Указание: {extra_instruction}"
    
    try:
        ai_response = await ask_ai(text_prompt=full_prompt)
        await callback.message.edit_text(ai_response)
    except Exception as e:
        await callback.message.edit_text(f"Ошибка ИИ: {e}")
    finally:
        await state.clear()

async def health_check(request):
    return web.Response(text="Bot is running!")

async def main():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    
    print("Бот запущен на сервере!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
