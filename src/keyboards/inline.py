"""Inline-клавиатуры."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..prompts import HERO_QUICK_PICKS, THEME_CHOICES


def main_menu_kb(
    continuation_hero: str | None = None,
    continuation_child: str | None = None,
) -> InlineKeyboardMarkup:
    """Главное меню. Минимальный набор: новая сказка, библиотека, подарок.
    Параметры continuation_* — legacy, игнорируются.

    Убрано:
    - «🌟 Тарифы и подписка» — теперь только в paywall и в левом меню (/plans).
    - «🌙 Помощь и забота» — оставлена команда /support в левом меню.
    """
    _ = continuation_hero, continuation_child
    kb = InlineKeyboardBuilder()
    kb.button(text="🪶 Сложить сегодняшнюю сказку", callback_data="story:new")
    kb.button(text="📚 Наша книжная полка", callback_data="lib:open")
    kb.button(text="💌 Пригласить близких — сказка в подарок", callback_data="ref:share")
    kb.adjust(1)
    return kb.as_markup()


def age_kb() -> InlineKeyboardMarkup:
    """Клавиатура выбора возраста: 3-4 или 5-6 лет.
    Возраст определяет, какой промпт получит сказочник:
    - 3-4 → toddler-промпт (8 архитектур, проще, обязательная счастливая развязка)
    - 5-6 → основной промпт (25 архитектур, сюрреализм, философская грань)
    В callback_data передаём середину диапазона (4 или 6).
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="👶 3-4 года", callback_data="age:4")
    kb.button(text="🧒 5-6 лет", callback_data="age:6")
    kb.button(text="◀ Назад", callback_data="story:cancel")
    kb.adjust(2, 1)
    return kb.as_markup()


def hero_kb() -> InlineKeyboardMarkup:
    """Клавиатура выбора героя. Эмодзи только в LABEL кнопки —
    в callback_data идёт чистое имя, чтоб не таскать эмодзи по БД и промптам."""
    kb = InlineKeyboardBuilder()
    for name, emoji in HERO_QUICK_PICKS.items():
        kb.button(text=f"{emoji} {name}", callback_data=f"hero:{name}")
    kb.button(text="✏️ Свой вариант", callback_data="hero:custom")
    kb.button(text="◀ Назад", callback_data="story:cancel")
    kb.adjust(2, 2, 2, 2, 2, 1, 1)
    return kb.as_markup()


def theme_kb() -> InlineKeyboardMarkup:
    """Клавиатура выбора темы. 14 тем + кнопка «Назад» → 7 рядов по 2 + 1.
    Автоматически адаптируется если добавятся новые темы в THEME_CHOICES."""
    kb = InlineKeyboardBuilder()
    # THEME_CHOICES хранит tuple из 3 элементов (label, desc, title_phrase),
    # для кнопки нужен только label — берём первый.
    for key, values in THEME_CHOICES.items():
        kb.button(text=values[0], callback_data=f"theme:{key}")
    kb.button(text="◀ Назад", callback_data="story:cancel")
    # Распределяем темы по 2 в ряд + последняя строка с кнопкой «Назад» (1 в ряд).
    # Считаем динамически — если темы будут добавляться/убираться, всё подстроится.
    n_themes = len(THEME_CHOICES)
    rows = [2] * (n_themes // 2)
    if n_themes % 2:
        rows.append(1)  # хвост-кнопка темы
    rows.append(1)  # «Назад»
    kb.adjust(*rows)
    return kb.as_markup()


def length_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Короткая (2-3 мин)", callback_data="length:short")
    kb.button(text="Средняя (4-5 мин)", callback_data="length:medium")
    kb.button(text="◀ Назад", callback_data="story:cancel")
    kb.adjust(1)
    return kb.as_markup()


def paywall_kb(can_referral: bool = True) -> InlineKeyboardMarkup:
    """Премиум-paywall: три тарифа от дешёвого к дорогому, потом реферал.

    Сортировка: разовая → пакет → подписка. Юзер сначала видит самую низкую
    цену, чтобы не отпугнуть. Скидки −34% и −50% подсвечены в названии —
    мгновенный сигнал «выгодно».

    Подарочная сказка временно убрана из UI — фокусируемся на разовой/пакете/
    подписке. Код gift handler оставлен, легко вернуть кнопку.
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="🕯 Одна сказка на вечер — 99 ₽", callback_data="bill:single")
    kb.button(text="📖 Пакет из 15 сказок — 999 ₽ (−34%)", callback_data="bill:pack")
    kb.button(text="🌙 Сказка каждый вечер на месяц — 1485 ₽ (−50%)", callback_data="bill:monthly")
    if can_referral:
        kb.button(text="💌 Пригласить близких", callback_data="ref:share")
    kb.button(text="◀ Вернуться в меню", callback_data="menu:main")
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
    """Клавиатура после сказки. Параметры has_sequel/hero/child_name
    оставлены в сигнатуре для обратной совместимости — больше не
    используются (продолжение серии убрано, см. main_menu_kb)."""
    _ = has_sequel, hero, child_name
    kb = InlineKeyboardBuilder()
    kb.button(text="🪶 Ещё одну сказку", callback_data="story:new")
    kb.button(text="🎁 Подарить эту сказку другу", callback_data=f"gift:share:{story_id}")
    kb.button(text="◀ В меню", callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()
