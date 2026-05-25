"""/start, главное меню, регистрация юзера, реферальный deep-link.

Атрибуция источника:
- `?start=ref_XXXX` — друг приглашает друга (внутренняя реферальная ссылка)
- `?start=<partner_code>` — партнёр (блогер/канал) — записывает user.partner_id,
  и при каждой оплате будет автоматически начисляться комиссия (см. partners.py)
- `?start=<любая_другая_строка>` — просто UTM-метка (записывается в utm_source).
  Используется для отслеживания эффективности конкретных постов (?start=mama_baby и т.п.).

ВАЖНО: партнёрский код проверяется ПЕРВЫМ. Если такой партнёр есть в БД,
а юзер ещё не зарегистрирован — мы привязываем его к этому партнёру навсегда."""
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
from ..services import find_partner_by_code

logger = logging.getLogger(__name__)
router = Router(name="start")


WELCOME = (
    "🕯 Здравствуйте. Вы попали в волшебный мир сказок. Здесь Вы "
    "можете заказать <b>персональную авторскую сказку</b> для своего "
    "ребёнка ко сну.\n\n"
    "Наши сказки отличаются тем, что они не только интересны, но ещё "
    "и поучительные и имеют глубокий смысл.\n\n"
    "Одна сказка перед сном — таков наш ритуал.\n\n"
    "<i>Продолжая, Вы соглашаетесь с нашими условиями — /legal</i>"
)


async def _ensure_user(
    message_user,
    *,
    friend_ref_code: str | None = None,
    partner_id: int | None = None,
    utm_source: str | None = None,
) -> User:
    """Создаёт юзера если его нет. Атрибуция применяется ТОЛЬКО при создании
    (чтобы повторное нажатие /start не переписывало источник)."""
    async with Session() as s:
        u = (await s.execute(select(User).where(User.telegram_id == message_user.id))).scalar_one_or_none()
        if u:
            return u
        inviter = None
        if friend_ref_code:
            inviter = (
                await s.execute(select(User).where(User.referral_code == friend_ref_code))
            ).scalar_one_or_none()
        u = User(
            telegram_id=message_user.id,
            username=message_user.username,
            first_name=message_user.first_name,
            language_code=message_user.language_code,
            referral_code=secrets.token_urlsafe(8),
            referred_by_id=inviter.id if inviter else None,
            partner_id=partner_id,
            utm_source=utm_source,
        )
        s.add(u)
        await s.flush()
        if inviter:
            s.add(Referral(inviter_id=inviter.id, invited_id=u.id))
        await s.commit()
        await s.refresh(u)
        if partner_id:
            logger.info("User %s attributed to partner_id=%s (utm=%s)",
                        message_user.id, partner_id, utm_source)
        elif utm_source:
            logger.info("User %s attributed to utm_source=%s", message_user.id, utm_source)
        return u


async def _resolve_start_payload(args: str | None) -> tuple[str | None, int | None, str | None]:
    """Разбирает payload из /start.

    Возвращает (friend_ref_code, partner_id, utm_source).

    Логика:
    - `ref_XXX` → friend_ref_code='XXX', utm_source='friend_ref'
    - известный partner.code → partner_id=ID, utm_source=код
    - любое другое → utm_source=исходная строка
    """
    if not args:
        return None, None, None
    payload = args.strip()
    if payload.startswith("ref_"):
        return payload[4:], None, "friend_ref"
    # Партнёрский код?
    partner = await find_partner_by_code(payload)
    if partner:
        return None, partner.id, partner.code
    # Просто UTM-метка
    # Обрезаем длину чтоб ничего не сломать в БД (VARCHAR(64))
    return None, None, payload[:64]


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
    friend_ref, partner_id, utm = await _resolve_start_payload(command.args)
    u = await _ensure_user(
        message.from_user,
        friend_ref_code=friend_ref,
        partner_id=partner_id,
        utm_source=utm,
    )
    hero, child = await _last_story_hero(u.id)
    await message.answer(
        WELCOME.format(brand=config.bot_brand),
        reply_markup=main_menu_kb(continuation_hero=hero, continuation_child=child),
    )


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    u = await _ensure_user(message.from_user)
    hero, child = await _last_story_hero(u.id)
    await message.answer(
        WELCOME.format(brand=config.bot_brand),
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
        WELCOME.format(brand=config.bot_brand),
        reply_markup=main_menu_kb(continuation_hero=hero, continuation_child=child),
    )
    await call.answer()
