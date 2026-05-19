"""Главный поток — создание сказки. FSM: имя → возраст → герой → тема → длина → генерация."""
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
from ..keyboards import after_story_kb, age_kb, hero_kb, length_kb, main_menu_kb, paywall_kb, theme_kb
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


def _is_paid(u: User) -> bool:
    if u.subscription_status != SubscriptionStatus.active:
        return False
    if not u.subscription_until:
        return False
    return u.subscription_until > dt.datetime.now(dt.timezone.utc)


async def _get_user(telegram_id: int) -> User:
    async with Session() as s:
        return (await s.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one()


@router.callback_query(F.data == "story:continue")
async def cb_story_continue(call: CallbackQuery, state: FSMContext) -> None:
    """Юзер жмёт «Что было дальше с {hero}?» — пропускаем мастер и сразу
    генерируем продолжение с теми же параметрами (имя ребёнка, hero, theme)
    из последней сказки. Длину спрашиваем."""
    u = await _get_user(call.from_user.id)
    if not _is_paid(u) and (u.free_stories_used + max(0, -u.bonus_stories) >= config.free_story_limit + u.bonus_stories):
        await call.message.edit_text(
            "Бесплатные сказки закончились. Выберите, как продолжить:",
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
        f"Новая история про <b>{accusative(last.child_name)}</b> и <b>{hero_accusative(last.hero)}</b>.\n"
        f"Какой длины делаем сегодня?",
        reply_markup=length_kb(),
    )
    await state.set_state(StoryWizard.waiting_length)
    await call.answer()


@router.callback_query(F.data == "story:new")
async def cb_story_new(call: CallbackQuery, state: FSMContext) -> None:
    u = await _get_user(call.from_user.id)
    if not _is_paid(u) and (u.free_stories_used + max(0, -u.bonus_stories) >= config.free_story_limit + u.bonus_stories):
        await call.message.edit_text(
            "Бесплатные сказки закончились. Выберите, как продолжить:",
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
    await state.update_data(child_name=name)
    await message.answer(f"Сколько лет {dative(name)}?", reply_markup=age_kb())
    await state.set_state(StoryWizard.waiting_child_age)


@router.callback_query(StoryWizard.waiting_child_age, F.data.startswith("age:"))
async def cb_age(call: CallbackQuery, state: FSMContext) -> None:
    age = int(call.data.split(":", 1)[1])
    await state.update_data(child_age=age)
    data = await state.get_data()
    await call.message.edit_text(
        f"Кто будет главным героем рядом с {instrumental(data['child_name'])}?",
        reply_markup=hero_kb(),
    )
    await state.set_state(StoryWizard.waiting_hero)
    await call.answer()


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
async def cb_theme(call: CallbackQuery, state: FSMContext) -> None:
    theme_key = call.data.split(":", 1)[1]
    if theme_key not in THEME_CHOICES:
        await call.answer("Тема недоступна")
        return
    await state.update_data(theme_key=theme_key)
    await call.message.edit_text("Какой длины делаем сказку?", reply_markup=length_kb())
    await state.set_state(StoryWizard.waiting_length)
    await call.answer()


@router.callback_query(StoryWizard.waiting_length, F.data.startswith("length:"))
async def cb_length(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    length = call.data.split(":", 1)[1]
    if length not in ("short", "medium"):
        return

    # Rate-limit перед дорогими API-вызовами (Gemini + FAL + ElevenLabs).
    # Защита от спама бесплатного триала и просто слишком частых нажатий.
    from ..services import check_story_limit
    allowed, msg = check_story_limit(call.from_user.id)
    if not allowed:
        await call.answer(msg or "Слишком быстро", show_alert=True)
        return

    data = await state.get_data()
    await state.clear()

    await call.message.edit_text("Готовлю сказку… это займёт около 10–15 секунд.")
    await call.answer()

    u = await _get_user(call.from_user.id)
    is_paid = _is_paid(u)

    # Первая сказка у нового юзера приходит с полным wow-эффектом
    is_demo_first = (not is_paid) and (u.free_stories_used == 0)
    full_quality = is_paid or is_demo_first

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

    # 2) Текст — основное содержимое. Разбиваем на части, если длиннее 4000 символов.
    await bot.send_chat_action(call.message.chat.id, "typing")
    for part in _split_for_telegram(text):
        await call.message.answer(part)

    # 3) Аудио — финальный аккорд, можно слушать ребёнку перед сном
    if audio_task:
        await bot.send_chat_action(call.message.chat.id, "upload_voice")
        try:
            res = await audio_task
            if isinstance(res, Path):
                audio_path = res
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
        if not _is_paid(u_db):
            if u_db.bonus_stories > 0:
                u_db.bonus_stories -= 1
            else:
                u_db.free_stories_used += 1
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
            text=text,
            audio_path=str(audio_path) if audio_path else None,
            image_path=str(image_path) if image_path else None,
            is_paid_quality=full_quality,
        )
        s.add(story_obj)
        await s.commit()
        await s.refresh(story_obj)
        story_id = story_obj.id

    # Если на бесплатном — управляем воронкой по этапу
    if not is_paid:
        remaining = max(0, (config.free_story_limit + u_db.bonus_stories) - u_db.free_stories_used)

        # 1-я сказка ever — была демо с полным набором
        if is_demo_first:
            await call.message.answer(
                "🌙 Это была демо-сказка — с озвучкой и обложкой, как у подписчиков.\n\n"
                f"Осталось бесплатных сказок: <b>{remaining}</b> (только текст).\n"
                "Чтобы каждая сказка была с озвучкой и картинкой — оформите подписку 490 ₽/мес.",
                reply_markup=after_story_kb(story_id, has_sequel=True, hero=data["hero"], child_name=data["child_name"]),
            )
            return

        # Промежуточные бесплатные — просто счётчик
        if remaining > 0:
            await call.message.answer(
                f"Осталось бесплатных сказок: <b>{remaining}</b>.\n"
                "Помните первую сказку — с озвучкой и обложкой? Это есть в подписке 490 ₽/мес.",
                reply_markup=after_story_kb(story_id, has_sequel=True, hero=data["hero"], child_name=data["child_name"]),
            )
            return

        # Закончились бесплатные — paywall с preview первой демо-сказки.
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
            "Это была последняя бесплатная сказка.\n\n"
            "Подписка 490 ₽/мес даёт безлимит сказок с озвучкой нежным голосом и обложкой к каждой — "
            "как в самой первой сказке, которую вы получили.\n\n"
            "Отменить можно в любой момент командой /cancel_subscription.",
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
