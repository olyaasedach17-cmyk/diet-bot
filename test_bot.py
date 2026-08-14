import pytest
import asyncio
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from aiogram.types import Message, Chat, User, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

# Импортируем из нашего бота нужные функции
from main import (
    calculate_norm, 
    extract_json, 
    start_handler, 
    today_handler,
    plan_handler
)

# =========================================================
# БЛОК 1: ТЕСТЫ ДЛЯ МАТЕМАТИКИ И ФОРМУЛ (Офлайн)
# =========================================================
def test_calculate_norm_female_loss_protection():
    norm = calculate_norm("F", 34, 165, 63.0, "loss", "low")
    assert norm["calories"] >= 1330, f"Ошибка: Калории упали до {norm['calories']} (ниже BMR)!"
    calc_cals = (norm["protein"] * 4) + (norm["fat"] * 9) + (norm["carbs"] * 4)
    assert abs(norm["calories"] - calc_cals) <= 15, "Ошибка: Сумма БЖУ не сходится!"

def test_calculate_norm_male_gain():
    norm = calculate_norm("M", 25, 180, 70.0, "gain", "high")
    assert norm["calories"] > 2800, "Ошибка: Для набора массы калорий слишком мало!"

def test_calculate_norm_extreme_low_weight():
    norm = calculate_norm("F", 20, 160, 35.0, "loss", "low")
    assert norm["calories"] >= 1200, "Ошибка: Защита от низких калорий не сработала!"

def test_extract_json_perfect_response():
    ai_text = '```json\n{"title": "Овсянка", "protein": 10, "fat": 5, "carbs": 50}\n```'
    data = extract_json(ai_text)
    assert data["title"] == "Овсянка"
    assert data["calories"] == 285

def test_extract_json_bad_ai_math():
    ai_text = '{"title": "Яблоко", "protein": 0, "fat": 0, "carbs": 20, "calories": 900}'
    data = extract_json(ai_text)
    assert data["calories"] == 80, "Ошибка: Код поверил неправильным калориям от ИИ!"

def test_extract_json_negative_numbers():
    ai_text = '{"title": "Странная еда", "protein": -5, "fat": -10, "carbs": 10}'
    data = extract_json(ai_text)
    assert data["protein"] == 0
    assert data["fat"] == 0
    assert data["calories"] == 40

def test_extract_json_invalid_format():
    ai_text = "Я думаю, что это яблоко. 50 калорий."
    with pytest.raises(ValueError, match="AI не вернул валидный JSON"):
        extract_json(ai_text)

# =========================================================
# БЛОК 2: ИНТЕГРАЦИОННЫЕ ТЕСТЫ (Проверка кнопок и команд)
# =========================================================

# Фикстуры для создания виртуального окружения Telegram
@pytest.fixture
def mock_bot():
    return AsyncMock(spec=Bot)

@pytest.fixture
def mock_message():
    msg = AsyncMock(spec=Message)
    msg.from_user = User(id=123456789, is_bot=False, first_name="TestUser")
    msg.chat = Chat(id=123456789, type="private")
    # Добавляем явное указание, что методы отправки сообщений - асинхронные
    msg.answer = AsyncMock()
    msg.edit_text = AsyncMock()
    msg.answer_photo = AsyncMock()
    return msg

@pytest.fixture
def mock_state(mock_bot):
    storage = MemoryStorage()
    return FSMContext(storage=storage, key=("test", 123456789, 123456789))

@pytest.mark.asyncio
async def test_start_command_new_user(mock_message, mock_state):
    """Тест: Новый пользователь нажимает /start. Должно появиться приветствие и кнопка 'Начать'."""
    
    # Подменяем обращение к базе данных, как будто пользователя там еще нет
    with patch("main.get_user_profile", new_callable=AsyncMock) as mock_get_profile:
        mock_get_profile.return_value = None
        
        await start_handler(mock_message, mock_state)
        
        # Проверяем, что бот ответил дважды (приветствие и просьба нажать кнопку)
        assert mock_message.answer.call_count == 2
        
        # Проверяем текст первого сообщения (ищем ключевые слова)
        args, kwargs = mock_message.answer.call_args_list[0]
        assert "Привет!" in args[0]
        assert "Пробный период" in args[0]
        
        # Проверяем, что появилась клавиатура с кнопкой "start_onb"
        args, kwargs = mock_message.answer.call_args_list[1]
        reply_markup = kwargs.get("reply_markup")
        assert reply_markup is not None
        assert reply_markup.inline_keyboard[0][0].callback_data == "start_onb"

@pytest.mark.asyncio
async def test_start_command_existing_user(mock_message, mock_state):
    """Тест: Старый пользователь нажимает /start. Бот должен сразу просить фото."""
    
    with patch("main.get_user_profile", new_callable=AsyncMock) as mock_get_profile:
        # Имитируем, что пользователь уже есть в базе
        mock_get_profile.return_value = {"weight": 60, "goal": "loss"}
        
        await start_handler(mock_message, mock_state)
        
        # Бот должен ответить только один раз
        mock_message.answer.assert_called_once()
        args, kwargs = mock_message.answer.call_args
        assert "С возвращением!" in args[0]
        assert "Пришли фото еды" in args[0]

@pytest.mark.asyncio
async def test_today_button_without_registration(mock_message, mock_state):
    """Тест: Нажатие '📊 Сегодня' без регистрации. Бот должен послать на /start."""
    
    with patch("main.get_user_profile", new_callable=AsyncMock) as mock_get_profile:
        mock_get_profile.return_value = None
        
        await today_handler(mock_message, mock_state)
        
        mock_message.answer.assert_called_once()
        args, kwargs = mock_message.answer.call_args
        assert "Сначала нажмите /start." in args[0]

@pytest.mark.asyncio
async def test_plan_command_with_user(mock_message, mock_state):
    """Тест: Команда /plan (Моя норма). Проверяем, что выводится расчет и аллергии."""
    
    with patch("main.get_user_profile", new_callable=AsyncMock) as mock_get_profile:
        # Имитируем профиль
        mock_get_profile.return_value = {
            "weight": 70.0,
            "target_weight": 65.0,
            "goal": "loss",
            "calories": 1800,
            "protein": 110,
            "fat": 60,
            "carbs": 150,
            "allergies": "Орехи"
        }
        
        await plan_handler(mock_message, mock_state)
        
        mock_message.answer.assert_called_once()
        args, kwargs = mock_message.answer.call_args
        text = args[0]
        
        # Проверяем, что все важные данные есть в тексте
        assert "1800 ккал" in text
        assert "Белки: 110" in text
        assert "Орехи" in text
        assert "Прогноз цели:" in text # Убеждаемся, что наша фишка с датой работает!
