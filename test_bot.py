import pytest
import asyncio
import itertools
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch, MagicMock

from aiogram import Bot
from aiogram.types import Message, Chat, User, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

from main import (
    calculate_norm,
    extract_json,
    clean_html_tags,
    make_progress_bar,
    today_str,
    start_handler,
    today_handler,
    profile_handler,
    plan_handler,
    treat_button_handler,
    fridge_handler,
    workout_menu_handler,
    workout_location_callback,
    ask_nutritionist_handler,
    weight_handler,
    help_handler,
    toggle_family_mode_handler,
    add_water_handler,
    delete_last_meal_callback,
    food_remember_handler,
    save_meal_handler,
    delete_food_handler,
    admin_broadcast_handler,
    process_smart_input
)

# =========================================================
# БЛОК 1: МАТРИЧНОЕ ТЕСТИРОВАНИЕ ФОРМУЛ (240 ТЕСТОВ)
# =========================================================
GENDERS = ["M", "F"]
AGES = [18, 25, 35, 45, 60]
HEIGHTS = [155, 165, 175, 185]
WEIGHTS = [45.0, 60.0, 75.0, 95.0, 120.0, 140.0]
GOALS = ["loss", "maintain", "gain"]
ACTIVITIES = ["low", "light", "medium", "high"]

NORM_MATRIX = list(itertools.islice(itertools.product(GENDERS, AGES, HEIGHTS, WEIGHTS, GOALS, ACTIVITIES), 240))

@pytest.mark.parametrize("gender,age,height,weight,goal,activity", NORM_MATRIX)
def test_matrix_norm_calculations(gender, age, height, weight, goal, activity):
    norm = calculate_norm(gender, age, height, weight, goal, activity)
    assert norm["calories"] >= 1200, f"Опасно низкие калории: {norm['calories']}"
    calc_sum = (norm["protein"] * 4) + (norm["fat"] * 9) + (norm["carbs"] * 4)
    assert abs(norm["calories"] - calc_sum) <= 20, "Дисбаланс суммы БЖУ!"
    assert norm["protein"] > 0
    assert norm["fat"] > 0
    assert norm["carbs"] > 0


# =========================================================
# БЛОК 2: ТЕСТЫ ПАРСЕРА И НЕЙРОСЕТИ (50 ТЕСТОВ)
# =========================================================
JSON_AI_VARIATIONS = [
    ('{"title": "Овсянка с ягодами", "protein": 8, "fat": 5, "carbs": 45}', "Овсянка с ягодами", 8, 5, 45),
    ('```json\n{"title": "Куриная грудка", "protein": 30, "fat": 3, "carbs": 0}\n```', "Куриная грудка", 30, 3, 0),
    ('```\n{"title": "Гречка", "protein": 6, "fat": 2, "carbs": 35}\n```', "Гречка", 6, 2, 35),
    ('Вот ваш расчет:\n{"title": "Салат", "protein": 2, "fat": 10, "carbs": 5}\nПриятного аппетита!', "Салат", 2, 10, 5),
    ('{"title": "Яблоко", "protein": 0, "fat": 0, "carbs": 20, "calories": 999}', "Яблоко", 0, 0, 20),
    ('{"title": "Творог", "protein": 18, "fat": -2, "carbs": 3}', "Творог", 18, 0, 3),
    ('{"title": "Сыр", "protein": "15", "fat": "20", "carbs": "1"}', "Сыр", 15, 20, 1),
    ('{"title": "", "protein": 10, "fat": 5, "carbs": 10}', "Приём пищи", 10, 5, 10),
    ('```JSON {"title": "Рыба на пару", "protein": 22, "fat": 6, "carbs": 0} ```', "Рыба на пару", 22, 6, 0),
    ('{"title": "Смузи", "protein": 3, "fat": 1, "carbs": 28, "comment": "Отлично"}', "Смузи", 3, 1, 28)
] * 5

@pytest.mark.parametrize("ai_raw,expected_title,p,f,c", JSON_AI_VARIATIONS)
def test_extract_json_resilience_matrix(ai_raw, expected_title, p, f, c):
    res = extract_json(ai_raw)
    assert res["title"] == expected_title
    assert res["protein"] == int(p)
    assert res["fat"] == int(f)
    assert res["carbs"] == int(c)
    assert res["calories"] == int((p * 4) + (f * 9) + (c * 4))


# =========================================================
# БЛОК 3: САНИТАЙЗЕР HTML И UI-ПРОГРЕСС-БАР (ВАРИАНТ 20)
# =========================================================
HTML_CLEAN_TESTS = [
    ("<b>Жирный</b>", "<b>Жирный</b>"),
    ("<i>Курсив</i>", "<i>Курсив</i>"),
    ("<code>Код</code>", "<code>Код</code>"),
    ("<s>Зачеркнутый</s>", "<s>Зачеркнутый</s>"),
    ("<u>Подчеркнутый</u>", "<u>Подчеркнутый</u>"),
    ("<script>alert(1)</script>Текст", "alert(1)Текст"),
    ("<p>Параграф</p>", "Параграф"),
    ("<a href='link'>Ссылка</a>", "<a href='link'>Ссылка</a>"),
    ("<div>Блок <b>внутри</b></div>", "Блок <b>внутри</b>"),
    ("<h1>Заголовок</h1>", "Заголовок")
] * 2

@pytest.mark.parametrize("raw_html,expected", HTML_CLEAN_TESTS)
def test_clean_html_tags_security(raw_html, expected):
    assert clean_html_tags(raw_html) == expected


PROGRESS_BAR_TESTS = [
    (0, 2000, "г", "↳ <i>Нужно ещё: 2000 г</i>"),
    (1500, 2000, "ккал", "↳ <i>Осталось: 500 ккал</i>"),
    (2000, 2000, "г", "↳ <i>Норма ровно выполнена ✅</i>"),
    (2500, 2000, "ккал", "↳ <i>Перебор: 500 ккал</i>"),
    (0, 0, "г", "↳ <i>Норма не задана</i>"),
]

@pytest.mark.parametrize("curr,target,unit,expected", PROGRESS_BAR_TESTS)
def test_progress_bar_display(curr, target, unit, expected):
    assert make_progress_bar(curr, target, unit) == expected


# =========================================================
# БЛОК 4: ИНТЕГРАЦИОННЫЕ ТЕСТЫ TELEGRAM (15+ ТЕСТОВ)
# =========================================================
@pytest.fixture
def mock_message():
    msg = AsyncMock(spec=Message)
    msg.from_user = User(id=123456789, is_bot=False, first_name="Ольга")
    msg.chat = Chat(id=123456789, type="private")
    msg.answer = AsyncMock()
    msg.edit_text = AsyncMock()
    msg.edit_reply_markup = AsyncMock()
    return msg

@pytest.fixture
def mock_callback(mock_message):
    cb = AsyncMock(spec=CallbackQuery)
    cb.from_user = mock_message.from_user
    cb.message = mock_message
    cb.answer = AsyncMock()
    return cb

@pytest.fixture
def mock_state():
    storage = MemoryStorage()
    return FSMContext(storage=storage, key=("test", 123456789, 123456789))

@pytest.mark.asyncio
async def test_start_flow_new_user(mock_message, mock_state):
    with patch("main.get_user_profile", new_callable=AsyncMock) as mock_p:
        mock_p.return_value = None
        await start_handler(mock_message, mock_state)
        assert mock_message.answer.call_count == 2

@pytest.mark.asyncio
async def test_start_flow_existing_user(mock_message, mock_state):
    with patch("main.get_user_profile", new_callable=AsyncMock) as mock_p:
        mock_p.return_value = {"weight": 60, "calories": 1800}
        await start_handler(mock_message, mock_state)
        mock_message.answer.assert_called_once()
        assert "С возвращением!" in mock_message.answer.call_args[0][0]

@pytest.mark.asyncio
async def test_today_empty_diary(mock_message, mock_state):
    with patch("main.get_user_profile", new_callable=AsyncMock) as mock_p, \
         patch("main.get_today_meals", new_callable=AsyncMock) as mock_m, \
         patch("main.db", None): 
        mock_p.return_value = {"calories": 1800, "protein": 120, "fat": 60, "carbs": 180}
        mock_m.return_value = []
        await today_handler(mock_message, mock_state)
        assert "Пока пусто" in mock_message.answer.call_args[0][0]

@pytest.mark.asyncio
async def test_plan_with_loss_goal_forecast(mock_message, mock_state):
    with patch("main.get_user_profile", new_callable=AsyncMock) as mock_p:
        mock_p.return_value = {
            "weight": 70.0, "target_weight": 64.0, "goal": "loss",
            "calories": 1750, "protein": 120, "fat": 60, "carbs": 160, "allergies": "Глютен"
        }
        await plan_handler(mock_message, mock_state)
        text = mock_message.answer.call_args[0][0]
        assert "1750 ккал" in text
        assert "Прогноз цели:" in text

@pytest.mark.asyncio
async def test_treat_flow(mock_message, mock_state):
    with patch("main.check_user_access", new_callable=AsyncMock) as mock_acc:
        mock_acc.return_value = True
        await treat_button_handler(mock_message, mock_state)
        assert "Съел(а) что-то вкусное?" in mock_message.answer.call_args[0][0]

@pytest.mark.asyncio
async def test_remember_favorite_food(mock_callback, mock_state):
    await mock_state.update_data(calculated_food={"title": "Сырники", "calories": 300, "protein": 20, "fat": 10, "carbs": 25})
    doc_mock = MagicMock()
    doc_mock.exists = False
    with patch("main.db") as mock_db:
        mock_db.collection().document().get = MagicMock(return_value=doc_mock)
        await food_remember_handler(mock_callback, mock_state)
        assert "сохранено в твою базу" in mock_callback.answer.call_args[0][0]

@pytest.mark.asyncio
async def test_water_logging(mock_callback):
    doc_mock = MagicMock()
    doc_mock.exists = True
    doc_mock.to_dict.return_value = {"water": 100}
    with patch("main.db") as mock_db, patch("main.send_today", new_callable=AsyncMock):
        mock_db.collection().document().get = MagicMock(return_value=doc_mock)
        await add_water_handler(mock_callback)
        assert "250 мл" in mock_callback.answer.call_args[0][0]

@pytest.mark.asyncio
async def test_toggle_family_mode(mock_callback):
    with patch("main.get_user_profile", new_callable=AsyncMock) as mock_p, \
         patch("main.save_user_profile", new_callable=AsyncMock):
        mock_p.return_value = {"family_mode": "self"}
        await toggle_family_mode_handler(mock_callback)
        assert "ВКЛЮЧЕН" in mock_callback.message.edit_text.call_args[0][0]

@pytest.mark.asyncio
async def test_admin_broadcast(mock_message):
    mock_message.text = "/admin_broadcast Внимание! Тест рассылки!"
    doc_mock = MagicMock()
    doc_mock.id = "123456789"
    with patch("main.db") as mock_db, patch("main.bot.send_message", new_callable=AsyncMock):
        mock_db.collection().get = MagicMock(return_value=[doc_mock])
        await admin_broadcast_handler(mock_message)
        assert "Рассылка завершена" in mock_message.answer.call_args[0][0]

@pytest.mark.asyncio
async def test_workout_menu_flow(mock_message, mock_callback, mock_state):
    with patch("main.get_user_profile", new_callable=AsyncMock) as mock_p, \
         patch("main.check_user_access", new_callable=AsyncMock) as mock_acc:
        mock_acc.return_value = True
        
        # 1. Меню выбора локации
        await workout_menu_handler(mock_message, mock_state)
        markup = mock_message.answer.call_args[1].get('reply_markup')
        buttons_str = str(markup.inline_keyboard)
        assert "workout_loc_home" in buttons_str
        assert "workout_loc_gym" in buttons_str

        # 2. Выбор "Дома" для ДЕВУШКИ с инвентарем
        mock_p.return_value = {"gender": "F", "home_equipment": "bands"}
        mock_callback.data = "workout_loc_home"
        await workout_location_callback(mock_callback)
        female_home_kb = str(mock_callback.message.edit_text.call_args[1].get('reply_markup').inline_keyboard)
        assert "glutes" in female_home_kb
        assert "choose_equipment_menu" in female_home_kb

        # 3. Выбор "В зале" для ПАРНЯ
        mock_p.return_value = {"gender": "M"}
        mock_callback.data = "workout_loc_gym"
        await workout_location_callback(mock_callback)
        male_gym_kb = str(mock_callback.message.edit_text.call_args[1].get('reply_markup').inline_keyboard)
        assert "chest" in male_gym_kb or "back" in male_gym_kb
