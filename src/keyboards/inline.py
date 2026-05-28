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
    # Витринный образец — первым пунктом. Юзер до покупки видит конкретный
    # пример продукта (PDF + обложка + текст), потом принимает решение.
    kb.button(text="🌟 Посмотреть образец сказки", callback_data="demo:show")
    kb.button(text="🪶 Сложить сегодняшнюю сказку", callback_data="story:new")
    kb.button(text="📚 Наша книжная полка", callback_data="lib:open")
    kb.button(text="💌 Пригласить близких — сказка в подарок", callback_data="ref:share")
    kb.adjust(1)
    return kb.as_markup()


def name_choice_kb(names: list[str]) -> InlineKeyboardMarkup:
    """Клавиатура выбора ребёнка — список ранее использованных имён.
    С мая 2026 лимит «одна сказка в день» стал per-user (не per-child) —
    юзер до этого окна не доходит, если сегодняшняя сказка уже была.
    Поэтому никаких маркеров «🌙 спит» / «🕯 ждёт» — все имена просто
    активны для клика.

    Кнопка «Другое имя» — ввести новое (например, племянник пришёл).
    """
    kb = InlineKeyboardBuilder()
    # До 5 последних имён в кнопках (больше не помещается в чате).
    for name in names[:5]:
        kb.button(
            text=f"🕯 {name}",
            callback_data=f"name:pick:{name}",
        )
    kb.button(text="✏️ Другое имя", callback_data="name:new")
    kb.button(text="◀ В меню", callback_data="story:cancel")
    kb.adjust(1)
    return kb.as_markup()


def gender_kb() -> InlineKeyboardMarkup:
    """Клавиатура выбора пола ребёнка — мальчик / девочка.

    Используется в обоих флоу (story + gift). Без варианта «не указывать»:
    пол важен для склонения имени, обращения к герою («он/она»), и
    выбора пола героя на иллюстрациях. Лучше спросить явно, чем
    угадывать из имени (Тася, Хрюша, Кузя могут быть как м, так и ж).
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="👦 Мальчик", callback_data="gender:male")
    kb.button(text="👧 Девочка", callback_data="gender:female")
    kb.adjust(2)
    return kb.as_markup()


# age_kb удалена в мае 2026 — возрастной шаг убран из обоих флоу
# (см. m_child_name / m_recipient_name). Если когда-то понадобится
# вернуть возрастной сплит — восстанавливать через git-историю.


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
    """Premium-paywall: ДВА тарифа — разовая и подписка. Пакет убран из UI
    (handler `bill:pack` оставлен в коде, легко вернуть кнопку если
    тестирование покажет нужду).

    Цены повышены на ребрендинге (май 2026):
      99 ₽ → 149 ₽ за разовую (премиум-позиционирование);
      1485 ₽ → 2990 ₽ за месяц (ежедневный ритуал).
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="🕯 Одна сказка на вечер — 149 ₽", callback_data="bill:single")
    kb.button(text="🌙 Сказка каждый вечер на месяц — 2990 ₽", callback_data="bill:monthly")
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


def daily_limit_kb() -> InlineKeyboardMarkup:
    """Клавиатура для сообщения отказа дневного лимита («для Маши сегодня
    сказка уже была»). Даёт явный путь к покупке сказки для другого
    ребёнка одним кликом — иначе юзер вынужден возвращаться в меню и
    проходить весь flow заново, что выглядит как тупик.
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Сделать для другого ребёнка", callback_data="name:new")
    kb.button(text="◀ В меню", callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()


def after_story_kb(
    story_id: int,
    has_sequel: bool = False,
    hero: str | None = None,
    child_name: str | None = None,
) -> InlineKeyboardMarkup:
    """Клавиатура после сказки. Параметры story_id/has_sequel/hero/child_name
    оставлены в сигнатуре для обратной совместимости — больше не используются.

    Почему так минималистично:
    - «Ещё одну сказку» УБРАНА. У нас жёсткий лимит — одна сказка в день
      для всех (даже для подписки и пакета). Кнопка вела на ложное обещание:
      юзер кликал, проходил визард, и его блокировал daily-лимит.
    - «🎁 Подарить сказку другу» — ведёт на ОБЩИЙ gift-флоу (создание новой
      подарочной сказки для близкого), не на «переслать эту сказку». Тоже
      логичнее: люди не «делятся прочитанной сказкой», они «дарят сказку».
    - «В меню» — возврат в главное меню.
    """
    _ = story_id, has_sequel, hero, child_name
    kb = InlineKeyboardBuilder()
    kb.button(text="🎁 Подарить сказку другу", callback_data="gift:new")
    kb.button(text="◀ В меню", callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()
