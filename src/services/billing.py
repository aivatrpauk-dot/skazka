"""Биллинг.
Первая оплата — через встроенные Telegram Payments (provider_token от ЮKassa).
Telegram сам показывает форму, СБП/карты, чек 54-ФЗ выбивается ЮKassa.

Дальнейшие списания (рекуррент 490 ₽/мес) — через прямой API ЮKassa,
используя payment_method_id из первой успешной оплаты (флаг save_payment_method=True)."""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from datetime import timedelta

import httpx
from aiogram import Bot
from aiogram.types import LabeledPrice

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..config import config
from ..db import Partner, Payment, PaymentKind, Session, SubscriptionStatus, User

logger = logging.getLogger(__name__)

YOOKASSA_BASE = "https://api.yookassa.ru/v3"


async def compute_subscription_price(telegram_user_id: int) -> tuple[int, Partner | None, int]:
    """Считает цену подписки для конкретного юзера с учётом партнёрской скидки.

    Возвращает (amount_kopecks, partner_or_None, discount_pct).

    Скидка применяется если:
    - У юзера есть partner_id (пришёл по ?start=<partner_code>)
    - У юзера ещё нет ни одной успешной оплаты подписки/рекуррента
    - У партнёра promo_discount_pct > 0

    Рекурренты в create_recurring_payment всегда списываются по полной цене.
    """
    amount = config.price_sub_kopecks
    async with Session() as s:
        u = (
            await s.execute(select(User).where(User.telegram_id == telegram_user_id))
        ).scalar_one_or_none()
        if not u or not u.partner_id:
            return amount, None, 0

        partner = await s.get(Partner, u.partner_id)
        if not partner or not partner.active or partner.promo_discount_pct <= 0:
            return amount, partner, 0

        # Уже была успешная подписка/рекуррент? Тогда скидка не применяется.
        already = (
            await s.execute(
                select(Payment.id).where(
                    Payment.user_id == u.id,
                    Payment.succeeded.is_(True),
                    Payment.kind.in_([PaymentKind.subscription, PaymentKind.renewal]),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if already:
            return amount, partner, 0

        discount_pct = partner.promo_discount_pct
        discounted = config.price_sub_kopecks * (100 - discount_pct) // 100
        return discounted, partner, discount_pct


async def create_subscription_invoice(bot: Bot, chat_id: int, user_id: int) -> None:
    """Шлёт пользователю invoice на подписку. Если юзер пришёл от партнёра и
    это его первая подписка — цена снижается на partner.promo_discount_pct
    (рекурренты потом по полной цене)."""
    amount, partner, discount_pct = await compute_subscription_price(user_id)

    if discount_pct > 0 and partner is not None:
        title = f"Подписка «Сказка» 1 месяц (−{discount_pct}%)"
        label = f"Подписка «Сказка» 1 месяц — −{discount_pct}% от {partner.name}"
        description = (
            f"Безлимит сказок + озвучка нежным голосом + обложка-картинка.\n"
            f"🎁 Партнёрская скидка: первый месяц {amount/100:.0f} ₽ вместо "
            f"{config.price_sub_kopecks/100:.0f} ₽. Дальше — обычная цена 490 ₽/мес, "
            f"отмена в любой момент."
        )
        receipt_desc = f"Подписка «Сказка» 1 месяц — скидка −{discount_pct}%"
    else:
        title = "Подписка «Сказка» 1 месяц"
        label = "Подписка «Сказка» на 1 месяц"
        description = "Безлимит сказок + озвучка нежным голосом + обложка-картинка"
        receipt_desc = "Подписка «Сказка» 1 месяц"

    prices = [LabeledPrice(label=label, amount=amount)]
    payload = f"sub:{user_id}:{uuid.uuid4().hex[:12]}"
    # provider_data — флаги для ЮKassa (запросить сохранение метода + receipt 54-ФЗ)
    provider_data = {
        "receipt": {
            "items": [
                {
                    "description": receipt_desc,
                    "quantity": "1",
                    "amount": {"value": f"{amount/100:.2f}", "currency": "RUB"},
                    "vat_code": 1,
                    "payment_subject": "service",
                    "payment_mode": "full_prepayment",
                }
            ]
        },
        "save_payment_method": True,
    }
    await bot.send_invoice(
        chat_id=chat_id,
        title=title,
        description=description,
        payload=payload,
        provider_token=config.yookassa_provider_token,
        currency="RUB",
        prices=prices,
        need_email=True,
        send_email_to_provider=True,
        provider_data=__import__("json").dumps(provider_data, ensure_ascii=False),
    )


async def create_gift_invoice(bot: Bot, chat_id: int, user_id: int, recipient: str) -> None:
    prices = [LabeledPrice(label=f"Подарочная сказка для {recipient}", amount=config.price_gift_kopecks)]
    payload = f"gift:{user_id}:{uuid.uuid4().hex[:12]}"
    provider_data = {
        "receipt": {
            "items": [
                {
                    "description": f"Подарочная сказка для {recipient}",
                    "quantity": "1",
                    "amount": {"value": f"{config.price_gift_kopecks/100:.2f}", "currency": "RUB"},
                    "vat_code": 1,
                    "payment_subject": "service",
                    "payment_mode": "full_prepayment",
                }
            ]
        }
    }
    await bot.send_invoice(
        chat_id=chat_id,
        title=f"Подарочная сказка для {recipient}",
        description="Готовая сказка с озвучкой и обложкой — отправим ссылкой",
        payload=payload,
        provider_token=config.yookassa_provider_token,
        currency="RUB",
        prices=prices,
        need_email=True,
        send_email_to_provider=True,
        provider_data=__import__("json").dumps(provider_data, ensure_ascii=False),
    )


async def process_successful_payment(
    *,
    telegram_user_id: int,
    payload: str,
    total_amount: int,
    telegram_charge_id: str,
    provider_charge_id: str,
) -> tuple[PaymentKind, User, Payment]:
    """Обрабатывает successful_payment update. Возвращает (тип, юзер, платёж).

    Возвращаем созданный Payment, чтобы хендлер мог зарегистрировать партнёрскую
    комиссию через services.partners.register_commission(payment, user.partner_id).
    """
    kind_str, _user_str, _nonce = payload.split(":", 2)
    kind = PaymentKind.subscription if kind_str == "sub" else PaymentKind.gift

    async with Session() as s:
        user = (await s.execute(__import__("sqlalchemy").select(User).where(User.telegram_id == telegram_user_id))).scalar_one()

        # Защита от replay: если этот telegram_payment_charge_id уже сохранён —
        # значит мы уже обработали этот платёж, второй раз ничего не делаем.
        existing_payment = (
            await s.execute(
                select(Payment).where(Payment.telegram_payment_charge_id == telegram_charge_id)
            )
        ).scalar_one_or_none()
        if existing_payment:
            logger.warning(
                "Duplicate successful_payment for telegram_charge_id=%s (payment.id=%s) — ignoring",
                telegram_charge_id, existing_payment.id,
            )
            return kind, user, existing_payment

        # save payment_method_id если есть (для рекуррента подписки)
        # provider_payment_charge_id в Telegram = id платежа ЮKassa,
        # для получения payment_method_id нужно дёрнуть YooKassa API:
        if kind == PaymentKind.subscription and config.yookassa_secret_key and config.yookassa_shop_id:
            try:
                payment_method_id = await _fetch_yookassa_payment_method(provider_charge_id)
                if payment_method_id:
                    user.yookassa_payment_method_id = payment_method_id
            except Exception as e:
                logger.exception("Не удалось получить payment_method_id: %s", e)

            user.subscription_status = SubscriptionStatus.active
            now = dt.datetime.now(dt.timezone.utc)
            base = user.subscription_until if user.subscription_until and user.subscription_until > now else now
            user.subscription_until = base + timedelta(days=30)
        # Сохраняем платёж
        payment = Payment(
            user_id=user.id,
            kind=kind,
            amount_kopecks=total_amount,
            telegram_payment_charge_id=telegram_charge_id,
            provider_payment_charge_id=provider_charge_id,
            yookassa_payment_id=provider_charge_id,
            succeeded=True,
        )
        s.add(payment)
        await s.commit()
        await s.refresh(user)
        await s.refresh(payment)
    return kind, user, payment


async def _fetch_yookassa_payment_method(payment_id: str) -> str | None:
    """Достаёт payment_method.id из платежа ЮKassa (для рекуррента)."""
    url = f"{YOOKASSA_BASE}/payments/{payment_id}"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(url, auth=(config.yookassa_shop_id, config.yookassa_secret_key))
        if resp.status_code != 200:
            logger.error("YooKassa fetch payment failed %s: %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        return (data.get("payment_method") or {}).get("id")


async def create_recurring_payment(user: User) -> tuple[bool, Payment | None, User | None]:
    """Списание следующего месяца. Используем сохранённый payment_method_id.

    Возвращает (succeeded, payment, user). Payment и user возвращаем, чтобы
    можно было после успешного списания зарегистрировать партнёрскую комиссию
    в renewal_worker (services.partners.attribute_payment_to_partner).
    """
    if not user.yookassa_payment_method_id:
        return False, None, None
    if not (config.yookassa_secret_key and config.yookassa_shop_id):
        return False, None, None
    url = f"{YOOKASSA_BASE}/payments"
    idem = uuid.uuid4().hex
    payload = {
        "amount": {"value": f"{config.price_sub_kopecks/100:.2f}", "currency": "RUB"},
        "payment_method_id": user.yookassa_payment_method_id,
        "capture": True,
        "description": "Продление подписки «Сказка» на 1 месяц",
        "receipt": {
            "customer": {"email": f"tg{user.telegram_id}@skazka.bot"},
            "items": [{
                "description": "Подписка «Сказка» 1 месяц",
                "quantity": "1.00",
                "amount": {"value": f"{config.price_sub_kopecks/100:.2f}", "currency": "RUB"},
                "vat_code": 1,
                "payment_subject": "service",
                "payment_mode": "full_prepayment",
            }],
        },
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            url,
            json=payload,
            headers={"Idempotence-Key": idem, "Content-Type": "application/json"},
            auth=(config.yookassa_shop_id, config.yookassa_secret_key),
        )
    if resp.status_code not in (200, 201):
        logger.error("Рекуррент не прошёл для %s: %s", user.telegram_id, resp.text[:200])
        return False, None, None
    data = resp.json()
    succeeded = data.get("status") == "succeeded"

    async with Session() as s:
        u = await s.get(User, user.id)
        if u is None:
            return False, None, None
        payment = Payment(
            user_id=u.id,
            kind=PaymentKind.renewal,
            amount_kopecks=config.price_sub_kopecks,
            yookassa_payment_id=data.get("id"),
            succeeded=succeeded,
        )
        s.add(payment)
        if succeeded:
            now = dt.datetime.now(dt.timezone.utc)
            base = u.subscription_until if u.subscription_until and u.subscription_until > now else now
            u.subscription_until = base + timedelta(days=30)
            u.subscription_status = SubscriptionStatus.active
        else:
            u.subscription_status = SubscriptionStatus.past_due
        await s.commit()
        await s.refresh(payment)
        await s.refresh(u)
    return succeeded, payment, u
