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
    process_smart_input,
    show_favorite_foods_handler,
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
    ('{"title": "Салат", "protein": 2, "fat": 10, "carbs": 5, "ingredients": [{"name": "Огурец", "weight_g": 100}]}', "Салат", 2, 10, 5),
    ('{"title": "Творог", "protein": 18, "fat": -2, "carbs": 3}', "Творог", 18, 0, 3),
    ('{"title": "Сыр", "protein": "15", "fat": "20", "carbs": "1"}', "Сыр", 15, 20, 1),
    ('{"title": "", "protein": 10, "fat": 5, "carbs": 10}', "Приём пищи", 10, 5, 10)
] * 5

@pytest.mark.parametrize("ai_raw,expected_title,p,f,c", JSON_AI_VARIATIONS)
def test_extract_json_resilience_matrix(ai_raw, expected_title, p, f, c):
    res = extract_json(ai_raw)
    assert res["title"] == expected_title
    assert res["protein"] == int(p)
    assert res["fat"] == int(f)
    assert res["carbs"] == int(c)
    assert res["calories"] == int((p * 4) + (f * 9) + (c * 4))
    assert isinstance(res.get("ingredients"), list), "Поле ingredients должно быть списком!"

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
    (0, 2000, "🥩", "⚪", "⚪⚪⚪⚪⚪⚪⚪"),
    (1000, 2000, "⚡", "⚪", "⚡⚡⚡⚡⚪⚪⚪"),
    (2000, 2000, "🥑", "⚪", "🥑🥑🥑🥑🥑🥑🥑"),
    (2500, 2000, "⚡", "⚪", "⚡⚡⚡⚡⚡⚡⚡"),
    (0, 0, "🥩", "⚪", "⚪⚪⚪⚪⚪⚪⚪"),
]

@pytest.mark.parametrize("curr,target,active,inactive,expected", PROGRESS_BAR_TESTS)
def test_progress_bar_display(curr, target, active, inactive, expected):
    assert make_progress_bar(curr, target, active, inactive) == expected

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
         patch("main.check_user_access", new_callable=AsyncMock) as mock_access, \
         patch("main.db", None): 
        
        mock_p.return_value = {"calories": 1800, "protein": 120, "fat": 60, "carbs": 180}
        mock_m.return_value = []
        mock_access.return_value = True 
        
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
    await mock_state.update_data(calculated_food={
        "title": "Сырники", "calories": 300, "protein": 20, "fat": 10, "carbs": 25,
        "ingredients": [{"name": "Творог", "weight_g": 150, "calories": 150}],
        "original_description": "150г творога"
    })
    
    doc_mock = MagicMock()
    doc_mock.exists = False
    doc_mock.to_dict.return_value = {}
    
    with patch("main.db") as mock_db:
        mock_user_doc = MagicMock()
        mock_db.collection().document.return_value = mock_user_doc
        mock_user_doc.get.return_value = doc_mock
        
        mock_dish_doc = MagicMock()
        mock_user_doc.collection().document.return_value = mock_dish_doc
        
        await food_remember_handler(mock_callback, mock_state)
        assert "сохранено в твою базу" in mock_callback.answer.call_args[0][0]
        mock_dish_doc.set.assert_called()

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
async def test_profile_favorite_foods_button(mock_message, mock_callback, mock_state):
    with patch("main.get_user_profile", new_callable=AsyncMock) as mock_p:
        mock_p.return_value = {
            "weight": 65,
            "favorite_foods": [
                {"title": "Сметанник без сахара", "calories": 360, "protein": 7, "fat": 29, "carbs": 18}
            ]
        }
        
        await profile_handler(mock_message, mock_state)
        markup = mock_message.answer.call_args[1].get('reply_markup')
        buttons_str = str(markup.inline_keyboard)
        assert "show_favorite_foods" in buttons_str, "Кнопка базы блюд не найдена в профиле!"

        from main import show_favorite_foods_handler
        mock_callback.data = "show_favorite_foods"
        await show_favorite_foods_handler(mock_callback)
        
        response_text = mock_callback.message.edit_text.call_args[0][0]
        assert "Сметанник без сахара" in response_text
        assert "360 ккал" in response_text

# =========================================================
# ТЕСТЫ ОПЛАТЫ
# =========================================================

@pytest.mark.asyncio
@patch("main.create_bepaid_bill", new_callable=AsyncMock)
async def test_buy_subscription_handler(mock_create_bill, mock_callback):
    mock_callback.data = "buy_3_months"
    mock_create_bill.return_value = "https://fake-pay-link.com"
    
    from main import buy_subscription_handler
    await buy_subscription_handler(mock_callback)
    
    mock_create_bill.assert_called_once_with(mock_callback.from_user.id, 29.0, 3)
    response_text = mock_callback.message.edit_text.call_args[0][0]
    assert "Оформление подписки на <b>3 мес.</b>" in response_text
    assert "29.0 BYN" in response_text
    
    reply_markup = mock_callback.message.edit_text.call_args[1]["reply_markup"]
    assert reply_markup.inline_keyboard[0][0].url == "https://fake-pay-link.com"

# =========================================================
# ТЕСТЫ НОВОГО ФУНКЦИОНАЛА: МОИ БЛЮДА И ОПЛАТА
# =========================================================

def test_calculate_saved_dish_portion():
    from main import calculate_saved_dish_portion
    
    dish = {
        "title": "Курица с гречкой",
        "calories": 300, "protein": 30, "fat": 5, "carbs": 40,
        "ingredients": [
            {"name": "Курица", "weight_g": 100, "calories": 165, "protein": 31, "fat": 3, "carbs": 0},
            {"name": "Гречка", "weight_g": 100, "calories": 135, "protein": 5, "fat": 2, "carbs": 29}
        ]
    }
    
    new_dish = calculate_saved_dish_portion(dish, 300.0)
    assert new_dish["calories"] == 450
    assert new_dish["protein"] == 45
    assert new_dish["fat"] == 7 
    assert new_dish["carbs"] == 60
    assert new_dish["ingredients"][0]["weight_g"] == 150
    assert new_dish["ingredients"][0]["protein"] == 46 


@pytest.mark.asyncio
async def test_start_handler_with_utm(mock_message, mock_state):
    from main import start_handler
    from unittest.mock import MagicMock, AsyncMock, patch 
    
    mock_message.text = "/start fb_cpc_promo"
    mock_message.from_user = MagicMock()
    mock_message.from_user.id = 12345
    
    mock_state.update_data = AsyncMock()
    
    with patch("main.get_user_profile", return_value=None):
        await start_handler(mock_message, mock_state)
        
        mock_state.update_data.assert_called_with(
            start_parameter="fb_cpc_promo",
            utm_source="fb",
            utm_medium="cpc",
            utm_campaign="promo"
        )

@pytest.mark.asyncio
async def test_bepaid_webhook_fraud_protection():
    from main import bepaid_webhook_handler
    from aiohttp import web
    
    mock_request = AsyncMock(spec=web.Request)
    mock_request.json.return_value = {
        "transaction": {
            "tracking_id": "sub_123_1",
            "status": "successful",
            "amount": 1, 
            "currency": "BYN"
        }
    }
    
    with patch("main.db") as mock_db:
        mock_doc = MagicMock()
        mock_doc.exists = True
        mock_doc.to_dict.return_value = {
            "status": "pending",
            "amount": 15.0,
            "currency": "BYN"
        }
        
        mock_collection = MagicMock()
        mock_db.collection.return_value = mock_collection
        mock_collection.document.return_value = mock_doc
        
        with patch("asyncio.to_thread", return_value=mock_doc):
            response = await bepaid_webhook_handler(mock_request)
            
            assert response.status == 200
            mock_doc.update.assert_not_called()

# =========================================================
# ИСПРАВЛЕННЫЕ ТЕСТЫ (Анкета 30 дней, Анализ и Тренировки)
# =========================================================

@pytest.mark.asyncio
async def test_finish_onboarding_30_days(mock_message, mock_state):
    from main import finish_onboarding
    from unittest.mock import patch, MagicMock
    
    mock_state.get_data = AsyncMock(return_value={
        "gender": "F", "age": 25, "height": 165, "weight": 60, "goal": "loss", "activity": "low"
    })
    
    mock_db = MagicMock()
    mock_doc = MagicMock()
    mock_doc.exists = False
    mock_db.collection.return_value.document.return_value.get = MagicMock(return_value=mock_doc)
    
    with patch("main.db", mock_db), \
         patch("main.calculate_norm", return_value={"calories": 1500, "protein": 100, "fat": 50, "carbs": 150}):
         
        await finish_onboarding(mock_message, mock_state, user_id=12345, allergy_text="Нет", is_cb=False)
        
        args, kwargs = mock_message.answer.call_args
        assert "Активировано 30 дней бесплатно!" in args[0]


@pytest.mark.asyncio
async def test_analyze_today_handler(mock_callback):
    from main import analyze_today_handler
    from unittest.mock import patch, AsyncMock, MagicMock
    
    mock_callback.data = "analyze_today"
    mock_callback.from_user = MagicMock()
    mock_callback.from_user.id = 12345
    
    with patch("main.get_user_profile", new_callable=AsyncMock) as mock_profile, \
         patch("main.get_today_meals", new_callable=AsyncMock) as mock_meals, \
         patch("main.ask_ai", new_callable=AsyncMock) as mock_ask_ai:
         
        mock_profile.return_value = {
            "calories": 2000, "protein": 150, "fat": 70, "carbs": 200, 
            "weight": 70, "goal": "loss"
        }
        mock_meals.return_value = [{"calories": 500, "protein": 30, "fat": 20, "carbs": 50}]
        mock_ask_ai.return_value = "Твой анализ: добавь больше белка на ужин!"
        
        await analyze_today_handler(mock_callback)
        
        mock_ask_ai.assert_called_once()
        args, kwargs = mock_callback.message.edit_text.call_args
        assert "Анализ дня от нутрициолога" in args[0]
        assert "Твой анализ: добавь больше белка на ужин!" in args[0]


@pytest.mark.asyncio
async def test_ask_workout_time_callback(mock_callback):
    from main import ask_workout_time_callback
    
    mock_callback.data = "gen_workout_home_glutes"
    await ask_workout_time_callback(mock_callback)
    
    args, kwargs = mock_callback.message.edit_text.call_args
    assert "Сколько времени у тебя есть" in args[0]
    assert "start_w_home_glutes_15" in str(kwargs['reply_markup'])


@pytest.mark.asyncio
async def test_generate_step_workout(mock_callback, mock_state):
    from main import generate_step_workout, WorkoutStates
    from unittest.mock import patch, AsyncMock, MagicMock
    
    mock_callback.data = "start_w_home_glutes_15"
    mock_callback.from_user = MagicMock()
    mock_callback.from_user.id = 12345
    
    with patch("main.get_user_profile", new_callable=AsyncMock) as mock_profile, \
         patch("main.ask_ai", new_callable=AsyncMock) as mock_ask_ai:
         
        mock_profile.return_value = {"weight": 60, "goal": "loss", "home_equipment": "dumbbells"}
        mock_ask_ai.return_value = "Разминка (5 мин)\nКрутим руками|||Приседания (15 раз)\nСпина прямая|||Отжимания (10 раз)\nДыши ровно"
        
        await generate_step_workout(mock_callback, mock_state)
        
        args, kwargs = mock_callback.message.edit_text.call_args
        assert "Шаг 1 из 3" in args[0]
        assert "Разминка (5 мин)" in args[0]
