"""Реферальная программа: /share-ссылка + бонусы за активацию."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import select, func

from ..config import config
from ..db import Referral, Session, User
from ..keyboards import main_menu_kb

router = Router(name="referral")


@router.callback_query(F.data == "ref:share")
async def cb_share(call: CallbackQuery) -> None:
    async with Session() as s:
        u = (await s.execute(select(User).where(User.telegram_id == call.from_user.id))).scalar_one()
        # сколько активированных рефералов
        activated = (await s.execute(
            select(func.count(Referral.id)).where(Referral.inviter_id == u.id)
        )).scalar_one()
        paid = (await s.execute(
            select(func.count(Referral.id)).where(
                Referral.inviter_id == u.id, Referral.bonus_granted == True  # noqa: E712
            )
        )).scalar_one()

    me = (await call.bot.get_me()).username
    link = f"https://t.me/{me}?start=ref_{u.referral_code}"

    text = (
        "<b>Приглашайте друзей — получайте сказки</b>\n\n"
        f"За каждого, кто откроет бота по Вашей ссылке и пройдёт первую сказку, "
        f"Вам +<b>{config.referral_bonus}</b> бесплатных сказки.\n"
        f"Если друг оформит подписку — Вам +<b>5</b> бонусных сказок дополнительно.\n\n"
        f"🌙 <i>Бонусные сказки приходят в общую очередь — по одной в день. "
        f"Одна сказка в день — это и есть наш формат, так и для бонусных.</i>\n\n"
        f"Ваша ссылка:\n<code>{link}</code>\n\n"
        f"Приглашено: <b>{activated}</b>  ·  Подписалось: <b>{paid}</b>"
    )
    await call.message.edit_text(text, reply_markup=main_menu_kb(), disable_web_page_preview=True)
    await call.answer()
