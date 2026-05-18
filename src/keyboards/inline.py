"""Inline-клавиатуры."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..prompts import HERO_QUICK_PICKS, THEME_CHOICES
from ..utils import accusative, hero_accusative


def main_menu_kb(
    continuation_hero: str | None = None,
    continuation_child: str | None = None,
) -> InlineKeyboardMarkup:
    """Главное меню. Если у юзера есть прошлая сказка, первая кнопка превращается
    в «Новое приключение про {child} и {hero}» — антология, не cliffhanger.
    Никаких обещаний выполнить — просто продолжение знакомства."""
    kb = InlineKeyboardBuilder()
    if continuation_hero and continuation_child:
        kb.button(
            text=f"🔮 Новое приключение про {accusative(continuation_child)} и {hero_accusative(continuation_hero)}",
            callback_data="story:continue",
        )
        kb.button(text="🌟 Другие герои, другой мир", callback_data="story:new")
    else:
        kb.button(text="Создать сказку", callback_data="story:new")
    kb.button(text="Мои сказки", callback_data="lib:open")
    kb.button(text="Подарить сказку", callback_data="gift:new")
    kb.button(text="Подписка и тарифы", callback_data="bill:plans")
    kb.button(text="Пригласить друга +3 сказки", callback_data="ref:share")
    kb.button(text="Помощь", callback_data="faq:open")
    kb.adjust(1)
    return kb.as_markup()


def age_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for label, val in [("3–4 года", 4), ("5–6 лет", 6), ("7–8 лет", 7), ("9–11 лет", 10)]:
        kb.button(text=label, callback_data=f"age:{val}")
    kb.button(text="◀ Назад", callback_data="story:cancel")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def hero_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for h in HERO_QUICK_PICKS:
        kb.button(text=h, callback_data=f"hero:{h}")
    kb.button(text="Свой вариант", callback_data="hero:custom")
    kb.button(text="◀ Назад", callback_data="story:cancel")
    kb.adjust(2, 2, 2, 2, 2, 1, 1)
    return kb.as_markup()


def theme_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key, (label, _) in THEME_CHOICES.items():
        kb.button(text=label, callback_data=f"theme:{key}")
    kb.button(text="◀ Назад", callback_data="story:cancel")
    kb.adjust(2, 2, 2, 1)
    return kb.as_markup()


def length_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Короткая (2-3 мин)", callback_data="length:short")
    kb.button(text="Средняя (4-5 мин)", callback_data="length:medium")
    kb.button(text="◀ Назад", callback_data="story:cancel")
    kb.adjust(1)
    return kb.as_markup()


def paywall_kb(can_referral: bool = True) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Подписка 490 ₽/мес — безлимит", callback_data="bill:sub")
    kb.button(text="Подарить близкому — 199 ₽", callback_data="gift:new")
    if can_referral:
        kb.button(text="Пригласить друга и получить +3", callback_data="ref:share")
    kb.button(text="◀ В меню", callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()


def library_kb(stories: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for sid, title in stories[:20]:
        kb.button(text=title, callback_data=f"lib:show:{sid}")
    kb.button(text="◀ В меню", callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()


def after_story_kb(
    story_id: int,
    has_sequel: bool = False,
    hero: str | None = None,
    child_name: str | None = None,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if has_sequel and hero and child_name:
        kb.button(
            text=f"🔮 Завтра — новое приключение про {accusative(child_name)} и {hero_accusative(hero)}",
            callback_data="story:continue",
        )
        kb.button(text="🌟 Другие герои, другой мир", callback_data="story:new")
    else:
        kb.button(text="Ещё одну сказку", callback_data="story:new")
    kb.button(text="Подарить эту сказку другу", callback_data=f"gift:share:{story_id}")
    kb.button(text="◀ В меню", callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()
