import pytest
from main import calculate_norm, extract_json

# =========================================================
# ТЕСТЫ ДЛЯ МАТЕМАТИКИ И ФОРМУЛ (calculate_norm)
# =========================================================
def test_calculate_norm_female_loss_protection():
    # Имитируем: Девушка, 34 года, 165 см, 63 кг, цель: похудение, активность: низкая
    norm = calculate_norm("F", 34, 165, 63.0, "loss", "low")
    
    # Базовый обмен (BMR) для нее ~1330 ккал. Бот не должен опускать норму ниже!
    assert norm["calories"] >= 1330, f"Ошибка: Калории упали до {norm['calories']} (ниже BMR)!"
    
    # Проверяем математику БЖУ (30% / 30% / 40%)
    calc_cals = (norm["protein"] * 4) + (norm["fat"] * 9) + (norm["carbs"] * 4)
    assert abs(norm["calories"] - calc_cals) <= 15, "Ошибка: Сумма БЖУ не сходится с общими калориями!"

def test_calculate_norm_male_gain():
    # Имитируем: Мужчина, 25 лет, 180 см, 70 кг, цель: набор массы, активность: высокая
    norm = calculate_norm("M", 25, 180, 70.0, "gain", "high")
    
    # Для таких параметров профицит должен быть солидным
    assert norm["calories"] > 2800, "Ошибка: Для набора массы калорий рассчитано слишком мало!"

def test_calculate_norm_extreme_low_weight():
    # Защита от аномальных данных: девушка весом 35 кг хочет похудеть
    norm = calculate_norm("F", 20, 160, 35.0, "loss", "low")
    
    # Жесткая заглушка в нашем коде стоит на 1200 ккал (минимальный порог выживания)
    assert norm["calories"] >= 1200, "Ошибка: Не сработала защита от экстремально низких калорий!"

# =========================================================
# ТЕСТЫ ДЛЯ ПАРСЕРА ИИ (extract_json)
# =========================================================
def test_extract_json_perfect_response():
    # Идеальный ответ от нейросети в формате Markdown
    ai_text = '```json\n{"title": "Овсянка", "protein": 10, "fat": 5, "carbs": 50}\n```'
    data = extract_json(ai_text)
    
    assert data["title"] == "Овсянка"
    # Наш код должен сам посчитать: 10*4 + 5*9 + 50*4 = 40 + 45 + 200 = 285 ккал
    assert data["calories"] == 285

def test_extract_json_bad_ai_math():
    # ИИ ошибся с математикой и написал 900 калорий для обычного яблока
    ai_text = '{"title": "Яблоко", "protein": 0, "fat": 0, "carbs": 20, "calories": 900}'
    data = extract_json(ai_text)
    
    # Наш код не должен верить ИИ. Он пересчитывает сам: 20*4 = 80 ккал
    assert data["calories"] == 80, "Ошибка: Код поверил неправильным калориям от ИИ!"

def test_extract_json_negative_numbers():
    # ИИ сошел с ума и выдал отрицательные граммы
    ai_text = '{"title": "Странная еда", "protein": -5, "fat": -10, "carbs": 10}'
    data = extract_json(ai_text)
    
    # Наш код должен обнулить отрицательные значения
    assert data["protein"] == 0
    assert data["fat"] == 0
    assert data["carbs"] == 10
    assert data["calories"] == 40 # Только углеводы дали калории

def test_extract_json_invalid_format():
    # ИИ выдал обычный текст вместо JSON
    ai_text = "Я думаю, что это яблоко. В нем 50 калорий и 10 углеводов."
    
    # Код должен честно выкинуть ошибку (чтобы мы могли ее перехватить и попросить ИИ ответить заново)
    with pytest.raises(ValueError, match="AI не вернул валидный JSON"):
        extract_json(ai_text)
