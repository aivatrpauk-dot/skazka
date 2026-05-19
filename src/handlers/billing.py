"""Платежи. Telegram Payments (provider=ЮKassa) + рекуррент через API ЮKassa."""
from __future__ import annotations

import datetime as dt
import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery
from sqlalchemy import select

from ..config import config
from ..db import PaymentKind, Referral, Session, SubscriptionStatus, User
from ..keyboards import main_menu_kb, paywall_kb
from ..services import (
    attribute_payment_to_partner,
    compute_subscription_price,
    create_gift_invoice,
    create_subscription_invoice,
    process_successful_payment,
)

logger = logging.getLogger(__name__)
router = Router(name="billing")


PLANS_TEXT = (
    "<b>Тарифы</b>\n\n"
    "🌙 <b>Бесплатно</b> — {free} сказки в подарок. Только текст.\n\n"
    "✨ <b>Подписка — 490 ₽/мес</b>\n"
    "• Безлимит сказок\n"
    "• Озвучка нежным голосом (ElevenLabs)\n"
    "• Картинка-обложка к каждой сказке\n"
    "• Лучшее качество текста\n"
    "• Архив всех сказок\n"
    "• Отмена в любой момент\n\n"
    "🎁 <b>Подарок близким — 199 ₽</b>\n"
    "Одна персональная сказка под имя ребёнка, с озвучкой и обложкой. "
    "Получаете ссылку — отправляете близким.\n\n"
    "<i>Нажимая «Подписаться» или «В подарок», вы принимаете публичную оферту "
    "и даёте согласие на обработку персональных данных. Подробно — /legal</i>"
)


@router.callback_query(F.data == "bill:plans")
async def cb_plans(call: CallbackQuery) -> None:
    # Если юзер пришёл от партнёра и ещё не платил — показываем баннер со скидкой
    amount, partner, discount_pct = await compute_subscription_price(call.from_user.id)
    banner = ""
    if discount_pct > 0 and partner is not None:
        banner = (
            f"🎁 <b>Спецпредложение от {partner.name}</b>\n"
            f"Первый месяц подписки — <b>{amount/100:.0f} ₽</b> "
            f"(вместо {config.price_sub_kopecks/100:.0f} ₽, экономия −{discount_pct}%).\n"
            f"Действует на ваш первый платёж. Дальше — обычная цена 490 ₽/мес.\n\n"
        )
    await call.message.edit_text(
        banner + PLANS_TEXT.format(free=config.free_story_limit),
        reply_markup=paywall_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "bill:sub")
async def cb_sub(call: CallbackQuery, bot: Bot) -> None:
    """Сразу открываем оплату. Нажатие кнопки «Подписаться» = конклюдентное
    согласие с офертой и политикой (ст. 438 ГК РФ). Юзер уже видит мелкую
    italic-строку про согласие в окне «Тарифы» (см. PLANS_TEXT).

    Записываем `tos_accepted_at` на этом этапе как audit-trail."""
    import datetime as dt

    await call.answer()

    async with Session() as s:
        u = (
            await s.execute(select(User).where(User.telegram_id == call.from_user.id))
        ).scalar_one_or_none()
        if u and not u.tos_accepted_at:
            u.tos_accepted_at = dt.datetime.now(dt.timezone.utc)
            await s.commit()
            logger.info("TOS accepted (implicit by 'Подписаться') user=%s at %s",
                        call.from_user.id, u.tos_accepted_at)

    await create_subscription_invoice(bot, call.message.chat.id, call.from_user.id)


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, bot: Bot) -> None:
    await bot.answer_pre_checkout_query(query.id, ok=True)


@router.message(F.successful_payment)
async def on_paid(message: Message, bot: Bot) -> None:
    sp = message.successful_payment
    kind, user, payment = await process_successful_payment(
        telegram_user_id=message.from_user.id,
        payload=sp.invoice_payload,
        total_amount=sp.total_amount,
        telegram_charge_id=sp.telegram_payment_charge_id,
        provider_charge_id=sp.provider_payment_charge_id,
    )
    # Партнёрская комиссия — если юзер пришёл от партнёра, начислим и уведомим его.
    # Идемпотентно (commit по unique payment_id).
    await attribute_payment_to_partner(payment, user, bot)
    # Бонус приглашающему: 50% от первого платежа (через bonus_stories для простоты — 5 сказок)
    async with Session() as s:
        ref = (
            await s.execute(select(Referral).where(Referral.invited_id == user.id))
        ).scalar_one_or_none()
        if ref and not ref.bonus_granted and kind == PaymentKind.subscription:
            inviter = await s.get(User, ref.inviter_id)
            if inviter:
                inviter.bonus_stories = (inviter.bonus_stories or 0) + 5
                ref.bonus_granted = True
                await s.commit()
                try:
                    await bot.send_message(
                        inviter.telegram_id,
                        "Ваш друг оформил подписку — вам +5 бонусных сказок. Спасибо!",
                    )
                except Exception:
                    pass

    if kind == PaymentKind.subscription:
        until = user.subscription_until.strftime("%d.%m.%Y") if user.subscription_until else "—"
        await message.answer(
            f"🎉 Подписка активна до <b>{until}</b>.\n"
            "Теперь сказки с озвучкой и обложкой — без лимитов.\n\n"
            "Создать сказку:",
            reply_markup=main_menu_kb(),
        )
    else:
        await message.answer(
            "🎁 Подарок оплачен. Готовлю сказку — это займёт около 15–20 секунд."
        )
        # Передаём в gift-флоу — он сам сгенерирует сказку и отправит покупателю
        from .gift import complete_gift_after_payment
        await complete_gift_after_payment(bot, message.from_user.id)


@router.message(F.text == "/cancel_subscription")
async def cancel_sub(message: Message) -> None:
    async with Session() as s:
        u = (await s.execute(select(User).where(User.telegram_id == message.from_user.id))).scalar_one()
        u.subscription_status = SubscriptionStatus.cancelled
        u.yookassa_payment_method_id = None
        await s.commit()
    until = u.subscription_until.strftime("%d.%m.%Y") if u.subscription_until else "—"
    await message.answer(
        f"Подписка отменена. Доступ к платным функциям сохранится до {until}.\n"
        "Дальше списаний не будет."
    )


@router.message(F.text == "/refund")
async def request_refund(message: Message) -> None:
    """Самостоятельный возврат в первые 7 дней."""
    async with Session() as s:
        u = (await s.execute(select(User).where(User.telegram_id == message.from_user.id))).scalar_one()
    if not u.subscription_until:
        await message.answer("Активной подписки не вижу. Если что-то не так — напишите /support.")
        return
    days = (dt.datetime.now(dt.timezone.utc) - (u.subscription_until - dt.timedelta(days=30))).days
    if days > 7:
        await message.answer(
            "Возврат без вопросов — только в первые 7 дней после оплаты. "
            "Сейчас напишите /support, разберёмся в индивидуальном порядке."
        )
        return
    await message.answer(
        "Хорошо. Возврат оформим в течение 1–2 рабочих дней через ЮKassa. "
        "Подписка отключена. Спасибо, что попробовали!"
    )
    async with Session() as s:
        u = (await s.execute(select(User).where(User.telegram_id == message.from_user.id))).scalar_one()
        u.subscription_status = SubscriptionStatus.cancelled
        u.yookassa_payment_method_id = None
        await s.commit()
    # Уведомить админа
    for admin_id in config.admin_ids:
        try:
            await message.bot.send_message(admin_id, f"REFUND-REQUEST: tg={message.from_user.id}")
        except Exception:
            pass
