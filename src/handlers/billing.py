"""Платежи. Telegram Payments (provider=ЮKassa) + рекуррент через API ЮKassa.

Текущие тарифы (см. services/billing.py):
  - bill:single  → 149 ₽ за одну сказку
  - bill:monthly → 2990 ₽/мес подписка (одна сказка/день, рекуррент)

Legacy/скрытое:
  - bill:pack — handler жив, но кнопка убрана из paywall_kb (премиум-фокус
                на single+monthly с мая 2026). Возврат — добавлением
                кнопки обратно в keyboards/inline.py.
  - bill:sub  → перенаправляется на bill:monthly (старый callback из
                490₽-времён, в архивных пушах продолжает работать).
  - bill:plans → показ paywall (видимые тарифы).
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
    "<b>🌟 Тарифы</b>\n\n"
    "🕯 <b>Знакомство — бесплатно</b>\n"
    "{free} сказка-демо в подарок при первом запуске.\n\n"
    "🕯 <b>Одна сказка на вечер — 149 ₽</b>\n"
    "Разовая покупка. Без обязательств и подписок — просто одна тёплая "
    "персональная история перед сном.\n\n"
    "🌙 <b>Сказка каждый вечер на месяц — 2990 ₽</b>\n"
    "Одна персональная сказка ежедневно тридцать дней — меньше 100 ₽ "
    "за каждую. Продление автоматическое, отмена — в один клик.\n\n"
    "<b>В каждой сказке для Вас приготовлено:</b>\n"
    "📖 PDF-книжечка с тремя авторскими иллюстрациями — открыли и "
    "читаете малышу вслух\n"
    "🌙 Персональная сказка на ночь, со смыслом — Ваш ребёнок в ней "
    "главный герой\n"
    "👩‍👧 А главное — читаете Вы. Не машина. Малыш засыпает под "
    "Ваш голос, и это самое тёплое, что может быть\n\n"
    "<i>Соглашаясь продолжить, Вы принимаете нашу публичную оферту "
    "и даёте согласие на обработку личных данных. Подробности — /legal</i>"
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
            f"💌 <b>Особое приглашение от {partner.name}</b>\n"
            f"Ваша первая покупка — со скидкой −{discount_pct}%.\n"
            f"Например, месяц сказок = {monthly_amount/100:.0f} ₽ "
            f"вместо обычных {config.price_monthly_kopecks/100:.0f} ₽.\n\n"
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
    """Одна сказка 149 ₽."""
    await call.answer()
    await _accept_tos(call.from_user.id)
    await create_single_invoice(bot, call.message.chat.id, call.from_user.id)


@router.callback_query(F.data == "bill:pack")
async def cb_pack(call: CallbackQuery, bot: Bot) -> None:
    """Пакет 15 сказок (скрыт из UI с мая 2026, handler оставлен)."""
    await call.answer()
    await _accept_tos(call.from_user.id)
    await create_pack_invoice(bot, call.message.chat.id, call.from_user.id)


@router.callback_query(F.data == "bill:monthly")
async def cb_monthly(call: CallbackQuery, bot: Bot) -> None:
    """Подписка 2990 ₽/мес (одна сказка в день, рекуррент)."""
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

    # Реферальный бонус: +config.referral_bonus (по умолчанию 1) приглашающему
    # ТОЛЬКО при первой успешной оплате друга (любого тарифа: single/pack/monthly).
    # Просто переход по ссылке без оплаты бонуса не даёт. Один друг — один бонус,
    # повторные оплаты того же друга бонус не дублируют (защита flag bonus_granted).
    if kind in (
        PaymentKind.single_story,
        PaymentKind.pack_15,
        PaymentKind.monthly_sub,
        PaymentKind.subscription,  # legacy
    ):
        async with Session() as s:
            ref = (
                await s.execute(select(Referral).where(Referral.invited_id == user.id))
            ).scalar_one_or_none()
            if ref and not ref.bonus_granted:
                inviter = await s.get(User, ref.inviter_id)
                if inviter:
                    bonus = config.referral_bonus
                    inviter.bonus_stories = (inviter.bonus_stories or 0) + bonus
                    ref.bonus_granted = True
                    await s.commit()
                    try:
                        suffix = "сказка" if bonus == 1 else "сказки"
                        await bot.send_message(
                            inviter.telegram_id,
                            f"💌 Радостная новость: близкий человек, "
                            f"которого Вы пригласили, заказал свою первую "
                            f"сказку. От души благодарим — и оставляем для "
                            f"Вас +{bonus} бонусную {suffix} на счёте.",
                        )
                    except Exception:
                        pass

    # Ответ юзеру в зависимости от типа покупки
    if kind in (PaymentKind.monthly_sub, PaymentKind.subscription):
        until = user.subscription_until.strftime("%d.%m.%Y") if user.subscription_until else "—"
        await message.answer(
            f"🌙 Чудесно — теперь сказка ждёт Вас каждый вечер до "
            f"<b>{until}</b>.\n"
            "Просто загляните сюда, когда наступит время ритуала.\n\n"
            "Можем сложить первую прямо сегодня:",
            reply_markup=main_menu_kb(),
        )
    elif kind == PaymentKind.pack_15:
        await message.answer(
            f"📖 Ваш пакет из <b>{user.pack_stories_remaining}</b> сказок "
            f"бережно отложен на полке. Открывайте по одной в день, "
            f"когда сердце попросит — сроки не сгорят.\n\n"
            "Если хочется — можем сложить первую прямо сейчас:",
            reply_markup=main_menu_kb(),
        )
    elif kind == PaymentKind.single_story:
        await message.answer(
            "🕯 Свечи зажжены, перо обмакнуто в чернила. Сейчас наша "
            "сказочница сложит для Вас сегодняшнюю историю.\n\n"
            "Если у Вас уже есть герой на примете — приступим:",
            reply_markup=main_menu_kb(),
        )
    elif kind == PaymentKind.gift:
        await message.answer(
            "💌 Ваш подарок принят. Готовлю особую сказку — это займёт "
            "пару тёплых минут."
        )
        from .gift import complete_gift_after_payment
        await complete_gift_after_payment(bot, message.from_user.id)
    else:
        await message.answer(
            "Спасибо. Можем приступить к сегодняшней сказке:",
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
        f"🕯 Хорошо. Сказки будут приходить как и прежде до "
        f"<b>{until}</b>. После — без лишних слов и списаний.\n\n"
        f"Всегда будем рады, если решите вернуться."
    )


@router.message(F.text == "/refund")
async def request_refund(message: Message) -> None:
    """Самостоятельный возврат в первые 7 дней (только для подписки).
    Для разовой/пакета — через /support вручную."""
    async with Session() as s:
        u = (await s.execute(select(User).where(User.telegram_id == message.from_user.id))).scalar_one()
    if not u.subscription_until:
        await message.answer(
            "🕯 Активной подписки у нас за Вами не числится. "
            "Если речь о разовой сказке или пакете — напишите нам "
            "через /support, обязательно разберёмся."
        )
        return
    days = (dt.datetime.now(dt.timezone.utc) - (u.subscription_until - dt.timedelta(days=30))).days
    if days > 7:
        await message.answer(
            "🕯 Возврат без вопросов мы делаем в первые семь дней после "
            "оплаты. Сейчас напишите в /support — рассмотрим Вашу историю "
            "и постараемся найти доброе решение."
        )
        return
    await message.answer(
        "🕯 Конечно. Возврат оформим в течение одного-двух рабочих дней — "
        "средства вернутся на ту же карту через ЮKassa. Подписка отключена. "
        "Благодарим, что попробовали — будем очень рады, если когда-нибудь "
        "решите вернуться."
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
