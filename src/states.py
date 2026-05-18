"""FSM-состояния мастера создания сказки."""
from aiogram.fsm.state import State, StatesGroup


class StoryWizard(StatesGroup):
    waiting_child_name = State()
    waiting_child_age = State()
    waiting_hero = State()
    waiting_theme = State()
    waiting_length = State()


class GiftWizard(StatesGroup):
    waiting_recipient_name = State()
    waiting_recipient_age = State()
    waiting_hero = State()
    waiting_theme = State()
    waiting_personal_note = State()
