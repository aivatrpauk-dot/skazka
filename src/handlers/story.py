"""Главный поток — создание сказки. FSM: имя → герой → тема → генерация.
Длина больше не выбирается: формат фиксированный — одна сказка на ночь ~500-700 слов."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, FSInputFile, Message
from sqlalchemy import desc, select, update

from ..config import config
from ..db import Session, Story, SubscriptionStatus, User
from ..keyboards import (
    after_story_kb,
    age_kb,
    daily_limit_kb,
    hero_kb,
    main_menu_kb,
    paywall_kb,
    theme_kb,
)
from ..prompts import HERO_QUICK_PICKS, THEME_CHOICES
from ..services import (
    extract_scene,
    generate_cover,
    generate_story,
    synthesize_speech,
    # summarize_story больше не используется — он был для антологии-продолжения,
    # которая теперь удалена (см. cb_story_continue).
)
from ..states import StoryWizard
from ..utils import (
    dative,
    genitive,
    # accusative, hero_accusative, hero_genitive, hero_instrumental, instrumental
    # больше не используются на верхнем уровне — после удаления cb_story_continue
    # и выбора героя в визарде. Локальный импорт _acc/_hero_acc в _run_generation
    # остался для совместимости со старой веткой PDF-заголовка «Сказка про X и Y».
    normalize_name,
    strip_emo_markers,
)

logger = logging.getLogger(__name__)
router = Router(name="story")


TELEGRAM_MSG_LIMIT = 4000  # с запасом до реального лимита 4096


def _split_for_telegram(text: str, limit: int = TELEGRAM_MSG_LIMIT) -> list[str]:
    """Бьёт длинный текст на части по абзацам, чтобы не упереться в лимит Telegram."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    buf = ""
    for paragraph in text.split("\n\n"):
        candidate = (buf + "\n\n" + paragraph) if buf else paragraph
        if len(candidate) <= limit:
            buf = candidate
        else:
            if buf:
                parts.append(buf)
            # сам абзац может быть слишком длинным — режем по предложениям
            if len(paragraph) <= limit:
                buf = paragraph
            else:
                # грубое деление по точкам
                sentences = paragraph.split(". ")
                tmp = ""
                for s in sentences:
                    cand = (tmp + ". " + s) if tmp else s
                    if len(cand) <= limit:
                        tmp = cand
                    else:
                        if tmp:
                            parts.append(tmp)
                        tmp = s
                buf = tmp
    if buf:
        parts.append(buf)
    return parts


def _has_active_subscription(u: User) -> bool:
    """Активная месячная подписка 1485 ₽."""
    if u.subscription_status != SubscriptionStatus.active:
        return False
    if not u.subscription_until:
        return False
    return u.subscription_until > dt.datetime.now(dt.timezone.utc)


# Legacy alias — старый код использует _is_paid. Оставляем чтобы не сломать
# импорты в других местах (например feedback, gift и т.д.).
_is_paid = _has_active_subscription


# Глобальный per-user cooldown (legacy). РЕАЛЬНЫЙ лимит «1 сказка в день
# на ребёнка» применяется per-child через _was_story_for_child_today()
# в _run_generation (см. ниже). Этот cooldown остаётся как доп. защита
# от спама для pack/sub источников (юзер с подпиской без явного имени).
DAILY_LIMIT_HOURS = 20  # с запасом ~24ч, чтоб ребёнок мог получить «вечером»


def _is_within_daily_cooldown(u: User) -> bool:
    if not u.last_story_at:
        return False
    elapsed = dt.datetime.now(dt.timezone.utc) - u.last_story_at
    return elapsed < dt.timedelta(hours=DAILY_LIMIT_HOURS)


# Источники, из которых юзер может «оплатить» сегодняшнюю сказку.
# Порядок проверки = порядок списания (от дешёвого/халявного к дорогому).
SOURCE_BONUS = "bonus"
SOURCE_FREE = "free"
SOURCE_SINGLE = "single"
SOURCE_PACK = "pack"
SOURCE_SUBSCRIPTION = "subscription"
# Спец-источник для админов: обходит ВСЕ лимиты (включая per-child),
# ничего не списывает. Нужен чтобы свободно тестировать прод без чисток.
SOURCE_ADMIN = "admin"


def _allowed_source(u: User) -> str | None:
    """Возвращает источник, по которому юзер может оплатить сказку.
    None — значит нет источника (нужен paywall).

    ВАЖНО: эта функция отвечает только за «есть ли чем заплатить», не за
    «можно ли сегодня для этого ребёнка». Per-child лимит «1 в день на
    ребёнка» проверяется отдельно через _was_story_for_child_today() в
    _run_generation, после того как известно имя ребёнка. Этот лимит
    применяется ко ВСЕМ источникам кроме админа — single тоже не
    позволит сделать 2 сказки одному ребёнку за день.

    Логика выбора источника (от халявного к дорогому):
      0. Админ → SOURCE_ADMIN (обходит всё, включая per-child).
      1. Бонусные сказки (от рефералки/feedback).
      2. Free trial первая сказка.
      3. Single-разовая покупка.
      4. Pack (если global per-user cooldown не активен).
      5. Subscription active (если global per-user cooldown не активен).
    """
    if u.telegram_id in config.admin_ids:
        return SOURCE_ADMIN
    if u.bonus_stories and u.bonus_stories > 0:
        return SOURCE_BONUS
    if (u.free_stories_used or 0) < config.free_story_limit:
        return SOURCE_FREE
    if u.single_stories_remaining and u.single_stories_remaining > 0:
        return SOURCE_SINGLE
    # Дальше — источники с daily-лимитом
    if _is_within_daily_cooldown(u):
        return None
    if u.pack_stories_remaining and u.pack_stories_remaining > 0:
        return SOURCE_PACK
    if _has_active_subscription(u):
        return SOURCE_SUBSCRIPTION
    return None


def _can_make_story(u: User) -> bool:
    """Быстрая проверка для legacy кода. True если есть хоть какой-то источник."""
    return _allowed_source(u) is not None


def _consume_story_source(u: User, source: str) -> None:
    """Списывает сказку с указанного источника. Вызывается ВНУТРИ сессии,
    после успешной генерации, перед commit().

    Также проставляет last_story_at для источников с daily-лимитом.
    Для SOURCE_ADMIN — ничего не списывается и last_story_at не ставится
    (админ может тестировать неограниченно).
    """
    now = dt.datetime.now(dt.timezone.utc)
    if source == SOURCE_ADMIN:
        # Админский обход — ничего не трогаем.
        return
    if source == SOURCE_BONUS:
        u.bonus_stories = max(0, (u.bonus_stories or 0) - 1)
    elif source == SOURCE_FREE:
        u.free_stories_used = (u.free_stories_used or 0) + 1
    elif source == SOURCE_SINGLE:
        u.single_stories_remaining = max(0, (u.single_stories_remaining or 0) - 1)
    elif source == SOURCE_PACK:
        u.pack_stories_remaining = max(0, (u.pack_stories_remaining or 0) - 1)
        u.last_story_at = now
    elif source == SOURCE_SUBSCRIPTION:
        u.last_story_at = now


def _paywall_reason_text(u: User) -> str:
    """Что показать юзеру когда _allowed_source вернул None."""
    if _is_within_daily_cooldown(u) and (
        u.pack_stories_remaining or _has_active_subscription(u)
    ):
        # Не закончились сказки, просто рано
        next_at = u.last_story_at + dt.timedelta(hours=DAILY_LIMIT_HOURS)
        return (
            "🌙 Сегодняшняя сказка уже сложена — её достаточно на этот вечер.\n"
            f"Следующая будет ждать Вас <b>{next_at.strftime('%d.%m в %H:%M')}</b>.\n\n"
            "Пока — просто побудьте рядом. Этого тоже хватит."
        )
    return (
        "🕯 Сказка, что была подарена Вам для знакомства, уже отзвучала. "
        "Чтобы наш вечерний ритуал не прерывался — выберите, что Вам "
        "ближе:"
    )


async def _get_user(telegram_id: int) -> User:
    async with Session() as s:
        return (await s.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one()


# cb_story_continue («Завтра — продолжение про того же героя») удалён:
# концепция продолжения серии устарела, теперь каждая сказка независимая,
# сказочник сам выбирает героя и архитектуру. См. main_menu_kb и after_story_kb.


async def _get_user_child_names(user_id: int) -> list[str]:
    """Возвращает список имён детей этого юзера в порядке последнего
    использования. Берём из таблицы Story (по DISTINCT child_name).
    Плюс если у юзера в User.child_name что-то лежит, а в Story ещё ничего
    нет (legacy) — кладём его в список первым.
    """
    from sqlalchemy import func
    async with Session() as s:
        rows = (await s.execute(
            select(
                Story.child_name,
                func.max(Story.created_at).label("last_used"),
            )
            .where(Story.user_id == user_id)
            .group_by(Story.child_name)
            .order_by(desc("last_used"))
            .limit(10)
        )).all()
        names = [r[0] for r in rows if r[0]]
        # Если у юзера в User.child_name есть имя без сказок (legacy) —
        # добавим первым, чтобы не потерять.
        u = (await s.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if u and u.child_name and u.child_name not in names:
            names.insert(0, u.child_name)
    return names


async def _was_story_for_child_today(user_id: int, child_name: str) -> bool:
    """Возвращает True, если у юзера УЖЕ была сказка для ЭТОГО ребёнка
    сегодня (по МСК). Дата сравнивается по МСК-календарю, не по часам.

    Это новая логика daily-лимита: лимит 1/день привязан к ИМЕНИ ребёнка,
    а не к юзеру в целом. Поэтому родитель с двумя детьми (Маша и Ваня)
    может сделать одну сказку для Маши и одну для Вани в один день.
    """
    import datetime as _dt
    async with Session() as s:
        last = (await s.execute(
            select(Story.created_at)
            .where(Story.user_id == user_id, Story.child_name == child_name)
            .order_by(desc(Story.created_at))
            .limit(1)
        )).scalar_one_or_none()
    if not last:
        return False
    MSK_TZ = _dt.timezone(_dt.timedelta(hours=3))
    now_msk = _dt.datetime.now(_dt.timezone.utc).astimezone(MSK_TZ).date()
    last_msk = last.astimezone(MSK_TZ).date()
    return last_msk == now_msk


async def _names_used_today(user_id: int, names: list[str]) -> set[str]:
    """Возвращает set имён из списка, для которых СЕГОДНЯ (по МСК) у юзера
    уже была сказка. Используется в name_choice_kb чтобы помечать имена
    с исчерпанным лимитом ещё до клика — юзер сразу видит, кому сегодня
    нельзя, и понимает что нужно ввести нового ребёнка.
    """
    import datetime as _dt
    if not names:
        return set()
    MSK_TZ = _dt.timezone(_dt.timedelta(hours=3))
    now_msk_date = _dt.datetime.now(_dt.timezone.utc).astimezone(MSK_TZ).date()
    # Граница «начало дня по МСК» в UTC = 00:00 МСК = 21:00 UTC предыдущего дня.
    midnight_msk = _dt.datetime.combine(
        now_msk_date, _dt.time(0, 0), tzinfo=MSK_TZ,
    )
    midnight_utc = midnight_msk.astimezone(_dt.timezone.utc)
    async with Session() as s:
        rows = (await s.execute(
            select(Story.child_name).distinct().where(
                Story.user_id == user_id,
                Story.child_name.in_(names),
                Story.created_at >= midnight_utc,
            )
        )).all()
    return {r[0] for r in rows if r[0]}


@router.callback_query(F.data == "story:new")
async def cb_story_new(call: CallbackQuery, state: FSMContext) -> None:
    u = await _get_user(call.from_user.id)
    if not _can_make_story(u):
        await call.message.edit_text(
            _paywall_reason_text(u),
            reply_markup=paywall_kb(),
        )
        await call.answer()
        return

    # Имя ВСЕГДА спрашиваем заново. Если у юзера уже были сказки — даём
    # список последних имён + «Другое имя». Если это первая сказка — сразу
    # просим написать имя.
    #
    # Имена, для которых сегодня уже была сказка, помечаем галочкой —
    # юзер не пройдёт визард впустую (раньше: выбор имени → возраст →
    # тема → ОТКАЗ; теперь видно «✅ Маша (сегодня уже была)» прямо в
    # списке, клик показывает alert, не запускает визард).
    from ..keyboards import name_choice_kb
    names = await _get_user_child_names(u.id)
    if names:
        used_today = await _names_used_today(u.id, names[:5])
        # Если ВСЕ имена в кнопках исчерпали лимит — мягко поясняем
        # контекст в заголовке, чтобы юзер не недоумевал почему всё в
        # галочках. Кнопка «Другое имя» остаётся доступной.
        if used_today and len(used_today) >= len(names[:5]):
            header = (
                "🌙 Для всех Ваших детей сегодня уже сложилась сказка.\n"
                "<i>Если хотите, можно сделать для другого ребёнка — "
                "введите его имя.</i>"
            )
        else:
            header = (
                "🕯 Для кого сегодня сказка?\n"
                "<i>Выберите ребёнка из списка или введите другое имя.</i>"
            )
        await call.message.edit_text(
            header,
            reply_markup=name_choice_kb(names, used_today=used_today),
        )
        await state.set_state(StoryWizard.waiting_name_choice)
        await call.answer()
        return

    await call.message.edit_text(
        "🕯 Как зовут ребёнка? Напишите имя.",
    )
    await state.set_state(StoryWizard.waiting_child_name)
    await call.answer()


@router.callback_query(F.data.startswith("name:pick:"))
async def cb_name_pick(call: CallbackQuery, state: FSMContext) -> None:
    """Юзер выбрал ИМЯ из списка прежних → сохраняем и идём к выбору возраста."""
    name = call.data.split(":", 2)[2]
    if not name:
        await call.answer("Пустое имя", show_alert=True)
        return
    await state.update_data(child_name=name)
    await call.message.edit_text(
        "Выбирайте версию, которая больше нравится ребёнку.",
        reply_markup=age_kb(),
    )
    await state.set_state(StoryWizard.waiting_child_age)
    await call.answer()


@router.callback_query(F.data == "name:new")
async def cb_name_new(call: CallbackQuery, state: FSMContext) -> None:
    """Юзер выбрал «Другое имя» → просим ввести новое."""
    await state.update_data(child_name=None)
    await call.message.edit_text(
        "🕯 Как зовут ребёнка? Напишите имя.",
    )
    await state.set_state(StoryWizard.waiting_child_name)
    await call.answer()


@router.callback_query(F.data.startswith("name:done:"))
async def cb_name_done(call: CallbackQuery) -> None:
    """Юзер кликнул на имя, помеченное «✅ … (сегодня уже была)».
    Показываем alert вместо запуска визарда, чтобы он сразу понял
    почему ничего не происходит и что нужно ввести другое имя.
    """
    name = call.data.split(":", 2)[2] if call.data.count(":") >= 2 else ""
    # Склоняем имя в родительный («у Кости», не «у Костя»). genitive()
    # внутри зовёт petrovich (точен для редких имён: Илья, Никита,
    # Лука) + pymorphy3 как fallback. Работает на русских, кавказских,
    # тюркских именах. См. utils/text.py:_decline.
    if name:
        gen = genitive(name)
        await call.answer(
            f"🌙 У {gen} сегодня уже есть своя сказка — пусть звучит "
            "у него перед сном.\n\nДля другого малыша можно сочинить "
            "новую: выберите имя сверху или введите новое.",
            show_alert=True,
        )
    else:
        await call.answer(
            "🌙 У этого малыша сегодня уже есть своя сказка — пусть "
            "звучит у него перед сном.\n\nДля другого ребёнка можно "
            "сочинить новую: выберите имя сверху или введите новое.",
            show_alert=True,
        )


@router.message(StoryWizard.waiting_child_name)
async def m_child_name(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()[:32]
    if not raw or not raw.replace("-", "").replace(" ", "").isalpha():
        await message.answer(
            "🕯 Только буквы, до тридцати двух знаков. Попробуйте ещё раз."
        )
        return
    name = normalize_name(raw)  # ЛИза → Лиза, анна-мария → Анна-Мария
    await state.update_data(child_name=name)
    # Возраст спрашиваем как «версию» сказки — кнопками 3-4 / 5-6. От него
    # зависит, какой промпт получит сказочник (toddler — проще и нежнее,
    # kids — глубже и с миядзаковским сюром).
    await message.answer(
        "Выбирайте версию, которая больше нравится ребёнку.",
        reply_markup=age_kb(),
    )
    await state.set_state(StoryWizard.waiting_child_age)


@router.callback_query(F.data == "story:change_name")
async def cb_change_name(call: CallbackQuery, state: FSMContext) -> None:
    """Юзер нажал «◀ Назад» в окне выбора возраста — хочет ввести имя
    другого ребёнка вместо сохранённого. Чистим имя из state и просим
    ввести новое.
    """
    await state.update_data(child_name=None)
    await call.message.edit_text(
        "🕯 Как зовут ребёнка? Напишите имя."
    )
    await state.set_state(StoryWizard.waiting_child_name)
    await call.answer()


@router.callback_query(StoryWizard.waiting_child_age, F.data.startswith("age:"))
async def cb_child_age(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Юзер выбрал возрастную группу — сохраняем и запускаем генерацию.

    Героя и тему НЕ спрашиваем — сказочник сам решает, кого позвать и о
    чём рассказать сегодня. callback_data = «age:4» (toddler) / «age:6»
    (kids). pick_storyteller_concept() в prompts.py выбирает концепт-
    промпт по возрасту, pick_params() в story_params.py выбирает 5
    параметров сегодняшней сказки из словарей.

    hero и theme_key выставляем пустыми — downstream код умеет это
    обрабатывать: PDF-заголовок становится «Ночная сказка для Маши»,
    after_story_kb не показывает «продолжить про X».
    """
    try:
        age = int(call.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await call.answer("Неверный возраст", show_alert=True)
        return
    if age not in (4, 6):
        await call.answer("Возраст должен быть 4 или 6", show_alert=True)
        return

    # Сохраняем возраст + заглушки для hero/theme_key, чтобы _run_generation
    # и downstream код не падали на отсутствующих ключах.
    await state.update_data(child_age=age, hero="", theme_key="")
    await call.answer()
    # Сразу запускаем генерацию — больше шагов в визарде нет.
    await _run_generation(call, state, bot)


@router.callback_query(StoryWizard.waiting_hero, F.data.startswith("hero:"))
async def cb_hero(call: CallbackQuery, state: FSMContext) -> None:
    raw = call.data.split(":", 1)[1]
    if raw == "custom":
        await call.message.edit_text(
            "🪶 Напишите, кого Вы хотели бы увидеть рядом с малышом сегодня.\n"
            "<i>Например: «дельфинёнок», «робот-садовник», «бабушкин кот Барсик».</i>"
        )
        await state.set_state(StoryWizard.waiting_hero)  # ждём текст
        await state.update_data(_await_custom_hero=True)
        await call.answer()
        return
    await state.update_data(hero=raw, _await_custom_hero=False)
    await call.message.edit_text(
        "🕯 О чём пусть будет эта сказка? Выберите то, что сейчас ближе всего:",
        reply_markup=theme_kb(),
    )
    await state.set_state(StoryWizard.waiting_theme)
    await call.answer()


@router.message(StoryWizard.waiting_hero)
async def m_custom_hero(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("_await_custom_hero"):
        return
    from ..utils.text import sanitize_user_text
    # Очищаем от спецсимволов, обрезаем до 48, схлопываем пробелы
    hero = sanitize_user_text(message.text or "", max_len=48)
    if not hero or len(hero) < 2:
        await message.answer(
            "🕯 Имя героя — до сорока восьми знаков, обычными буквами. "
            "Попробуйте ещё раз."
        )
        return
    await state.update_data(hero=hero, _await_custom_hero=False)
    await message.answer(
        "🕯 О чём пусть будет эта сказка? Выберите то, что сейчас ближе всего:",
        reply_markup=theme_kb(),
    )
    await state.set_state(StoryWizard.waiting_theme)


@router.callback_query(StoryWizard.waiting_theme, F.data.startswith("theme:"))
async def cb_theme(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    theme_key = call.data.split(":", 1)[1]
    if theme_key not in THEME_CHOICES:
        await call.answer("Тема недоступна")
        return
    await state.update_data(theme_key=theme_key)
    await call.answer()
    # Без шага «выбора длины» — сразу запускаем генерацию
    await _run_generation(call, state, bot)


async def _run_generation(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Запускает генерацию сказки с фиксированными параметрами:
    length=medium (~5-6 минут чтения), полное качество (PDF + ambient + 3 картинки).

    Вызывается из cb_theme (новая сказка) и cb_story_continue (антология).
    Длину больше не спрашиваем — стандарт: одна сказка на ночь, 5+ минут.
    Параметр length оставлен в сигнатуре LLM для backward compat, но в
    промпте длина зашита жёстко (см. SYSTEM_STORYTELLER → «ЖЁСТКАЯ ДЛИНА»).
    """
    length = "medium"

    # Rate-limit перед дорогими API-вызовами (Gemini + FAL + ElevenLabs).
    # Защита от спама бесплатного триала и просто слишком частых нажатий.
    from ..services import check_story_limit
    allowed, msg = check_story_limit(call.from_user.id)
    if not allowed:
        await call.answer(msg or "Слишком быстро", show_alert=True)
        return

    # ───── Лимит 1 сказка в день НА ИМЯ РЕБЁНКА (по Москве) ─────
    # Логика: лимит «одна сказка в день» привязан к ИМЕНИ ребёнка, не к
    # юзеру в целом. У родителя с двумя детьми (Маша, Ваня) сегодня может
    # быть и сказка для Маши, и сказка для Вани — это не нарушает «один
    # ритуал в день для одного ребёнка». Сбрасывается в полночь по МСК.
    # Админы (config.admin_ids) обходят лимит — для тестирования.
    u_pre = await _get_user(call.from_user.id)
    is_admin = u_pre.telegram_id in config.admin_ids
    pre_data = await state.get_data()
    candidate_name = (pre_data.get("child_name") or "").strip()
    if candidate_name and not is_admin:
        if await _was_story_for_child_today(u_pre.id, candidate_name):
            await call.message.edit_text(
                f"🌙 Для {genitive(candidate_name)} сказка уже была сегодня.\n\n"
                "Пусть ребёнок подумает о ней перед сном и спокойно уснёт — "
                "это и есть ритуал, где важно качество, а не количество.\n\n"
                "Завтра — новая. А если хочется сказку для другого "
                "ребёнка — нажмите кнопку ниже и введите имя.",
                reply_markup=daily_limit_kb(),
            )
            # Сбрасываем имя из state, чтобы клик «Другому ребёнку»
            # (callback name:new) корректно ушёл в чистый ввод имени.
            await state.update_data(child_name=None)
            await state.set_state(StoryWizard.waiting_name_choice)
            await call.answer()
            return

    data = await state.get_data()
    await state.clear()

    await call.message.edit_text(
        "🕯 Зажигаю свечи в библиотеке. Пара минут, и я принесу Вам "
        "сегодняшнюю сказку."
    )
    await call.answer()

    u = await _get_user(call.from_user.id)

    # Определяем источник, из которого пойдёт списание. Если источника нет —
    # этот код не должен был сюда дойти (фильтры выше отсеивают), но на всякий
    # случай защищаемся paywall'ом.
    source = _allowed_source(u)
    if source is None:
        await call.message.answer(_paywall_reason_text(u), reply_markup=paywall_kb())
        return
    is_paid = source in (SOURCE_PACK, SOURCE_SUBSCRIPTION, SOURCE_SINGLE)
    is_demo_first = source == SOURCE_FREE and (u.free_stories_used or 0) == 0
    # В премиум-стеке всегда полное качество (Azure + Sonnet + Recraft).
    full_quality = True

    # ─ Выбор параметров сказки на стороне бота («казино»).
    # Раньше: модель сама выбирала архитектуру из 25 шаблонов и писала
    # маркер первой строкой, мы парсили и сохраняли. Теперь: бот сам
    # выбирает 5 параметров (форма/юмор/жанр/зачин/интонация) из словарей,
    # исключая всё, что использовалось в текущем цикле этого ребёнка.
    # Модель просто получает 5 слов и пишет одну сказку.
    from ..services.story_params import pick_params
    child_age_int = int(data.get("child_age") or 5)
    params = pick_params(
        child_age=child_age_int,
        used_architectures=u.used_architectures or [],
        used_humors=u.used_humors or [],
        used_openings=u.used_openings or [],
        used_tones=u.used_tones or [],
        last_category=u.last_story_category,
    )
    logger.info(
        "Параметры сказки для user=%s: форма=%s, юмор=%s, жанр=%s, "
        "зачин=%s, интонация=%s (cat=%s)",
        u.telegram_id, params.form, params.humor, params.genre,
        params.opening, params.tone, params.new_category,
    )

    try:
        text, story_title, scenes = await generate_story(
            child_name=data["child_name"],
            child_age=child_age_int,
            form=params.form,
            humor=params.humor,
            genre=params.genre,
            opening=params.opening,
            tone=params.tone,
            paid_quality=full_quality,
            hero=data["hero"],
            theme_key=data["theme_key"],
            length=length,
        )
    except Exception as e:
        logger.exception("Ошибка генерации сказки: %s", e)
        await call.message.answer(
            "🕯 Простите — у нашего сказочника погасли все свечи в эту минуту. "
            "Дайте ему собраться и попробуйте через пару минут заново."
        )
        return

    # Сохраняем обновлённые used-массивы для ротации на следующий раз.
    # Делаем СРАЗУ после успешной генерации, до длинного пайплайна
    # PDF/картинок — если что-то ниже упадёт, ротация всё равно сдвинется
    # (пользователю не выпадет та же комбинация подряд).
    # Legacy-колонки last_story_architecture/humor_register/group/category
    # тоже обновляем — для дашбордов и обратной совместимости.
    async with Session() as s:
        # Извлекаем последний индекс архитектуры из массива (для legacy)
        last_arch_idx = params.new_used_architectures[-1] if params.new_used_architectures else None
        last_humor_idx = params.new_used_humors[-1] if params.new_used_humors else None
        await s.execute(
            update(User)
            .where(User.id == u.id)
            .values(
                used_architectures=params.new_used_architectures,
                used_humors=params.new_used_humors,
                used_openings=params.new_used_openings,
                used_tones=params.new_used_tones,
                last_story_category=params.new_category,
                last_story_architecture=last_arch_idx,
                last_story_humor_register=last_humor_idx,
            )
        )
        await s.commit()

    # ─────────────────── Новый flow (USE_TTS=false): PDF-книжка + ambient ───────────────────
    # Стандартный путь после рефакторинга: бот НЕ читает сказку голосом —
    # делает PDF-книжку с 3 иллюстрациями и прикладывает фоновую музыку.
    # Родитель сам читает ребёнку под музыку.
    #
    # Старый flow с TTS-озвучкой включается через USE_TTS=true в .env
    # (оставлен для тестов / будущего возврата к озвучке).
    from ..prompts import THEME_CHOICES
    from ..services.image import generate_three_illustrations
    from ..services.pdf_book import build_story_pdf
    from ..utils import accusative as _acc, genitive as _gen, hero_accusative as _hero_acc

    audio_path: Path | None = None
    image_path: Path | None = None  # для совместимости с БД (Story.image_path)
    display_text = strip_emo_markers(text)

    # Заголовок книжки. Раньше формировался из имени + героя + темы:
    # «Сказка про Машу и робота Мику, о настоящей смелости». Но с новой
    # моделью (сказочник сам выбирает героя и тему) у нас этих параметров
    # нет — поэтому заголовок становится простым «Ночная сказка для Маши».
    try:
        title_phrase = THEME_CHOICES[data["theme_key"]][2] if data.get("theme_key") else ""
    except (KeyError, IndexError):
        title_phrase = ""
    # Заголовок книжки. Приоритет: название, которое сказочник придумал сам
    # (приходит из generate_story, парсится из второй строки в «ёлочках»).
    # Если сказочник название не дал — fallback на общий «Сказка для X».
    if story_title:
        book_title = story_title
    else:
        book_title = f"Сказка для {_gen(data['child_name'])}"
    book_subtitle = title_phrase

    if not config.use_tts:
        # ─── 3 иллюстрации ───
        # Сцены приходят из generate_story (сказочник сам их выдал в
        # блоке ---SCENES--- по нашей инструкции в _SCENE_BLOCK_INSTRUCTIONS).
        # Раньше тут был отдельный вызов extract_three_scenes к Gemini —
        # выключили: один API вместо двух, сказочник лучше знает мир,
        # который только что написал. Если scenes=None (сказочник забыл
        # блок или JSON битый) — generate_three_illustrations нарисует
        # без сюжетного мотива, чисто по stage-промптам.
        await bot.send_chat_action(call.message.chat.id, "upload_photo")
        illustrations = await generate_three_illustrations(
            data["hero"], data["theme_key"], scenes=scenes,
        )

        # Собираем PDF
        try:
            pdf_path = build_story_pdf(
                title=book_title,
                subtitle=book_subtitle,
                text=display_text,
                cover_image=illustrations.get("opening"),
                climax_image=illustrations.get("climax"),
                ending_image=illustrations.get("ending"),
            )
        except Exception as e:
            logger.exception("PDF build failed: %s", e)
            pdf_path = None

        # Для сохранения в БД — путь к обложке (Story.image_path остаётся в схеме)
        cover_path = illustrations.get("opening")
        if cover_path:
            image_path = cover_path

        # Сначала отправляем обложку как красивое превью
        if cover_path and cover_path.exists():
            try:
                await call.message.answer_photo(FSInputFile(str(cover_path)))
            except Exception as e:
                logger.warning("Cover send failed: %s", e)

        # Затем PDF-книжку
        if pdf_path and pdf_path.exists():
            try:
                # Имя файла для юзера в Telegram — название самой сказки
                # (то, что сказочник пишет первой строкой, парсится в
                # parse_story_title). Если по какой-то причине названия
                # нет — fallback на «{ребёнок} & {герой}». Санитизируем
                # символы, которые ломают filesystem или Telegram-клиента.
                _raw_title = (story_title or "").strip()
                if _raw_title:
                    # Убираем символы, недопустимые в именах файлов на
                    # Windows/Mac/Linux: / \ : * ? " < > | + перенос строк.
                    import re as _re_fn
                    _safe = _re_fn.sub(r'[/\\:*?"<>|\r\n\t]', "-", _raw_title)
                    # Обрезаем по 80 символов с запасом под расширение
                    _safe = _safe.strip(" .-")[:80] or "Сказка"
                    safe_name = f"{_safe}.pdf"
                else:
                    safe_name = f"{data['child_name']} & {data['hero']}.pdf".replace("/", "-")
                await call.message.answer_document(
                    FSInputFile(str(pdf_path), filename=safe_name),
                    caption=f"📖 Сказка на сегодня для {_gen(data['child_name'])}",
                )
            except Exception as e:
                logger.exception("PDF send failed: %s", e)

    else:
        # ─────────────────── Legacy flow (USE_TTS=true): озвучка как раньше ───────────────────
        scene_task = asyncio.create_task(extract_scene(text)) if full_quality else None
        audio_task = asyncio.create_task(synthesize_speech(text)) if full_quality else None

        if full_quality:
            scene = None
            if scene_task:
                try:
                    scene = await scene_task
                except Exception as e:
                    logger.warning("scene_task failed: %s", e)
            image_task = asyncio.create_task(
                generate_cover(data["hero"], data["theme_key"], scene_description=scene)
            )
            await bot.send_chat_action(call.message.chat.id, "upload_photo")
            try:
                res = await image_task
                if isinstance(res, Path):
                    image_path = res
                    await call.message.answer_photo(FSInputFile(str(image_path)))
            except Exception as e:
                logger.warning("Картинка не сгенерилась: %s", e)

        await bot.send_chat_action(call.message.chat.id, "typing")
        for part in _split_for_telegram(display_text):
            await call.message.answer(part)

        if audio_task:
            status_msg = None
            if not audio_task.done():
                try:
                    status_msg = await call.message.answer(
                        "🎙 <i>Дописываю голос рассказчика — ещё несколько секунд…</i>"
                    )
                except Exception as e:
                    logger.debug("status msg failed: %s", e)
            await bot.send_chat_action(call.message.chat.id, "upload_voice")
            try:
                res = await audio_task
                if isinstance(res, Path):
                    audio_path = res
                    if status_msg:
                        try:
                            await status_msg.delete()
                        except Exception:
                            pass
                    await call.message.answer_audio(
                        FSInputFile(str(audio_path)),
                        title=f"Сказка для {genitive(data['child_name'])}",
                        performer=config.bot_brand,
                    )
            except Exception as e:
                logger.warning("Аудио не сгенерилось: %s", e)

    # Сохраняем сказку в БД, обновляем счётчики.
    # Колонка next_episode_teaser больше не используется (модель антологии вместо
    # обещаний). Оставлена в схеме на случай если когда-то понадобится.
    async with Session() as s:
        u_db = (await s.execute(select(User).where(User.telegram_id == call.from_user.id))).scalar_one()
        # Списываем из источника, который мы определили перед генерацией.
        # _consume_story_source проставит last_story_at там где нужен daily-лимит.
        _consume_story_source(u_db, source)
        # child_name пишем только при первом разе (он редко меняется),
        # а child_age — каждый раз, потому что:
        # - возраст определяет выбор промпта (3-4 toddler / 5-6 kids);
        # - у родителя может быть несколько детей разного возраста;
        # - ребёнок может вырасти из toddler-промпта в kids-промпт.
        # И child_name, и child_age перезаписываем КАЖДЫЙ раз, потому что:
        # - у родителя может быть несколько детей разного возраста и имени;
        # - после нажатия «Назад» юзер вводит другое имя — оно должно стать
        #   новым «дефолтом» при следующем заходе в визард;
        # - возраст определяет выбор промпта (3-4 toddler / 5-6 kids).
        u_db.child_name = data["child_name"]
        u_db.child_age = int(data.get("child_age") or 5)
        story_obj = Story(
            user_id=u_db.id,
            child_name=data["child_name"],
            child_age=int(data.get("child_age") or 5),
            hero=data["hero"],
            theme=data["theme_key"],
            length=length,
            # В БД сохраняем «чистый» текст, без эмо-маркеров — он используется
            # для /library и как summary-контекст для следующих сказок. Аудио
            # уже создано с маркерами выше, повторно генерить не нужно.
            text=display_text,
            audio_path=str(audio_path) if audio_path else None,
            image_path=str(image_path) if image_path else None,
            pdf_path=str(pdf_path) if pdf_path else None,
            is_paid_quality=full_quality,
        )
        s.add(story_obj)
        await s.commit()
        await s.refresh(story_obj)
        story_id = story_obj.id

    # Если источник — FREE / BONUS, юзер на «бесплатной воронке».
    # Если FREE-лимит ещё не исчерпан или есть бонусы — отправляем тёплое прощание.
    # Если только что использован последний free — показываем paywall.
    if source in (SOURCE_FREE, SOURCE_BONUS):
        remaining_free = max(0, config.free_story_limit - (u_db.free_stories_used or 0))
        remaining_bonus = max(0, (u_db.bonus_stories or 0))
        has_more_free = remaining_free > 0 or remaining_bonus > 0

        if has_more_free:
            child_name = data["child_name"]
            await call.message.answer(
                f"🌙 Тёплых снов, {child_name}.\n\n"
                f"Завтра вечером, когда на небе зажгутся первые звёзды, "
                f"новая сказка будет ждать Вас здесь. До нашей встречи.",
                reply_markup=after_story_kb(story_id, has_sequel=True, hero=data["hero"], child_name=child_name),
            )
            from .feedback import maybe_ask_for_feedback
            await maybe_ask_for_feedback(call.message, call.from_user.id)
            return

        # Закончилась бесплатная (для модели "первая бесплатно" — после первой
        # же сказки). НЕ показываем paywall в этот же момент — это давление.
        # Юзер натолкнётся на paywall сам, когда захочет вторую сказку.
        # Сейчас — только мягкий запрос критики за бонусную сказку.
        from .feedback import maybe_ask_for_feedback
        await maybe_ask_for_feedback(call.message, call.from_user.id)
        return

    await call.message.answer(
        "🌙 Готово. Тёплых снов Вам и малышу.",
        reply_markup=after_story_kb(story_id, has_sequel=True, hero=data["hero"], child_name=data["child_name"]),
    )


@router.callback_query(F.data == "story:cancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text(
        "🕯 Хорошо. Возвращаемся в меню — буду ждать Вас здесь.",
        reply_markup=main_menu_kb(),
    )
    await call.answer()
