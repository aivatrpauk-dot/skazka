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
from sqlalchemy import select

from ..config import config
from ..db import Session, Story, SubscriptionStatus, User
from ..keyboards import after_story_kb, hero_kb, main_menu_kb, paywall_kb, theme_kb
from ..prompts import HERO_QUICK_PICKS, THEME_CHOICES
from ..services import (
    extract_scene,
    generate_cover,
    generate_story,
    summarize_story,
    synthesize_speech,
)
from ..states import StoryWizard
from ..utils import (
    accusative,
    dative,
    genitive,
    hero_accusative,
    hero_instrumental,
    instrumental,
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


# Лимит «одна сказка в сутки». Применяется к bonus/pack/subscription, но НЕ
# к разовой покупке (юзер платит каждый раз) и НЕ к free trial первой сказки.
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


def _allowed_source(u: User) -> str | None:
    """Возвращает источник, по которому юзер может сейчас сделать сказку.
    None — значит нельзя (нужен paywall).

    Логика:
      1. Бонусные сказки (от рефералки/feedback) — без daily-лимита.
      2. Free trial первая сказка — без daily-лимита.
      3. Single-разовая покупка — без daily-лимита (платил же).
      4. Pack — с daily-лимитом 1/сутки.
      5. Subscription active — с daily-лимитом 1/сутки.
    """
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
    """
    now = dt.datetime.now(dt.timezone.utc)
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
            "🌙 Сегодняшняя сказка уже создана.\n"
            f"Следующая будет доступна {next_at.strftime('%d.%m в %H:%M')}.\n\n"
            "Пока — давайте просто отдохнём перед сном."
        )
    return "Бесплатные сказки закончились. Выберите, как продолжить:"


async def _get_user(telegram_id: int) -> User:
    async with Session() as s:
        return (await s.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one()


@router.callback_query(F.data == "story:continue")
async def cb_story_continue(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Юзер жмёт «Что было дальше с {hero}?» — пропускаем мастер и сразу
    генерируем продолжение с теми же параметрами (имя ребёнка, hero, theme)
    из последней сказки. Длину НЕ спрашиваем — сразу генерируем."""
    u = await _get_user(call.from_user.id)
    if not _can_make_story(u):
        await call.message.edit_text(
            _paywall_reason_text(u),
            reply_markup=paywall_kb(),
        )
        await call.answer()
        return

    async with Session() as s:
        # Берём последнюю сказку этого юзера (любую — антология не требует тизера)
        last = (await s.execute(
            select(Story)
            .where(Story.user_id == u.id)
            .order_by(Story.created_at.desc())
            .limit(1)
        )).scalar_one_or_none()

    if not last:
        await call.answer("Нет прошлой сказки", show_alert=True)
        return

    # Запоминаем параметры в state и метку «это антология-продолжение»
    await state.update_data(
        child_name=last.child_name,
        child_age=last.child_age,
        hero=last.hero,
        theme_key=last.theme,
        continue_series=True,
    )
    await call.message.edit_text(
        f"Новая история про <b>{accusative(last.child_name)}</b> и <b>{hero_accusative(last.hero)}</b>."
    )
    await call.answer()
    # Без шага «выбора длины» — сразу запускаем генерацию
    await _run_generation(call, state, bot)


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

    if u.child_name:
        # уже знаем ребёнка — предложим использовать его данные
        await state.update_data(child_name=u.child_name, child_age=u.child_age or 5)
        name_gen = genitive(u.child_name)        # для Лизы
        name_ins = instrumental(u.child_name)    # рядом с Лизой
        await call.message.edit_text(
            f"Делаем сказку для <b>{name_gen}</b> ({u.child_age} лет).\n"
            f"Если для другого ребёнка — нажмите «Назад» и введите имя.\n\n"
            f"Кто будет главным героем рядом с {name_ins}?",
            reply_markup=hero_kb(),
        )
        await state.set_state(StoryWizard.waiting_hero)
        await call.answer()
        return

    await call.message.edit_text(
        "Как зовут ребёнка? Напишите имя.\n<i>Например: Маша, Тимофей, Лиза.</i>",
    )
    await state.set_state(StoryWizard.waiting_child_name)
    await call.answer()


@router.message(StoryWizard.waiting_child_name)
async def m_child_name(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()[:32]
    if not raw or not raw.replace("-", "").replace(" ", "").isalpha():
        await message.answer("Имя только буквами, до 32 символов. Попробуйте ещё раз.")
        return
    name = normalize_name(raw)  # ЛИза → Лиза, анна-мария → Анна-Мария
    # Возраст больше не спрашиваем — наши сказки только для 3-6 лет.
    # Фиксируем дефолт 5 (середина диапазона) — используется в БД и логах,
    # на сам промпт не влияет.
    await state.update_data(child_name=name, child_age=5)
    await message.answer(
        f"Кто будет главным героем рядом с {instrumental(name)}?",
        reply_markup=hero_kb(),
    )
    await state.set_state(StoryWizard.waiting_hero)


@router.callback_query(StoryWizard.waiting_hero, F.data.startswith("hero:"))
async def cb_hero(call: CallbackQuery, state: FSMContext) -> None:
    raw = call.data.split(":", 1)[1]
    if raw == "custom":
        await call.message.edit_text("Напишите, кто будет героем. Например: «дельфинёнок», «робот-садовник».")
        await state.set_state(StoryWizard.waiting_hero)  # ждём текст
        await state.update_data(_await_custom_hero=True)
        await call.answer()
        return
    await state.update_data(hero=raw, _await_custom_hero=False)
    await call.message.edit_text("Какая тема сказки?", reply_markup=theme_kb())
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
        await message.answer("Напишите имя или название героя, до 48 символов "
                             "(только буквы, цифры, пробелы, дефисы).")
        return
    await state.update_data(hero=hero, _await_custom_hero=False)
    await message.answer("Какая тема сказки?", reply_markup=theme_kb())
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
    length=medium (~4-5 минут чтения), полное качество (озвучка + картинка).

    Вызывается из cb_theme (новая сказка) и cb_story_continue (антология).
    Длину больше не спрашиваем — у нас один формат «полноценная сказка на ночь».
    """
    length = "medium"

    # Rate-limit перед дорогими API-вызовами (Gemini + FAL + ElevenLabs).
    # Защита от спама бесплатного триала и просто слишком частых нажатий.
    from ..services import check_story_limit
    allowed, msg = check_story_limit(call.from_user.id)
    if not allowed:
        await call.answer(msg or "Слишком быстро", show_alert=True)
        return

    # ───── Лимит 1 сказка в КАЛЕНДАРНЫЙ ДЕНЬ (по Москве, для ВСЕХ) ─────
    # Сбрасывается в полночь по МСК — не зависит от времени предыдущего заказа.
    # Так мама может заказать днём (тихий час) или вечером — гибко.
    u_pre = await _get_user(call.from_user.id)
    if u_pre.last_story_at:
        import datetime as _dt
        MSK_TZ = _dt.timezone(_dt.timedelta(hours=3))
        now_msk_date = _dt.datetime.now(_dt.timezone.utc).astimezone(MSK_TZ).date()
        last_msk_date = u_pre.last_story_at.astimezone(MSK_TZ).date()
        if last_msk_date == now_msk_date:
            # Уже была сегодня (по Москве)
            await call.message.edit_text(
                "🌙 На сегодня сказка уже была.\n\n"
                "Пусть ребёнок подумает о ней перед сном и "
                "спокойно уснёт — это и есть ритуал, где важно "
                "качество, а не количество.\n\n"
                "Завтра — новая. Заглядывайте, когда нужно "
                "уложить.",
                reply_markup=main_menu_kb(),
            )
            await call.answer()
            return

    data = await state.get_data()
    await state.clear()

    await call.message.edit_text("Готовлю сказку… это займёт около 10–15 секунд.")
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

    # ─ Контекст антологии: если это продолжение — даём LLM саммари прошлой сказки ─
    # БЕЗ обещаний и тизеров — просто «те же герои, новый эпизод».
    previous_summary: str | None = None
    if data.get("continue_series"):
        async with Session() as s:
            last = (await s.execute(
                select(Story)
                .where(Story.user_id == u.id)
                .order_by(Story.created_at.desc())
                .limit(1)
            )).scalar_one_or_none()
        if last:
            previous_summary = await summarize_story(last.text)
            logger.info("Антология-продолжение для user=%s, last_story=%s", u.telegram_id, last.id)

    try:
        text = await generate_story(
            child_name=data["child_name"],
            child_age=int(data.get("child_age") or 5),
            hero=data["hero"],
            theme_key=data["theme_key"],
            length=length,
            paid_quality=full_quality,
            previous_summary=previous_summary,
        )
    except Exception as e:
        logger.exception("Ошибка генерации сказки: %s", e)
        await call.message.answer("Что-то пошло не так у сказочника. Попробуйте ещё раз через минуту.")
        return

    # Параллельно — сцена для картинки (если генерим картинку)
    scene_task = asyncio.create_task(extract_scene(text)) if full_quality else None

    # Озвучка стартует сразу без ожидания сцены — нам нужен только текст сказки
    audio_task = asyncio.create_task(synthesize_speech(text)) if full_quality else None

    audio_path: Path | None = None
    image_path: Path | None = None
    image_task = None

    # ─ Порядок отправки: КАРТИНКА → ТЕКСТ → АУДИО ─
    # 1) Сначала ждём сцену, потом запускаем картинку
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

    # 2) Текст — основное содержимое. Юзеру отдаём БЕЗ эмо-маркеров
    # ([laughs softly] и т.п. — они только для озвучки ElevenLabs).
    # Разбиваем на части, если длиннее 4000 символов.
    await bot.send_chat_action(call.message.chat.id, "typing")
    display_text = strip_emo_markers(text)
    for part in _split_for_telegram(display_text):
        await call.message.answer(part)

    # 3) Аудио — финальный аккорд, можно слушать ребёнку перед сном.
    # Аудио идёт ПОСЛЕ текста, потому что синтез + микширование могут
    # занять ещё 5-10 сек после получения текста. Чтобы пауза не казалась
    # «непонятным зависанием», явно сообщаем юзеру что голос дописывается.
    if audio_task:
        # Если аудио ещё не готово к моменту отправки текста — показываем
        # явное «голос идёт следом». Это убирает ощущение «бот завис».
        status_msg = None
        if not audio_task.done():
            try:
                status_msg = await call.message.answer(
                    "🎙 <i>Записываю голос — ещё несколько секунд…</i>"
                )
            except Exception as e:
                logger.debug("status msg failed: %s", e)
        await bot.send_chat_action(call.message.chat.id, "upload_voice")
        try:
            res = await audio_task
            if isinstance(res, Path):
                audio_path = res
                # Удаляем статус ПЕРЕД отправкой аудио — чтоб порядок был чистый
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
        if not u_db.child_name:
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
                f"🌙 Хорошего сна, {child_name}.\n\n"
                f"Завтра вечером — новая сказка. До встречи перед сном.",
                reply_markup=after_story_kb(story_id, has_sequel=True, hero=data["hero"], child_name=child_name),
            )
            from .feedback import maybe_ask_for_feedback
            await maybe_ask_for_feedback(call.message, call.from_user.id)
            return

        # Закончилась бесплатная (для модели "первая бесплатно" — после первой же
        # сказки). Показываем preview этой первой сказки и новый paywall.
        async with Session() as s_demo:
            demo_story = (await s_demo.execute(
                select(Story)
                .where(Story.user_id == u_db.id, Story.audio_path.isnot(None))
                .order_by(Story.created_at.asc())
                .limit(1)
            )).scalar_one_or_none()

        if demo_story and demo_story.audio_path:
            try:
                await call.message.answer(
                    f"🎧 Помните, как звучала самая первая сказка для {genitive(demo_story.child_name)}? Вот она:"
                )
                await call.message.answer_audio(
                    FSInputFile(demo_story.audio_path),
                    title=f"Сказка для {genitive(demo_story.child_name)}",
                    performer=config.bot_brand,
                )
            except Exception as e:
                logger.warning("Не удалось отправить preview-аудио: %s", e)

        await call.message.answer(
            "🌙 Получилось?\n\n"
            "Если ребёнок засыпал лучше — давайте сделаем ритуал постоянным. "
            "Три варианта, выбирайте удобный:\n\n"
            "• <b>Одна сказка — 99 ₽.</b> Без подписок и обязательств.\n"
            "• <b>Пакет 15 сказок — 999 ₽</b> <i>(−34%)</i>. По одной в день, "
            "хватит на две недели.\n"
            "• <b>Подписка на месяц — 1485 ₽</b> <i>(−50%)</i>. Сказка каждый "
            "вечер на месяц, отмена через /cancel_subscription в один клик.\n\n"
            "В любом тарифе — озвучка тёплым женским голосом и обложка "
            "как у настоящих книжек.",
            reply_markup=paywall_kb(),
        )
        return

    await call.message.answer(
        "Готово. Сладких снов!",
        reply_markup=after_story_kb(story_id, has_sequel=True, hero=data["hero"], child_name=data["child_name"]),
    )


@router.callback_query(F.data == "story:cancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("Хорошо, возвращаемся в меню.", reply_markup=main_menu_kb())
    await call.answer()
