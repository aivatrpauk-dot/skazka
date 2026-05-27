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
        "💌 <b>Пригласите близких к нашим сказкам</b>\n\n"
        f"Когда подруга или родственница, пришедшие по Вашей ссылке, "
        f"закажут свою первую сказку (даже самую скромную, за 149 ₽) — "
        f"мы благодарим Вас одной бесплатной сказкой на Вашем счёте.\n\n"
        f"🕯 <i>Бонусная сказка ничем не отличается от обычной — "
        f"без срока годности. Действует общее правило: одна сказка "
        f"в день.</i>\n\n"
        f"Ваша личная ссылка-приглашение:\n<code>{link}</code>\n\n"
        f"Уже пришли по Вашей ссылке: <b>{activated}</b>  ·  "
        f"Заказали сказку: <b>{paid}</b>"
    )
    await call.message.edit_text(text, reply_markup=main_menu_kb(), disable_web_page_preview=True)
    await call.answer()
