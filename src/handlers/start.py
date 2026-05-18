"""/start, главное меню, регистрация юзера, реферальный deep-link."""
from __future__ import annotations

import logging
import secrets

from aiogram import F, Router
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import CallbackQuery, Message
from sqlalchemy import desc, select

from ..config import config
from ..db import Referral, Session, Story, User
from ..keyboards import main_menu_kb

logger = logging.getLogger(__name__)
router = Router(name="start")


WELCOME = (
    "Здравствуйте! Это <b>{brand}</b> — бот, который пишет персональные сказки на ночь "
    "для вашего ребёнка.\n\n"
    "Имя ребёнка, любимый герой, тема — и через 10 секунд у вас готовая сказка с "
    "озвучкой нежным голосом и обложкой-картинкой.\n\n"
    "🎁 <b>Первая сказка — бесплатная демо с озвучкой и обложкой</b>, чтобы вы услышали "
    "и увидели сразу. Дальше — ещё {free_minus_one} бесплатных сказок текстом.\n\n"
    "После — подписка 490 ₽/мес (безлимит + озвучка + картинка) или одноразовая сказка "
    "в подарок близким за 199 ₽."
)


async def _ensure_user(message_user, ref_code: str | None = None) -> User:
    async with Session() as s:
        u = (await s.execute(select(User).where(User.telegram_id == message_user.id))).scalar_one_or_none()
        if u:
            return u
        inviter = None
        if ref_code:
            inviter = (await s.execute(select(User).where(User.referral_code == ref_code))).scalar_one_or_none()
        u = User(
            telegram_id=message_user.id,
            username=message_user.username,
            first_name=message_user.first_name,
            language_code=message_user.language_code,
            referral_code=secrets.token_urlsafe(8),
            referred_by_id=inviter.id if inviter else None,
        )
        s.add(u)
        await s.flush()
        if inviter:
            s.add(Referral(inviter_id=inviter.id, invited_id=u.id))
        await s.commit()
        await s.refresh(u)
        return u


async def _last_story_hero(user_id: int) -> tuple[str | None, str | None]:
    """Если у юзера есть хотя бы одна сказка, возвращаем (hero, child_name)
    последней — это включает кнопку «🔮 Новое приключение про {child} и {hero}»
    в главном меню. Модель антологии: одни герои, новый эпизод."""
    async with Session() as s:
        last = (await s.execute(
            select(Story)
            .where(Story.user_id == user_id)
            .order_by(desc(Story.created_at))
            .limit(1)
        )).scalar_one_or_none()
        if last:
            return last.hero, last.child_name
        return None, None


@router.message(CommandStart(deep_link=True))
async def cmd_start_deep(message: Message, command: CommandObject) -> None:
    ref_code = None
    if command.args and command.args.startswith("ref_"):
        ref_code = command.args[4:]
    u = await _ensure_user(message.from_user, ref_code=ref_code)
    hero, child = await _last_story_hero(u.id)
    await message.answer(
        WELCOME.format(brand=config.bot_brand, free_minus_one=max(0, config.free_story_limit - 1)),
        reply_markup=main_menu_kb(continuation_hero=hero, continuation_child=child),
    )


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    u = await _ensure_user(message.from_user)
    hero, child = await _last_story_hero(u.id)
    await message.answer(
        WELCOME.format(brand=config.bot_brand, free_minus_one=max(0, config.free_story_limit - 1)),
        reply_markup=main_menu_kb(continuation_hero=hero, continuation_child=child),
    )


@router.callback_query(F.data == "menu:main")
async def cb_main_menu(call: CallbackQuery) -> None:
    async with Session() as s:
        u = (await s.execute(select(User).where(User.telegram_id == call.from_user.id))).scalar_one_or_none()
    if u:
        hero, child = await _last_story_hero(u.id)
    else:
        hero, child = None, None
    await call.message.edit_text(
        WELCOME.format(brand=config.bot_brand, free_minus_one=max(0, config.free_story_limit - 1)),
        reply_markup=main_menu_kb(continuation_hero=hero, continuation_child=child),
    )
    await call.answer()
