"""FSM-состояния мастера создания сказки."""
from aiogram.fsm.state import State, StatesGroup


class StoryWizard(StatesGroup):
    # Выбор ребёнка из списка ранее использованных имён + «Другое имя».
    # Если у юзера уже есть истории — он каждый раз САМ выбирает имя.
    waiting_name_choice = State()
    # Ввод НОВОГО имени (если выбрано «Другое имя» или это первая сказка).
    # После ввода имени сразу запускается генерация — больше шагов нет.
    waiting_child_name = State()
    # waiting_child_age убран в мае 2026 — возрастной сплит storyteller-промпта
    # удалён (см. prompts.STORYTELLER_VARIANTS). child_age=6 как дефолт.
    # waiting_hero / waiting_theme — legacy, в текущем флоу не достижимы
    # (hero и theme не спрашиваем — сказочник сам решает). Состояния
    # оставлены, чтобы старые stale-callback'и из устаревших keyboard'ов
    # на стороне клиента не падали с ошибкой неизвестного state.
    waiting_hero = State()
    waiting_theme = State()


class GiftWizard(StatesGroup):
    waiting_recipient_name = State()
    waiting_recipient_age = State()
    waiting_hero = State()
    waiting_theme = State()
    waiting_personal_note = State()


class FeedbackFlow(StatesGroup):
    """Юзер пишет критику после первой сказки. За критику — бонусная сказка."""
    waiting_text = State()
