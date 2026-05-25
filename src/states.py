"""FSM-состояния мастера создания сказки."""
from aiogram.fsm.state import State, StatesGroup


class StoryWizard(StatesGroup):
    # Выбор ребёнка из списка ранее использованных имён + «Другое имя».
    # Если у юзера уже есть истории — он каждый раз САМ выбирает имя.
    waiting_name_choice = State()
    # Ввод НОВОГО имени (если выбрано «Другое имя» или это первая сказка).
    waiting_child_name = State()
    # Возраст спрашиваем кнопками 3-4 / 5-6 — определяет выбор промпта
    # (toddler-промпт для младших, основной с 25 архитектурами для старших).
    waiting_child_age = State()
    waiting_hero = State()
    waiting_theme = State()
    # waiting_length убран — у нас фиксированный формат «одна полноценная
    # сказка на ночь» (500-700 слов, ~4-5 минут с озвучкой). Выбор длины
    # создавал лишний шаг и сомнение: «короткая или средняя?» В ритуале
    # на ночь это не нужно — отдаём один правильный вариант.


class GiftWizard(StatesGroup):
    waiting_recipient_name = State()
    waiting_recipient_age = State()
    waiting_hero = State()
    waiting_theme = State()
    waiting_personal_note = State()


class FeedbackFlow(StatesGroup):
    """Юзер пишет критику после первой сказки. За критику — бонусная сказка."""
    waiting_text = State()
