import asyncio
import os
import base64
import json
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime, timedelta
from dotenv import load_dotenv
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from openai import AsyncOpenAI

import firebase_admin
from firebase_admin import credentials, firestore

load_dotenv()
TOKEN = os.getenv('BOT_TOKEN')
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
    print("❌ ОШИБКА: Нет FIREBASE_JSON!")
    db = None

bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- СОСТОЯНИЯ ---
class BotStates(StatesGroup):
    waiting_for_status = State()
    waiting_for_clarification = State()
    waiting_for_menu_ingredients = State()
    waiting_for_extra_permission = State()
    waiting_for_cheat_meal = State()

class ProfileStates(StatesGroup):
    gender = State()
    age = State()
    height = State()
    weight = State()
    goal = State()
    activity = State()
    new_weight = State()

# --- КЛАВИАТУРЫ ---
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🍎 Меню из того что есть"), KeyboardButton(text="🛒 Список на неделю")],
        [KeyboardButton(text="📊 Мой дневник"), KeyboardButton(text="📈 Итоги недели")],
        [KeyboardButton(text="🍩 Хочу вкусняшку!"), KeyboardButton(text="👤 Мой профиль")],
        [KeyboardButton(text="❌ Сбросить шаг")]
    ],
    resize_keyboard=True,
    input_field_placeholder="Выбери действие..."
)

gender_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="👨 Мужчина", callback_data="gender_M"), InlineKeyboardButton(text="👩 Женщина", callback_data="gender_F")]
])

goal_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📉 Похудение", callback_data="goal_loss")],
    [InlineKeyboardButton(text="⚖️ Поддержание веса", callback_data="goal_maintain")],
    [InlineKeyboardButton(text="📈 Набор массы", callback_data="goal_gain")]
])

activity_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🛋 Низкая (сидячая работа)", callback_data="act_low")],
    [InlineKeyboardButton(text="🚶 Средняя (1-3 тренировки)", callback_data="act_med")],
    [InlineKeyboardButton(text="🏃 Высокая (спорт 3+ раз)", callback_data="act_high")]
])

status_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🥩 Сырые продукты", callback_data="status_raw"), InlineKeyboardButton(text="🍳 Готовое блюдо", callback_data="status_cooked")]
])

extra_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🧑‍🍳 Добавь базу (масло, лук)", callback_data="extra_yes")],
    [InlineKeyboardButton(text="🛑 СТРОГО из моего списка", callback_data="extra_no")]
])

# ЖЕСТКОЕ ПРАВИЛО ДЛЯ КРАСИВОГО ТЕКСТА (ДЛЯ АРТЕМА)
SYSTEM_PROMPT = """Ты — профессиональный ИИ-нутрициолог. 
ТВОЕ ГЛАВНОЕ ПРАВИЛО: НИКАКИХ ПОЛОТЕН ТЕКСТА! Всегда структурируй свой ответ. Используй короткие абзацы, списки (с буллитами), выделяй жирным шрифтом заголовки и итоговое КБЖУ. Обязательно добавляй подходящие эмодзи для красоты.
ВАЖНОЕ ПРАВИЛО ПО ЦИФРАМ: НИКАКИХ ДИАПАЗОНОВ! Выдавай только ОДНО точное число для веса, калорий и БЖУ (например, пиши не 15-20 г, а ровно 18 г).

ЕСЛИ СЧИТАЕШЬ ФОТО ИЛИ ЕДУ: 
1. Если на фото блюдо с закрытой начинкой (блины, пирожки), задай МАКСИМУМ ОДИН вопрос только про главную начинку. Начни ответ со слова "УТОЧНИТЬ:". 
2. После того как пользователь ответил на первый вопрос, БОЛЬШЕ НИЧЕГО НЕ СПРАШИВАЙ!
3. Если ты не знаешь точный вес, размер порции или мелкие добавки (сахар, масло, соль) — НЕ СПРАШИВАЙ! Просто визуально оцени объем, используй средние стандартные значения для таких порций и СРАЗУ выдавай финальный расчет.
4. Если всё понятно, выдай красивый и четкий расчет."""
async def ask_ai(image_base64=None, text_prompt=None, context=""):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    
    if image_base64:
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": f"Контекст: {context}. Изучи еду, оцени вес и КБЖУ."},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
            ]
        })
    elif text_prompt:
        messages.append({"role": "user", "content": text_prompt})

    response = await client.chat.completions.create(model="gpt-4o", messages=messages, temperature=0.3)
    return response.choices[0].message.content

def calculate_norm(gender, age, height, weight, goal, activity):
    if gender == 'M': bmr = (10 * weight) + (6.25 * height) - (5 * age) + 5
    else: bmr = (10 * weight) + (6.25 * height) - (5 * age) - 161
    act_mults = {"Низкая": 1.2, "Средняя": 1.55, "Высокая": 1.725}
    tdee = bmr * act_mults.get(activity, 1.2)
    if goal == "Похудение": tdee *= 0.8
    elif goal == "Набор массы": tdee *= 1.2
    return int(tdee)

def get_user_profile(user_id):
    doc = db.collection('users').document(str(user_id)).get()
    return doc.to_dict() if doc.exists else None

# --- ПРОФИЛЬ ---
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_name = message.from_user.first_name or "друг"
    if get_user_profile(message.from_user.id):
        await message.answer(f"Привет, {user_name}! 👋 Главное меню обновлено. Жду фото еды!", reply_markup=main_menu)
    else:
        await message.answer(f"Привет, {user_name}! 👋 Давай настроим твой профиль.\nУкажи свой пол:", reply_markup=gender_kb)
        await state.set_state(ProfileStates.gender)

@dp.callback_query(ProfileStates.gender, F.data.startswith("gender_"))
async def ask_age(callback: CallbackQuery, state: FSMContext):
    await state.update_data(gender=callback.data.split("_")[1])
    await callback.message.edit_text("Отлично! Напиши свой возраст (цифрой):")
    await state.set_state(ProfileStates.age)

@dp.message(ProfileStates.age)
async def ask_height(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("Только цифры.")
    await state.update_data(age=int(message.text))
    await message.answer("Укажи свой рост в см:")
    await state.set_state(ProfileStates.height)

@dp.message(ProfileStates.height)
async def ask_weight(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("Только цифры.")
    await state.update_data(height=int(message.text))
    await message.answer("Укажи свой текущий вес в кг:")
    await state.set_state(ProfileStates.weight)

@dp.message(ProfileStates.weight)
async def ask_prof_goal(message: Message, state: FSMContext):
    try:
        await state.update_data(weight=float(message.text.replace(',', '.')))
        await message.answer("Какая у тебя цель?", reply_markup=goal_keyboard)
        await state.set_state(ProfileStates.goal)
    except ValueError:
        await message.answer("Введи вес цифрами.")

@dp.callback_query(ProfileStates.goal, F.data.startswith("goal_"))
async def ask_prof_act(callback: CallbackQuery, state: FSMContext):
    goals = {"goal_loss": "Похудение", "goal_maintain": "Поддержание", "goal_gain": "Набор массы"}
    await state.update_data(goal=goals[callback.data])
    await callback.message.edit_text("Уровень активности:", reply_markup=activity_keyboard)
    await state.set_state(ProfileStates.activity)

@dp.callback_query(ProfileStates.activity, F.data.startswith("act_"))
async def finish_profile(callback: CallbackQuery, state: FSMContext):
    acts = {"act_low": "Низкая", "act_med": "Средняя", "act_high": "Высокая"}
    data = await state.get_data()
    norm = calculate_norm(data['gender'], data['age'], data['height'], data['weight'], data['goal'], acts[callback.data])
    db.collection('users').document(str(callback.from_user.id)).set({
        'gender': data['gender'], 'age': data['age'], 'height': data['height'], 'weight': data['weight'],
        'goal': data['goal'], 'activity': acts[callback.data], 'norm': norm
    })
    await callback.message.delete()
    await callback.message.answer(f"✅ Профиль создан!\nНорма: **{norm} ккал**.", reply_markup=main_menu)
    await state.clear()

@dp.message(F.text == "👤 Мой профиль")
async def show_my_profile(message: Message):
    data = get_user_profile(message.from_user.id)
    if not data: return await message.answer("Профиль не найден. Нажми /start")
    user_name = message.from_user.first_name or "Пользователь"
    text = f"👤 **Профиль: {user_name}**\n\nВес: {data['weight']} кг\nРост: {data['height']} см\nВозраст: {data['age']} лет\nЦель: {data['goal']}\n\n🔥 **Дневная норма: {data['norm']} ккал**"
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚖️ Обновить вес", callback_data="update_weight")]]))

@dp.callback_query(F.data == "update_weight")
async def req_new_weight(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введи свой новый вес (кг):")
    await state.set_state(ProfileStates.new_weight)

@dp.message(ProfileStates.new_weight)
async def save_new_weight(message: Message, state: FSMContext):
    try:
        new_w = float(message.text.replace(',', '.'))
        user_id = str(message.from_user.id)
        doc_ref = db.collection('users').document(user_id)
        data = doc_ref.get().to_dict()
        new_norm = calculate_norm(data['gender'], data['age'], data['height'], new_w, data['goal'], data['activity'])
        doc_ref.update({'weight': new_w, 'norm': new_norm})
        await message.answer(f"🎉 Вес обновлен! Новая норма: **{new_norm} ккал**.", reply_markup=main_menu)
        await state.clear()
    except ValueError:
        await message.answer("Введи цифры.")

@dp.message(F.text == "❌ Сбросить шаг")
async def cancel_action(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🔄 Шаг отменен. Жду фото или команду из меню!", reply_markup=main_menu)

# --- НОВЫЕ ФИШКИ ---
@dp.message(F.text == "🛒 Список на неделю")
async def weekly_grocery_list(message: Message):
    data = get_user_profile(message.from_user.id)
    if not data: return await message.answer("Сначала заполни профиль (/start).")
    msg = await message.answer("⏳ Составляю меню на 5 дней и сводный список продуктов. Это займет около 15-20 секунд...")
    prompt = f"Составь разнообразное меню на 5 дней (завтрак, обед, ужин) для нормы {data['norm']} ккал ({data['goal']}). В конце ОБЯЗАТЕЛЬНО напиши общий сводный список покупок для супермаркета (продукты и граммы/штуки)."
    try:
        res = await ask_ai(text_prompt=prompt)
        await msg.edit_text(res)
    except Exception:
        await msg.edit_text("Ошибка при составлении списка.")

@dp.message(F.text == "🍩 Хочу вкусняшку!")
async def cheat_meal_start(message: Message, state: FSMContext):
    if not get_user_profile(message.from_user.id): return await message.answer("Сначала заполни профиль (/start).")
    await message.answer("🤤 Какую вкусняшку ты хочешь съесть? Напиши текстом (например: кусок Наполеона, сникерс):")
    await state.set_state(BotStates.waiting_for_cheat_meal)

@dp.message(BotStates.waiting_for_cheat_meal)
async def cheat_meal_process(message: Message, state: FSMContext):
    data = get_user_profile(message.from_user.id)
    msg = await message.answer("⏳ Считаю калории и подбираю варианты...")
    prompt = f"Пользователь хочет съесть: '{message.text}'. Его дневная норма: {data['norm']} ккал ({data['goal']}). Оцени примерную калорийность этой вкусняшки. Затем предложи легкий вариант обеда или ужина, который поможет компенсировать эти калории."
    try:
        res = await ask_ai(text_prompt=prompt)
        await msg.edit_text(res)
    except Exception:
        await msg.edit_text("Ошибка.")
    finally:
        await state.clear()

@dp.message(F.text == "📈 Итоги недели")
async def weekly_summary(message: Message):
    user_id = str(message.from_user.id)
    data = get_user_profile(user_id)
    if not data: return await message.answer("Сначала заполни профиль (/start).")
    msg = await message.answer("📊 Собираю твой дневник за последние 7 дней...")
    
    today = datetime.now()
    diary_entries = []
    for i in range(7):
        date_str = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        doc = db.collection('diaries').document(f"{user_id}_{date_str}").get()
        if doc.exists and doc.to_dict().get('meals'):
            meals_text = "\n".join(doc.to_dict().get('meals'))
            diary_entries.append(f"--- День {date_str} ---\n{meals_text}")
            
    if not diary_entries:
        return await msg.edit_text("Твой дневник за неделю пуст! Начни записывать еду, чтобы я мог сделать разбор.")
        
    all_text = "\n\n".join(diary_entries)
    prompt = f"Вот выгрузка дневника питания за последние дни:\n{all_text}\n\nНорма пользователя: {data['norm']} ккал ({data['goal']}). Сделай профессиональный, но дружелюбный анализ недели. Похвали за то, что получилось хорошо. Дай 2-3 практичных совета."
    try:
        res = await ask_ai(text_prompt=prompt)
        await msg.edit_text(res)
    except Exception:
        await msg.edit_text("Ошибка при анализе недели.")

# --- РЕЦЕПТЫ ИЗ ТОГО ЧТО ЕСТЬ ---
@dp.message(F.text == "🍎 Меню из того что есть")
async def start_menu_generation(message: Message, state: FSMContext):
    if not get_user_profile(message.from_user.id): return await message.answer("Заполни профиль (/start).")
    await message.answer("📝 Напиши список продуктов (например: курица, рис, помидоры):")
    await state.set_state(BotStates.waiting_for_menu_ingredients)

@dp.message(BotStates.waiting_for_menu_ingredients)
async def process_menu_ingredients(message: Message, state: FSMContext):
    await state.update_data(user_ingredients=message.text)
    await message.answer("Можно добавить базовые продукты (масло, специи, лук)?", reply_markup=extra_keyboard)
    await state.set_state(BotStates.waiting_for_extra_permission)

@dp.callback_query(BotStates.waiting_for_extra_permission, F.data.startswith("extra_"))
async def process_extra_permission(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.edit_text("🍳 Придумываю красивый рецепт...")
    data = get_user_profile(callback.from_user.id)
    s_data = await state.get_data()
    ex = "РАЗРЕШЕНО добавлять базу." if callback.data == "extra_yes" else "СТРОГО ЗАПРЕЩЕНО добавлять чужие ингредиенты."
    prompt = f"Составь меню на 1 прием пищи. Цель {data['goal']}, норма {data['norm']} ккал. Ингредиенты: {s_data.get('user_ingredients')}. {ex} Напиши рецепт и граммовки. Выведи КБЖУ."
    try:
        res = await ask_ai(text_prompt=prompt)
        await state.update_data(last_ai_response=f"🍽 По рецепту:\n{res}")
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🍽 Я съем это! (В дневник)", callback_data="save_to_diary")]])
        await callback.message.edit_text(res, reply_markup=kb)
        await state.set_state(None)
    except Exception:
        await callback.message.edit_text("Ошибка при составлении меню.")
        await state.clear()

# --- ДНЕВНИК ---
def get_today_doc_id(user_id):
    return f"{user_id}_{datetime.now().strftime('%Y-%m-%d')}"

@dp.message(F.text == "📊 Мой дневник")
async def show_diary(message: Message):
    user_id = str(message.from_user.id)
    data = get_user_profile(user_id)
    if not data: return await message.answer("Заполни профиль (/start).")
    doc = db.collection('diaries').document(get_today_doc_id(user_id)).get()
    if not doc.exists or not doc.to_dict().get('meals'):
        return await message.answer(f"Дневник пуст! Отправь фото. (Цель: {data['norm']} ккал)", reply_markup=main_menu)
    meals = doc.to_dict().get('meals', [])
    msg = await message.answer("📊 Считаю итоги за сегодня...")
    prompt = f"Съедено:\n{chr(10).join(meals)}\nСделай отчет, итог КБЖУ. МОЯ НОРМА: {data['norm']} ккал. Сколько осталось?"
    try:
        await msg.edit_text(await ask_ai(text_prompt=prompt))
    except Exception:
        await msg.edit_text("Ошибка.")

@dp.callback_query(F.data == "save_to_diary")
async def save_to_diary(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("last_ai_response"): return await callback.answer("Нечего сохранять!", show_alert=True)
    record = f"⏰ {datetime.now().strftime('%H:%M:%S')}\n{data['last_ai_response']}"
    db.collection('diaries').document(get_today_doc_id(callback.from_user.id)).set({'meals': firestore.ArrayUnion([record])}, merge=True)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("✅ Записано в дневник!")

# --- ФОТО И УТОЧНЕНИЯ ---
@dp.message(F.photo)
async def handle_photo(message: Message, state: FSMContext):
    await state.clear()
    file = await bot.get_file(message.photo[-1].file_id)
    d_file = await bot.download_file(file.file_path)
    await state.update_data(saved_photo=base64.b64encode(d_file.read()).decode('utf-8'), photo_caption=message.caption or "")
    await state.set_state(BotStates.waiting_for_status)
    await message.answer("Уточни статус продукта:", reply_markup=status_keyboard)

@dp.callback_query(BotStates.waiting_for_status, F.data.in_(["status_raw", "status_cooked"]))
async def process_photo_status(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.edit_text("⏳ Нейросеть изучает фото...")
    data = await state.get_data()
    st = "СЫРЫЕ ПРОДУКТЫ" if callback.data == "status_raw" else "ГОТОВОЕ БЛЮДО"
    if data.get("photo_caption"): st += f". Коммент: {data['photo_caption']}"
    
    try:
        res = await ask_ai(image_base64=data.get("saved_photo"), context=st)
        if await state.get_state() != BotStates.waiting_for_status.state: return 
        
        # 💡 ИСПРАВЛЕННЫЙ БЛОК: Ищем слово УТОЧНИТЬ в тексте, даже если ИИ добавил звездочки
        if "УТОЧНИТЬ:" in res:
            question = res.replace("**УТОЧНИТЬ:**", "").replace("УТОЧНИТЬ:", "").strip()
            await callback.message.edit_text(f"🤔 {question}\n\n(Напиши ответ текстом)")
            await state.update_data(current_context=st)
            await state.set_state(BotStates.waiting_for_clarification)
            return

        # Если это не вопрос, а готовый расчет, то выдаем его и кнопку дневника
        await state.update_data(last_ai_response=res)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Записать в дневник", callback_data="save_to_diary")]])
        await callback.message.edit_text(res, reply_markup=kb)
        await state.set_state(None)
        
    except Exception:
        await callback.message.edit_text("Ошибка при обработке фото.")
        await state.clear()
    
    try:
        await state.update_data(last_ai_response=res)
        await callback.message.edit_text(res, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Записать в дневник", callback_data="save_to_diary")]]))
        await state.set_state(None)
    except Exception:
        await callback.message.edit_text("Ошибка.")
        await state.clear()

@dp.message(BotStates.waiting_for_clarification)
async def process_clarification(message: Message, state: FSMContext):
    data = await state.get_data()
    new_context = data.get("current_context", "") + f". Уточнение пользователя: {message.text}"
    msg = await message.answer("⏳ Пересчитываю с учетом твоих данных...")
    
    try:
        res = await ask_ai(image_base64=data.get("saved_photo"), context=new_context)
        
        # 💡 ИСПРАВЛЕНИЕ: Если нейросеть задает ВТОРОЙ (или третий) уточняющий вопрос
        if "УТОЧНИТЬ:" in res:
            question = res.replace("**УТОЧНИТЬ:**", "").replace("УТОЧНИТЬ:", "").strip()
            await msg.edit_text(f"🤔 {question}\n\n(Напиши ответ текстом)")
            await state.update_data(current_context=new_context)
            # Состояние НЕ сбрасываем, ждем следующий текстовый ответ
            return

        # Если вопросов больше нет и это финальный расчет — выдаем кнопку
        await state.update_data(last_ai_response=res)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Записать в дневник", callback_data="save_to_diary")]])
        await msg.edit_text(res, reply_markup=kb)
        await state.set_state(None)
        
    except Exception:
        await msg.edit_text("Ошибка при пересчете.")
        await state.clear()

# --- СЕРВЕР ---
async def health_check(request):
    return web.Response(text="Bot is running!")

# --- УТРЕННЯЯ РАССЫЛКА ---
async def send_morning_reminders():
    if not db:
        return
    print("🌅 Запуск утренней рассылки...")
    users_ref = db.collection('users').stream()
    
    for doc in users_ref:
        user_id = doc.id
        try:
            # Отправляем сообщение каждому пользователю в базе
            await bot.send_message(
                chat_id=user_id, 
                text="☀️ Доброе утро! Готов посчитать твои калории.\nЖду фото твоего завтрака! 🍳", 
                reply_markup=main_menu
            )
        except Exception as e:
            print(f"❌ Не удалось отправить сообщение пользователю {user_id}: {e}")

# --- СЕРВЕР И ЗАПУСК ---
async def health_check(request):
    return web.Response(text="Bot is running!")

async def main():
    # Настраиваем таймер
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    # Ставим рассылку каждый день в 09:00 утра
    scheduler.add_job(send_morning_reminders, trigger=CronTrigger(hour=9, minute=0))
    scheduler.start()

    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 8080)))
    await site.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
