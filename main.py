import asyncioimport base64import jsonimport loggingimport osimport refrom datetime import datetimefrom aiohttp import webfrom dotenv import load_dotenvfrom aiogram import Bot, Dispatcher, Ffrom aiogram.client.default import DefaultBotPropertiesfrom aiogram.enums import ParseModefrom aiogram.filters import Command, CommandStartfrom aiogram.fsm.context import FSMContextfrom aiogram.fsm.state import State, StatesGroupfrom aiogram.types import (
 BotCommand,
 CallbackQuery,
 InlineKeyboardButton,
 InlineKeyboardMarkup,
 KeyboardButton,
 Message,
 ReplyKeyboardMarkup,
)

from openai import (
 APIConnectionError,
 APIStatusError,
 AsyncOpenAI,
 AuthenticationError,
 BadRequestError,
 RateLimitError,
)

import firebase_adminfrom firebase_admin import credentials, firestore# =========================================================# НАСТРОЙКИ# =========================================================load_dotenv()

logging.basicConfig(
 level=logging.INFO,
 format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)

BOT_TOKEN = (
 os.getenv("BOT_TOKEN")
 or os.getenv("TELEGRAM_BOT_TOKEN")
)

AI_API_KEY = (
 os.getenv("AI_API_KEY")
 or os.getenv("POLZA_API_KEY")
)

AI_BASE_URL = (
 os.getenv("AI_BASE_URL")
 or os.getenv("POLZA_BASE_URL")
)

AI_MODEL = os.getenv("AI_MODEL")

if not BOT_TOKEN:
 raise RuntimeError(
 "Не найден BOT_TOKEN или TELEGRAM_BOT_TOKEN"
 )

if not AI_API_KEY:
 raise RuntimeError(
 "Не найден AI_API_KEY или POLZA_API_KEY"
 )

if not AI_BASE_URL:
 raise RuntimeError(
 "Не найден AI_BASE_URL или POLZA_BASE_URL"
 )

if not AI_MODEL:
 raise RuntimeError(
 "Не найден AI_MODEL. Укажите точное название модели из Polza."
 )


logger.info("AI_BASE_URL: %s", AI_BASE_URL)
logger.info("AI_MODEL: %s", AI_MODEL)

ai_client = AsyncOpenAI(
 api_key=AI_API_KEY,
 base_url=AI_BASE_URL.rstrip("/"),
 timeout=90,
 max_retries=2,
)


# =========================================================# FIREBASE# =========================================================db = Nonefirebase_json = os.getenv("FIREBASE_JSON")

if firebase_json:
 try:
 firebase_data = json.loads(firebase_json)

 if firebase_data.get("private_key"):
 firebase_data["private_key"] = (
 firebase_data["private_key"].replace("\\n", "\n")
 )

 firebase_cred = credentials.Certificate(firebase_data)

 if not firebase_admin._apps:
 firebase_admin.initialize_app(firebase_cred)

 db = firestore.client()
 logger.info("Firebase подключен")

 except Exception:
 logger.exception("Ошибка подключения Firebase")
 raiseelse:
 logger.warning(
 "FIREBASE_JSON не найден. Данные сохраняться не будут."
 )


# =========================================================# TELEGRAM# =========================================================bot = Bot(
 token=BOT_TOKEN,
 default=DefaultBotProperties(
 parse_mode=ParseMode.HTML ),
)

dp = Dispatcher()


main_menu = ReplyKeyboardMarkup(
 keyboard=[
 [
 KeyboardButton(text="📊 Сегодня"),
 KeyboardButton(text="🥗 Что приготовить"),
 ],
 [
 KeyboardButton(text="⚖️ Вес"),
 KeyboardButton(text="👤 Профиль"),
 ],
 ],
 resize_keyboard=True,
 input_field_placeholder="Пришли фото еды",
)


# =========================================================# СОСТОЯНИЯ# =========================================================class Onboarding(StatesGroup):
 stats = State()


class BotStates(StatesGroup):
 edit_food = State()
 fridge = State()


# =========================================================# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ# =========================================================def today_id(user_id: int | str) -> str:
 date = datetime.now().strftime("%Y-%m-%d")
 return f"{user_id}_{date}"def get_user(user_id: int | str):
 if db is None:
 return None document = (
 db.collection("users")
 .document(str(user_id))
 .get()
 )

 if document.exists:
 return document.to_dict()

 return Nonedef calculate_norm(
 gender: str,
 age: int,
 height: int,
 weight: float,
 goal: str,
 activity: str,
):
 if gender == "M":
 bmr = (
10 * weight +6.25 * height -5 * age +5 )
 else:
 bmr = (
10 * weight +6.25 * height -5 * age -161 )

 activity_coefficients = {
 "low":1.2,
 "light":1.375,
 "medium":1.55,
 "high":1.725,
 "sport":1.9,
 }

 tdee = bmr * activity_coefficients.get(
 activity,
1.2,
 )

 if goal == "loss":
 norm = tdee *0.78 elif goal == "gain":
 norm = tdee *1.15 else:
 norm = tdee return {
 "bmr": int(bmr),
 "tdee": int(tdee),
 "norm": int(norm),
 "protein": int(norm *0.27 /4),
 "fat": int(norm *0.40 /9),
 "carbs": int(norm *0.33 /4),
 }


async def set_commands():
 await bot.set_my_commands([
 BotCommand("today", "Дневник за сегодня"),
 BotCommand("plan", "Моя норма КБЖУ"),
 BotCommand("fridge", "Меню из холодильника"),
 BotCommand("weight", "Вес"),
 BotCommand("profile", "Профиль"),
 BotCommand("help", "Помощь"),
 ])


async def ask_ai(
 text: str,
 image_base64: str | None = None,
):
 messages = [
 {
 "role": "system",
 "content": (
 "Ты помощник по питанию и дневнику еды. "
 "Отвечай на русском языке."
 ),
 }
 ]

 if image_base64:
 messages.append({
 "role": "user",
 "content": [
 {
 "type": "text",
 "text": text,
 },
 {
 "type": "image_url",
 "image_url": {
 "url": (
 "data:image/jpeg;base64,"
 f"{image_base64}"
 ),
 },
 },
 ],
 })
 else:
 messages.append({
 "role": "user",
 "content": text,
 })

 try:
 response = await ai_client.chat.completions.create(
 model=AI_MODEL,
 messages=messages,
 )

 if not response.choices:
 return "Нейросеть не вернула ответ."

 return (
 response.choices[0]
 .message .content or "Нейросеть вернула пустой ответ."
 )

 except AuthenticationError:
 logger.exception("Ошибка авторизации AI")
 return "Ошибка авторизации. Проверьте AI_API_KEY."

 except BadRequestError:
 logger.exception("Некорректный запрос AI")
 return (
 "AI-сервис не принял запрос. "
 "Проверьте название модели."
 )

 except RateLimitError:
 logger.exception("Превышен лимит AI")
 return (
 "Закончился лимит AI или баланс. "
 "Проверьте Polza."
 )

 except APIConnectionError:
 logger.exception("Ошибка подключения AI")
 return (
 "Не удалось подключиться к AI-сервису. "
 "Проверьте AI_BASE_URL."
 )

 except APIStatusError as error:
 logger.exception("Ошибка AI HTTP %s", error.status_code)
 return (
 f"AI-сервис вернул ошибку {error.status_code}."
 )

 except Exception:
 logger.exception("Неизвестная ошибка AI")
 return "Произошла ошибка при анализе. Попробуйте ещё раз."


def extract_number(pattern: str, text: str, default: int =0):
 match = re.search(pattern, text, re.IGNORECASE)
 if not match:
 return default try:
 return int(match.group(1))
 except ValueError:
 return defaultasync def show_today(message: Message):
 user_id = message.from_user.id user = get_user(user_id)

 if not user:
 await message.answer("Сначала нажмите /start.")
 return if db is None:
 await message.answer(
 "Firebase не подключен. Проверьте FIREBASE_JSON."
 )
 return document = (
 db.collection("diaries")
 .document(today_id(user_id))
 .get()
 )

 data = document.to_dict() if document.exists else {}
 meals = data.get("meals", [])
 total = data.get("total_kcal",0)

 norm = user.get("norm",0)
 remaining = max(norm - total,0)

 text = (
 "📊 <b>ДНЕВНИК ЗА СЕГОДНЯ</b>\n"
 "━━━━━━━━━━━━━━━━━━━━\n"
 )

 if meals:
 text += "\n\n".join(meals) + "\n\n"
 else:
 text += "Пока записей нет.\n\n"

 text += (
 "━━━━━━━━━━━━━━━━━━━━\n"
 f"🔥 Съедено: <b>{total} ккал</b>\n"
 f"🎯 Норма: <b>{norm} ккал</b>\n"
 f"Осталось: <b>{remaining} ккал</b>"
 )

 await message.answer(text)


# =========================================================# ОНБОРДИНГ# =========================================================@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
 await state.clear()

 user = get_user(message.from_user.id)

 if user:
 await message.answer(
 "С возвращением! Пришлите фото еды 📸",
 reply_markup=main_menu,
 )
 return keyboard = InlineKeyboardMarkup(
 inline_keyboard=[
 [
 InlineKeyboardButton(
 text="👨 Мужчина",
 callback_data="gender:M",
 )
 ],
 [
 InlineKeyboardButton(
 text="👩 Женщина",
 callback_data="gender:F",
 )
 ],
 ]
 )

 await message.answer(
 "Привет! Я помогу вести дневник питания 🥗\n\n"
 "Сначала ответьте на несколько вопросов.\n\n"
 "<b>Ваш пол?</b>",
 reply_markup=keyboard,
 )


@dp.callback_query(F.data.startswith("gender:"))
async def select_gender(
 callback: CallbackQuery,
 state: FSMContext,
):
 gender = callback.data.split(":")[1]

 await state.update_data(gender=gender)
 await state.set_state(Onboarding.stats)

 await callback.message.edit_text(
 "Напишите через пробел:\n"
 "<b>возраст рост вес</b>\n\n"
 "Например: <code>3218292</code>"
 )

 await callback.answer()


@dp.message(Onboarding.stats)
async def onboarding_stats(
 message: Message,
 state: FSMContext,
):
 if not message.text:
 await message.answer(
 "Напишите возраст, рост и вес числами."
 )
 return numbers = re.findall(
 r"\d+(?:[.,]\d+)?",
 message.text,
 )

 if len(numbers) <3:
 await message.answer(
 "Нужно указать три значения: возраст, рост и вес."
 )
 return age = int(float(numbers[0].replace(",", ".")))
 height = int(float(numbers[1].replace(",", ".")))
 weight = float(numbers[2].replace(",", "."))

 await state.update_data(
 age=age,
 height=height,
 weight=weight,
 )

 keyboard = InlineKeyboardMarkup(
 inline_keyboard=[
 [
 InlineKeyboardButton(
 text="📉 Похудеть",
 callback_data="goal:loss",
 )
 ],
 [
 InlineKeyboardButton(
 text="⚖️ Удержать вес",
 callback_data="goal:maintain",
 )
 ],
 [
 InlineKeyboardButton(
 text="📈 Набрать массу",
 callback_data="goal:gain",
 )
 ],
 ]
 )

 await message.answer(
 "<b>Какая ваша цель?</b>",
 reply_markup=keyboard,
 )


@dp.callback_query(F.data.startswith("goal:"))
async def select_goal(
 callback: CallbackQuery,
 state: FSMContext,
):
 goal = callback.data.split(":")[1]

 await state.update_data(goal=goal)

 keyboard = InlineKeyboardMarkup(
 inline_keyboard=[
 [
 InlineKeyboardButton(
 text="🛋 Сидячий образ жизни",
 callback_data="activity:low",
 )
 ],
 [
 InlineKeyboardButton(
 text="🚶 Лёгкая активность",
 callback_data="activity:light",
 )
 ],
 [
 InlineKeyboardButton(
 text="🏃 Умеренная активность",
 callback_data="activity:medium",
 )
 ],
 [
 InlineKeyboardButton(
 text="🏋️ Высокая активность",
 callback_data="activity:high",
 )
 ],
 ]
 )

 await callback.message.edit_text(
 "<b>Какой у вас уровень активности?</b>",
 reply_markup=keyboard,
 )

 await callback.answer()


@dp.callback_query(F.data.startswith("activity:"))
async def finish_onboarding(
 callback: CallbackQuery,
 state: FSMContext,
):
 activity = callback.data.split(":")[1]
 data = await state.get_data()

 result = calculate_norm(
 gender=data["gender"],
 age=data["age"],
 height=data["height"],
 weight=data["weight"],
 goal=data["goal"],
 activity=activity,
 )

 if db is not None:
 db.collection("users").document(
 str(callback.from_user.id)
 ).set({
 **data,
 "activity": activity,
 **result,
 "created_at": datetime.utcnow(),
 })

 await state.clear()

 await callback.message.edit_text(
 "🎯 <b>Ваша дневная норма</b>\n"
 "━━━━━━━━━━━━━━━━━━━━\n"
 f"🔥 Калории: <b>{result['norm']} ккал</b>\n"
 f"🥩 Белки: <b>{result['protein']} г</b>\n"
 f"🥑 Жиры: <b>{result['fat']} г</b>\n"
 f"🍚 Углеводы: <b>{result['carbs']} г</b>\n\n"
 "Теперь пришлите фотографию еды 📸"
 )

 await callback.message.answer(
 "Готово! Я буду считать рацион и сохранять его в дневник.",
 reply_markup=main_menu,
 )

 await callback.answer()


# =========================================================# ФОТО ЕДЫ# =========================================================@dp.message(F.photo)
async def photo_food(
 message: Message,
 state: FSMContext,
):
 user = get_user(message.from_user.id)

 if not user:
 await message.answer("Сначала пройдите регистрацию: /start")
 return wait_message = await message.answer(
 "👀 Анализирую фотографию..."
 )

 try:
 telegram_file = await bot.get_file(
 message.photo[-1].file_id )

 downloaded = await bot.download_file(
 telegram_file.file_path )

 image_base64 = base64.b64encode(
 downloaded.read()
 ).decode("utf-8")

 result = await ask_ai(
 image_base64=image_base64,
 text=(
 "Определи блюдо на фото. "
 "Укажи примерный состав и вес ингредиентов. "
 "Не считай калории. Ответь кратко."
 ),
 )

 await state.update_data(
 recognized_food=result,
 )

 keyboard = InlineKeyboardMarkup(
 inline_keyboard=[
 [
 InlineKeyboardButton(
 text="✅ Верно",
 callback_data="food:correct",
 )
 ],
 [
 InlineKeyboardButton(
 text="✏️ Поправить",
 callback_data="food:edit",
 ),
 InlineKeyboardButton(
 text="❌ Не то",
 callback_data="food:wrong",
 ),
 ],
 ]
 )

 await wait_message.edit_text(
 f"{result}\n\nВсё верно?",
 reply_markup=keyboard,
 )

 except Exception:
 logger.exception("Ошибка обработки фотографии")
 await wait_message.edit_text(
 "Не удалось обработать фото. Попробуйте ещё раз."
 )


@dp.callback_query(F.data == "food:edit")
async def edit_food(
 callback: CallbackQuery,
 state: FSMContext,
):
 await state.set_state(BotStates.edit_food)

 await callback.message.edit_text(
 "Напишите, что исправить.\n"
 "Например: <i>курицы250 г, а не150 г</i>"
 )

 await callback.answer()


@dp.message(BotStates.edit_food)
async def process_edit(
 message: Message,
 state: FSMContext,
):
 data = await state.get_data()

 result = await ask_ai(
 text=(
 "Вот распознанная еда:\n"
 f"{data.get('recognized_food', '')}\n\n"
 "Исправление пользователя:\n"
 f"{message.text}\n\n"
 "Сформируй исправленный состав блюда."
 )
 )

 await state.update_data(recognized_food=result)
 await state.set_state(None)

 keyboard = InlineKeyboardMarkup(
 inline_keyboard=[
 [
 InlineKeyboardButton(
 text="✅ Верно",
 callback_data="food:correct",
 )
 ],
 [
 InlineKeyboardButton(
 text="✏️ Исправить ещё",
 callback_data="food:edit",
 )
 ],
 ]
 )

 await message.answer(
 f"{result}\n\nВсё верно?",
 reply_markup=keyboard,
 )


@dp.callback_query(F.data == "food:wrong")
async def wrong_food(callback: CallbackQuery):
 await callback.message.edit_text(
 "Понял 🙈 Пришлите фотографию ещё раз "
 "или опишите блюдо текстом."
 )
 await callback.answer()


@dp.callback_query(F.data == "food:correct")
async def calculate_food(
 callback: CallbackQuery,
 state: FSMContext,
):
 data = await state.get_data()
 user = get_user(callback.from_user.id)

 food = data.get("recognized_food", "")
 norm = user.get("norm",2000) if user else2000 await callback.message.edit_text(
 "⏳ Рассчитываю калории и БЖУ..."
 )

 result = await ask_ai(
 text=(
 "Рассчитай калории и БЖУ блюда.\n\n"
 f"Состав:\n{food}\n\n"
 f"Дневная норма: {norm} ккал.\n\n"
 "Ответь строго в формате:\n"
 "Блюдо: название\n"
 "Калории: число ккал\n"
 "Белки: число г\n"
 "Жиры: число г\n"
 "Углеводы: число г\n"
 "Вес: число г\n"
 "Комментарий: короткий совет"
 )
 )

 await state.update_data(calculated_food=result)

 keyboard = InlineKeyboardMarkup(
 inline_keyboard=[
 [
 InlineKeyboardButton(
 text="📊 Сохранить в дневник",
 callback_data="food:save",
 )
 ],
 [
 InlineKeyboardButton(
 text="🗑 Удалить",
 callback_data="food:delete",
 )
 ],
 ]
 )

 await callback.message.edit_text(
 result,
 reply_markup=keyboard,
 )

 await callback.answer()


@dp.callback_query(F.data == "food:save")
async def save_food(
 callback: CallbackQuery,
 state: FSMContext,
):
 if db is None:
 await callback.answer(
 "Firebase не подключен",
 show_alert=True,
 )
 return data = await state.get_data()
 result = data.get("calculated_food", "")

 calories = extract_number(
 r"Калории\s*:\s*(\d+)",
 result,
 )

 protein = extract_number(
 r"Белки\s*:\s*(\d+)",
 result,
 )

 fat = extract_number(
 r"Жиры\s*:\s*(\d+)",
 result,
 )

 carbs = extract_number(
 r"Углеводы\s*:\s*(\d+)",
 result,
 )

 title_match = re.search(
 r"Блюдо\s*:\s*(.+)",
 result,
 re.IGNORECASE,
 )

 title = (
 title_match.group(1).strip()
 if title_match else "Приём пищи"
 )

 meal = (
 f"🍽 {title}\n"
 f"🔥 {calories} ккал\n"
 f"Б {protein} г · "
 f"Ж {fat} г · "
 f"У {carbs} г"
 )

 diary_ref = (
 db.collection("diaries")
 .document(today_id(callback.from_user.id))
 )

 diary_ref.set({
 "meals": firestore.ArrayUnion([meal]),
 "total_kcal": firestore.Increment(calories),
 "total_protein": firestore.Increment(protein),
 "total_fat": firestore.Increment(fat),
 "total_carbs": firestore.Increment(carbs),
 }, merge=True)

 await state.clear()
 await callback.message.edit_text(
 "✅ Приём пищи сохранён в дневник."
 )

 await show_today(callback.message)
 await callback.answer()


@dp.callback_query(F.data == "food:delete")
async def delete_food(
 callback: CallbackQuery,
 state: FSMContext,
):
 await state.clear()
 await callback.message.edit_text(
 "Запись удалена."
 )
 await callback.answer()


# =========================================================# МЕНЮ И КОМАНДЫ# =========================================================@dp.message(F.text == "📊 Сегодня")
@dp.message(Command("today"))
async def today(message: Message):
 await show_today(message)


@dp.message(Command("plan"))
async def plan(message: Message):
 user = get_user(message.from_user.id)

 if not user:
 await message.answer("Сначала нажмите /start.")
 return await message.answer(
 "🎯 <b>Ваша норма</b>\n"
 f"🔥 {user.get('norm',0)} ккал\n"
 f"🥩 Б: {user.get('protein',0)} г\n"
 f"🥑 Ж: {user.get('fat',0)} г\n"
 f"🍚 У: {user.get('carbs',0)} г"
 )


@dp.message(F.text == "🥗 Что приготовить")
@dp.message(Command("fridge"))
async def fridge(
 message: Message,
 state: FSMContext,
):
 await state.set_state(BotStates.fridge)

 await message.answer(
 "Напишите продукты через запятую.\n"
 "Например: курица, рис, помидоры."
 )


@dp.message(BotStates.fridge)
async def make_recipe(
 message: Message,
 state: FSMContext,
):
 await state.clear()

 result = await ask_ai(
 text=(
 "Придумай простой рецепт из продуктов:\n"
 f"{message.text}\n\n"
 "Укажи ингредиенты, шаги приготовления "
 "и примерные калории и БЖУ."
 )
 )

 await message.answer(result)


@dp.message(F.text == "⚖️ Вес")
@dp.message(Command("weight"))
async def weight(message: Message):
 await message.answer(
 "Раздел динамики веса пока в разработке."
 )


@dp.message(F.text == "👤 Профиль")
@dp.message(Command("profile"))
async def profile(message: Message):
 user = get_user(message.from_user.id)

 if not user:
 await message.answer("Сначала нажмите /start.")
 return await message.answer(
 f"👤 Профиль\n"
 f"Возраст: {user.get('age', '-')}\n"
 f"Рост: {user.get('height', '-')} см\n"
 f"Вес: {user.get('weight', '-')} кг"
 )


@dp.message(Command("help"))
async def help_command(message: Message):
 await message.answer(
 "📸 Пришлите фотографию еды — я распознаю её "
 "и рассчитаю калории и БЖУ.\n\n"
 "Также доступны кнопки меню и команды /today, "
 "/plan и /fridge."
 )


# =========================================================# HTTP-СЕРВЕР ДЛЯ RENDER# =========================================================async def health(request):
 return web.json_response({
 "status": "ok",
 "service": "telegram-bot",
 })


async def main():
 app = web.Application()
 app.router.add_get("/", health)
 app.router.add_get("/health", health)

 runner = web.AppRunner(app)
 await runner.setup()

 port = int(os.getenv("PORT", "10000"))

 site = web.TCPSite(
 runner,
 "0.0.0.0",
 port,
 )

 try:
 await site.start()
 logger.info("HTTP-сервер запущен на порту %s", port)

 await set_commands()

 telegram_user = await bot.get_me()
 logger.info(
 "Telegram подключен: @%s",
 telegram_user.username,
 )

 await bot.delete_webhook(
 drop_pending_updates=True )

 logger.info("Polling запущен")

 await dp.start_polling(
 bot,
 allowed_updates=dp.resolve_used_update_types(),
 )

 except Exception:
 logger.exception("Критическая ошибка запуска")
 raise finally:
 await bot.session.close()
 await runner.cleanup()


if __name__ == "__main__":
 asyncio.run(main())
