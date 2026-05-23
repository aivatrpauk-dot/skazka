"""Сбор обратной связи (критики) от юзеров после первой сказки.

Логика:
1. После доставки 1-й демо-сказки → бот предлагает «Дать критику»
2. Юзер пишет — сохраняем в БД, форвардим админам, +1 бонусная сказка
3. Если пропустил — повторно спросим через 3 сказки (один раз)
4. Если написал — больше не просим никогда

Принципиально просим именно КРИТИКУ, не «отзыв» — нужны слабые места
продукта, а не комплименты.
"""
from __future__ import annotations

import datetime as dt
import logging

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
)
from sqlalchemy import select

from ..config import config
from ..db import Feedback, Session, User
from ..states import FeedbackFlow

logger = logging.getLogger(__name__)
router = Router(name="feedback")


# ───────── Логика «когда спрашивать» ─────────

def should_show_feedback_prompt(u: User) -> bool:
    """Решает показывать ли приглашение на критику.

    Правила:
    - Если юзер уже дал критику (feedback_given=True) — никогда не спрашиваем
    - Первая ever сказка (free_stories_used == 1) → спрашиваем
    - Если пропустил один раз (feedback_skipped_count == 1) и прошло 3+
      сказок (free_stories_used >= 4) → спрашиваем ещё один раз
    - Дальше не просим
    """
    if u.feedback_given:
        return False
    if u.feedback_skipped_count == 0 and u.free_stories_used == 1:
        return True
    if u.feedback_skipped_count == 1 and u.free_stories_used >= 4:
        return True
    return False


# ───────── Показ приглашения ─────────

def _feedback_prompt_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Дать критику", callback_data="feedback:start")],
        [InlineKeyboardButton(text="Пропустить", callback_data="feedback:skip")],
    ])


FEEDBACK_INVITE_TEXT = (
    "💬 <b>Помогите сделать сказку лучше</b>\n\n"
    "Меня интересует НЕ «всё круто» — а что <b>разочаровало</b>.\n\n"
    "Что показалось слабым? Где провисал сюжет? Какой момент был "
    "нелепым? Имя ребёнка прозвучало нормально или как-то не так? "
    "Что бы Вы переделали?\n\n"
    "Любая честная критика — золото. Благодаря ей продукт растёт.\n"
    "За это подарю ещё одну полноценную сказку с озвучкой и картинкой 🎁"
)


async def maybe_ask_for_feedback(message: Message, user_telegram_id: int) -> None:
    """Вызывается ПОСЛЕ доставки сказки. Если пора — показывает приглашение
    и помечает что спросили."""
    async with Session() as s:
        u = (await s.execute(
            select(User).where(User.telegram_id == user_telegram_id)
        )).scalar_one_or_none()
        if not u:
            return
        if not should_show_feedback_prompt(u):
            return
        # Помечаем что спросили — чтобы потом понять что юзер хоть раз видел приглашение
        u.feedback_asked_at = dt.datetime.now(dt.timezone.utc)
        await s.commit()

    try:
        await message.answer(FEEDBACK_INVITE_TEXT, reply_markup=_feedback_prompt_kb())
    except Exception as e:
        logger.warning("Не удалось показать приглашение на критику: %s", e)


# ───────── Кнопка «Дать критику» ─────────

@router.callback_query(F.data == "feedback:start")
async def cb_feedback_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(FeedbackFlow.waiting_text)
    await call.message.edit_text(
        "Слушаю Вас. Не стесняйтесь быть прямым — мне нужны слабые "
        "места, не комплименты. Напишите 2-3 предложения честно как есть."
    )
    await call.answer()


# ───────── Кнопка «Пропустить» ─────────

@router.callback_query(F.data == "feedback:skip")
async def cb_feedback_skip(call: CallbackQuery, state: FSMContext) -> None:
    async with Session() as s:
        u = (await s.execute(
            select(User).where(User.telegram_id == call.from_user.id)
        )).scalar_one_or_none()
        if u:
            u.feedback_skipped_count += 1
            await s.commit()
            logger.info(
                "Юзер %s пропустил критику (всего пропусков: %d)",
                u.telegram_id, u.feedback_skipped_count,
            )
    try:
        await call.message.edit_text(
            "Хорошо, без критики так без критики. Если позже захотите "
            "поделиться — напишите в /support."
        )
    except Exception:
        pass
    await state.clear()
    await call.answer()


# ───────── Приём текста критики ─────────

@router.message(StateFilter(FeedbackFlow.waiting_text))
async def feedback_text_received(message: Message, state: FSMContext) -> None:
    raw_text = (message.text or "").strip()
    if len(raw_text) < 5:
        await message.answer(
            "Слишком коротко. Напишите хотя бы пару предложений — что "
            "именно не зашло, и я смогу это улучшить."
        )
        return
    if len(raw_text) > 2000:
        raw_text = raw_text[:2000] + "…"

    # Сохраняем в БД, начисляем бонусную сказку, форвардим админу
    async with Session() as s:
        u = (await s.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )).scalar_one_or_none()
        if not u:
            await state.clear()
            return

        # Последняя сказка юзера — привязываем критику к ней (необязательно)
        from ..db import Story
        last_story = (await s.execute(
            select(Story)
            .where(Story.user_id == u.id)
            .order_by(Story.created_at.desc())
            .limit(1)
        )).scalar_one_or_none()

        fb = Feedback(
            user_id=u.id,
            story_id=last_story.id if last_story else None,
            text=raw_text,
            bonus_granted=True,
        )
        s.add(fb)

        # Бонусная сказка + помечаем что критика получена
        u.feedback_given = True
        u.bonus_stories = (u.bonus_stories or 0) + 1
        await s.commit()
        await s.refresh(fb)

        logger.info("Получена критика #%s от юзера %s (%d симв.)",
                    fb.id, u.telegram_id, len(raw_text))

    await message.answer(
        "Спасибо, это бесценно — именно так продукт становится лучше.\n\n"
        "🎁 <b>Бонусная сказка</b> с озвучкой и картинкой зачислена. "
        "Сегодняшняя сказка уже была — а бонусную можно будет заказать "
        "<b>завтра вечером</b>. Одна сказка в день — таков формат."
    )
    await state.clear()

    # Форвардим админам в личку
    last_story_info = ""
    if last_story:
        last_story_info = (
            f"\n<b>Последняя сказка:</b> {last_story.child_name} "
            f"({last_story.child_age}л) • {last_story.hero} • {last_story.theme}"
        )

    username_part = f"@{message.from_user.username}" if message.from_user.username else "(без юзернейма)"
    admin_text = (
        f"📬 <b>Новая критика</b> от {username_part} "
        f"(tg_id: <code>{message.from_user.id}</code>)"
        f"{last_story_info}\n\n"
        f"<b>Текст:</b>\n{raw_text}"
    )
    for admin_id in config.admin_ids:
        try:
            await message.bot.send_message(admin_id, admin_text)
        except Exception as e:
            logger.warning("Не удалось отправить критику админу %s: %s", admin_id, e)
