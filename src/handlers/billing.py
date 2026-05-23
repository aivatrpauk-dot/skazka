"""Платежи. Telegram Payments (provider=ЮKassa) + рекуррент через API ЮKassa.

Три тарифа (см. services/billing.py):
  - bill:single  → 99 ₽ за одну сказку
  - bill:pack    → 999 ₽ за пакет 15 сказок (−34%)
  - bill:monthly → 1485 ₽/мес подписка (−50%, рекуррент)

Legacy:
  - bill:sub  → перенаправляется на bill:monthly (старый callback в архивных
                сообщениях/пушах продолжает работать).
  - bill:plans → показ paywall (все три тарифа сразу).
"""
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
    create_monthly_invoice,
    create_pack_invoice,
    create_single_invoice,
    process_successful_payment,
)

logger = logging.getLogger(__name__)
router = Router(name="billing")


# ─────────────────── Текст витрины тарифов ───────────────────
# Показывается при нажатии «Тарифы» / при попадании на paywall.
# Подсветка экономии (−34%, −50%) — главный triggers для конверсии в пакет/подписку.

PLANS_TEXT = (
    "<b>Тарифы</b>\n\n"
    "🌙 <b>Бесплатно</b> — {free} сказка в подарок при первом запуске.\n\n"
    "✨ <b>Одна сказка — 99 ₽</b>\n"
    "Разовая покупка. Никаких подписок, без ограничений по дням.\n\n"
    "📚 <b>Пакет 15 сказок — 999 ₽</b> <i>(−34%)</i>\n"
    "Одна сказка в день в течение двух недель. Срок пакета не сгорает —\n"
    "пользуйтесь когда хочется. Экономия 486 ₽ против штучной цены.\n\n"
    "🌟 <b>Подписка на месяц — 1485 ₽</b> <i>(−50%)</i>\n"
    "Сказка каждый день на месяц. Автопродление, отмена в любой момент.\n"
    "Экономия 1485 ₽ против штучной цены.\n\n"
    "🎁 <b>Подарок близким — 199 ₽</b>\n"
    "Одна персональная сказка под имя ребёнка — отправляете ссылкой.\n\n"
    "Во всех тарифах: озвучка тёплым женским голосом + обложка-картинка к каждой сказке.\n\n"
    "<i>Нажимая «Купить» / «Подписаться», вы принимаете публичную оферту "
    "и даёте согласие на обработку персональных данных. Подробно — /legal</i>"
)


@router.callback_query(F.data == "bill:plans")
async def cb_plans(call: CallbackQuery) -> None:
    """Показать paywall с тремя тарифами. Если юзер от партнёра и ещё не платил
    — добавим баннер со скидкой (для подписки)."""
    monthly_amount, partner, discount_pct = await compute_subscription_price(
        call.from_user.id, "monthly"
    )
    banner = ""
    if discount_pct > 0 and partner is not None:
        banner = (
            f"🎁 <b>Спецпредложение от {partner.name}</b>\n"
            f"Первая покупка — со скидкой −{discount_pct}%.\n"
            f"Например, подписка на месяц = {monthly_amount/100:.0f} ₽ "
            f"вместо {config.price_monthly_kopecks/100:.0f} ₽.\n\n"
        )
    await call.message.edit_text(
        banner + PLANS_TEXT.format(free=config.free_story_limit),
        reply_markup=paywall_kb(),
    )
    await call.answer()


async def _accept_tos(telegram_id: int) -> None:
    """Конклюдентное согласие с офертой = нажатие любой кнопки оплаты.
    Записываем audit-trail tos_accepted_at если ещё не записано."""
    async with Session() as s:
        u = (
            await s.execute(select(User).where(User.telegram_id == telegram_id))
        ).scalar_one_or_none()
        if u and not u.tos_accepted_at:
            u.tos_accepted_at = dt.datetime.now(dt.timezone.utc)
            await s.commit()
            logger.info("TOS accepted (implicit) user=%s at %s",
                        telegram_id, u.tos_accepted_at)


# ─────────────────── Три новых хендлера ───────────────────

@router.callback_query(F.data == "bill:single")
async def cb_single(call: CallbackQuery, bot: Bot) -> None:
    """Одна сказка 99 ₽."""
    await call.answer()
    await _accept_tos(call.from_user.id)
    await create_single_invoice(bot, call.message.chat.id, call.from_user.id)


@router.callback_query(F.data == "bill:pack")
async def cb_pack(call: CallbackQuery, bot: Bot) -> None:
    """Пакет 15 сказок 999 ₽."""
    await call.answer()
    await _accept_tos(call.from_user.id)
    await create_pack_invoice(bot, call.message.chat.id, call.from_user.id)


@router.callback_query(F.data == "bill:monthly")
async def cb_monthly(call: CallbackQuery, bot: Bot) -> None:
    """Подписка 1485 ₽/мес."""
    await call.answer()
    await _accept_tos(call.from_user.id)
    await create_monthly_invoice(bot, call.message.chat.id, call.from_user.id)


# ─────────────────── Legacy ───────────────────

@router.callback_query(F.data == "bill:sub")
async def cb_sub_legacy(call: CallbackQuery, bot: Bot) -> None:
    """Старый callback из 490 ₽-времён. Перенаправляем на новую месячную."""
    await call.answer()
    await _accept_tos(call.from_user.id)
    await create_monthly_invoice(bot, call.message.chat.id, call.from_user.id)


# ─────────────────── pre_checkout + successful_payment ───────────────────

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

    # Партнёрская комиссия (если юзер пришёл от партнёра)
    await attribute_payment_to_partner(payment, user, bot)

    # Бонус приглашающему: +5 сказок только при первой подписке (monthly или legacy)
    if kind in (PaymentKind.monthly_sub, PaymentKind.subscription):
        async with Session() as s:
            ref = (
                await s.execute(select(Referral).where(Referral.invited_id == user.id))
            ).scalar_one_or_none()
            if ref and not ref.bonus_granted:
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

    # Ответ юзеру в зависимости от типа покупки
    if kind in (PaymentKind.monthly_sub, PaymentKind.subscription):
        until = user.subscription_until.strftime("%d.%m.%Y") if user.subscription_until else "—"
        await message.answer(
            f"🎉 Подписка активна до <b>{until}</b>.\n"
            "Каждый день — новая сказка с озвучкой и обложкой.\n\n"
            "Создать сказку:",
            reply_markup=main_menu_kb(),
        )
    elif kind == PaymentKind.pack_15:
        await message.answer(
            f"📚 Пакет активирован! У вас <b>{user.pack_stories_remaining}</b> сказок "
            f"(одна в день).\n\n"
            "Создать сказку:",
            reply_markup=main_menu_kb(),
        )
    elif kind == PaymentKind.single_story:
        await message.answer(
            "✨ Оплата прошла! Сейчас сделаю вашу сказку.\n\n"
            "Создать сказку:",
            reply_markup=main_menu_kb(),
        )
    elif kind == PaymentKind.gift:
        await message.answer(
            "🎁 Подарок оплачен. Готовлю сказку — это займёт около 15–20 секунд."
        )
        from .gift import complete_gift_after_payment
        await complete_gift_after_payment(bot, message.from_user.id)
    else:
        await message.answer(
            "Оплата прошла. Создать сказку:",
            reply_markup=main_menu_kb(),
        )


@router.message(F.text == "/cancel_subscription")
async def cancel_sub(message: Message) -> None:
    async with Session() as s:
        u = (await s.execute(select(User).where(User.telegram_id == message.from_user.id))).scalar_one()
        u.subscription_status = SubscriptionStatus.cancelled
        u.yookassa_payment_method_id = None
        await s.commit()
    until = u.subscription_until.strftime("%d.%m.%Y") if u.subscription_until else "—"
    await message.answer(
        f"Подписка отменена. Доступ к ежедневным сказкам сохранится до {until}.\n"
        "Дальше списаний не будет."
    )


@router.message(F.text == "/refund")
async def request_refund(message: Message) -> None:
    """Самостоятельный возврат в первые 7 дней (только для подписки).
    Для разовой/пакета — через /support вручную."""
    async with Session() as s:
        u = (await s.execute(select(User).where(User.telegram_id == message.from_user.id))).scalar_one()
    if not u.subscription_until:
        await message.answer(
            "Активной подписки не вижу. Если разовая или пакет — напишите /support, разберёмся."
        )
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
    for admin_id in config.admin_ids:
        try:
            await message.bot.send_message(admin_id, f"REFUND-REQUEST: tg={message.from_user.id}")
        except Exception:
            pass
