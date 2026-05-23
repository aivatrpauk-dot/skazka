"""FSM-состояния мастера создания сказки."""
from aiogram.fsm.state import State, StatesGroup


class StoryWizard(StatesGroup):
    waiting_child_name = State()
    # waiting_child_age убран — наши сказки только для 3-6 лет, возраст не спрашиваем
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
