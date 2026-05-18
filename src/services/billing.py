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

from ..config import config
from ..db import Payment, PaymentKind, Session, SubscriptionStatus, User

logger = logging.getLogger(__name__)

YOOKASSA_BASE = "https://api.yookassa.ru/v3"


async def create_subscription_invoice(bot: Bot, chat_id: int, user_id: int) -> None:
    """Шлёт пользователю invoice на 490 ₽/мес, сохраняя payment method для рекуррента."""
    prices = [LabeledPrice(label="Подписка «Сказка» на 1 месяц", amount=config.price_sub_kopecks)]
    payload = f"sub:{user_id}:{uuid.uuid4().hex[:12]}"
    # provider_data — флаги для ЮKassa (запросить сохранение метода + receipt 54-ФЗ)
    provider_data = {
        "receipt": {
            "items": [
                {
                    "description": "Подписка «Сказка» 1 месяц",
                    "quantity": "1",
                    "amount": {"value": f"{config.price_sub_kopecks/100:.2f}", "currency": "RUB"},
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
        title="Подписка «Сказка» 1 месяц",
        description="Безлимит сказок + озвучка маминым голосом + обложка-картинка",
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
) -> tuple[PaymentKind, User]:
    """Обрабатывает successful_payment update. Возвращает (тип, юзер)."""
    kind_str, _user_str, _nonce = payload.split(":", 2)
    kind = PaymentKind.subscription if kind_str == "sub" else PaymentKind.gift

    async with Session() as s:
        user = (await s.execute(__import__("sqlalchemy").select(User).where(User.telegram_id == telegram_user_id))).scalar_one()
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
        s.add(Payment(
            user_id=user.id,
            kind=kind,
            amount_kopecks=total_amount,
            telegram_payment_charge_id=telegram_charge_id,
            provider_payment_charge_id=provider_charge_id,
            yookassa_payment_id=provider_charge_id,
            succeeded=True,
        ))
        await s.commit()
        await s.refresh(user)
    return kind, user


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


async def create_recurring_payment(user: User) -> bool:
    """Списание следующего месяца. Используем сохранённый payment_method_id."""
    if not user.yookassa_payment_method_id:
        return False
    if not (config.yookassa_secret_key and config.yookassa_shop_id):
        return False
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
        return False
    data = resp.json()
    succeeded = data.get("status") == "succeeded"

    async with Session() as s:
        u = await s.get(User, user.id)
        if u is None:
            return False
        s.add(Payment(
            user_id=u.id,
            kind=PaymentKind.renewal,
            amount_kopecks=config.price_sub_kopecks,
            yookassa_payment_id=data.get("id"),
            succeeded=succeeded,
        ))
        if succeeded:
            now = dt.datetime.now(dt.timezone.utc)
            base = u.subscription_until if u.subscription_until and u.subscription_until > now else now
            u.subscription_until = base + timedelta(days=30)
            u.subscription_status = SubscriptionStatus.active
        else:
            u.subscription_status = SubscriptionStatus.past_due
        await s.commit()
    return succeeded
