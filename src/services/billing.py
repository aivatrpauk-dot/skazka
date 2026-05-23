"""Биллинг для премиум-тарифов.

Три типа покупки:
  1. single_story  — разовая сказка 99 ₽ (без рекуррента, без save_payment_method)
  2. pack_15       — пакет 15 сказок 999 ₽ (без рекуррента, кладём +15 на счётчик)
  3. monthly_sub   — месячная подписка 1485 ₽ (РЕКУРРЕНТ через ЮKassa API, save_payment_method=True)

Первая оплата по любому из тарифов идёт через Telegram Payments
(provider_token = ЮKassa). Telegram сам показывает форму, СБП/карты,
чек 54-ФЗ выбивается ЮKassa.

Дальнейшие списания (только для monthly_sub, 1485 ₽/мес) — через прямой API
ЮKassa, payment_method_id из первой успешной оплаты.

Подарок (gift, 199 ₽) — оставлен для функции /gift, отдельный flow."""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from datetime import timedelta
from typing import Literal

import httpx
from aiogram import Bot
from aiogram.types import LabeledPrice

from sqlalchemy import select

from ..config import config
from ..db import Partner, Payment, PaymentKind, Session, SubscriptionStatus, User

logger = logging.getLogger(__name__)

YOOKASSA_BASE = "https://api.yookassa.ru/v3"

# Payload prefixes — короткие маркеры что покупает юзер. Парсятся в
# process_successful_payment по первой части (split ':').
PAYLOAD_SINGLE = "sng"      # single_story
PAYLOAD_PACK = "pck"        # pack_15
PAYLOAD_MONTHLY = "mon"     # monthly_sub
PAYLOAD_GIFT = "gift"       # gift (legacy)
PAYLOAD_SUB_LEGACY = "sub"  # старая 490 ₽ подписка (для backward compat если в очереди есть)


# ─────────────────── Партнёрская скидка (для monthly_sub) ───────────────────
# Партнёрская скидка теперь применяется только к месячной подписке
# (где есть смысл «первый месяц со скидкой») и не применяется к разовой/пакету.

async def compute_subscription_price(
    telegram_user_id: int,
    tariff: Literal["single", "pack", "monthly"] = "monthly",
) -> tuple[int, Partner | None, int]:
    """Считает цену для конкретного тарифа конкретного юзера с учётом
    партнёрской скидки (применяется только к первой покупке от партнёра).

    Возвращает (amount_kopecks, partner_or_None, discount_pct).
    """
    base_amount = {
        "single": config.price_single_kopecks,
        "pack": config.price_pack_kopecks,
        "monthly": config.price_monthly_kopecks,
    }[tariff]

    async with Session() as s:
        u = (
            await s.execute(select(User).where(User.telegram_id == telegram_user_id))
        ).scalar_one_or_none()
        if not u or not u.partner_id:
            return base_amount, None, 0

        partner = await s.get(Partner, u.partner_id)
        if not partner or not partner.active or partner.promo_discount_pct <= 0:
            return base_amount, partner, 0

        # Уже была хоть одна успешная оплата (любого типа)? Тогда скидка не применяется.
        already = (
            await s.execute(
                select(Payment.id).where(
                    Payment.user_id == u.id,
                    Payment.succeeded.is_(True),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if already:
            return base_amount, partner, 0

        discount_pct = partner.promo_discount_pct
        discounted = base_amount * (100 - discount_pct) // 100
        return discounted, partner, discount_pct


# ─────────────────── Общий helper для создания инвойса ───────────────────

async def _send_invoice(
    bot: Bot,
    chat_id: int,
    *,
    title: str,
    description: str,
    payload: str,
    amount_kopecks: int,
    receipt_desc: str,
    save_payment_method: bool,
    item_label: str,
) -> None:
    """Унифицированный вызов bot.send_invoice с правильным receipt 54-ФЗ
    и опциональным save_payment_method (для рекуррента)."""
    provider_data: dict = {
        "receipt": {
            "items": [
                {
                    "description": receipt_desc,
                    "quantity": "1",
                    "amount": {"value": f"{amount_kopecks/100:.2f}", "currency": "RUB"},
                    "vat_code": 1,
                    "payment_subject": "service",
                    "payment_mode": "full_prepayment",
                }
            ]
        }
    }
    if save_payment_method:
        provider_data["save_payment_method"] = True

    await bot.send_invoice(
        chat_id=chat_id,
        title=title,
        description=description,
        payload=payload,
        provider_token=config.yookassa_provider_token,
        currency="RUB",
        prices=[LabeledPrice(label=item_label, amount=amount_kopecks)],
        need_email=True,
        send_email_to_provider=True,
        provider_data=json.dumps(provider_data, ensure_ascii=False),
    )


# ─────────────────── Три новых invoice flow ───────────────────

async def create_single_invoice(bot: Bot, chat_id: int, user_id: int) -> None:
    """Инвойс на одну сказку 99 ₽. Без рекуррента."""
    amount, partner, discount_pct = await compute_subscription_price(user_id, "single")
    if discount_pct > 0 and partner is not None:
        title = f"Одна сказка (−{discount_pct}%)"
        item_label = f"Сказка — −{discount_pct}% от {partner.name}"
        description = (
            f"Одна персональная сказка с озвучкой и обложкой.\n"
            f"🎁 Скидка партнёра: {amount/100:.0f} ₽ вместо "
            f"{config.price_single_kopecks/100:.0f} ₽."
        )
        receipt_desc = f"Сказка «Сказка» — скидка −{discount_pct}%"
    else:
        title = "Одна сказка"
        item_label = "Сказка «Сказка»"
        description = "Одна персональная сказка с озвучкой и обложкой"
        receipt_desc = "Одна сказка «Сказка»"

    payload = f"{PAYLOAD_SINGLE}:{user_id}:{uuid.uuid4().hex[:12]}"
    await _send_invoice(
        bot, chat_id,
        title=title, description=description, payload=payload,
        amount_kopecks=amount, receipt_desc=receipt_desc,
        save_payment_method=False, item_label=item_label,
    )


async def create_pack_invoice(bot: Bot, chat_id: int, user_id: int) -> None:
    """Инвойс на пакет из 15 сказок 999 ₽ (−34% от штучной). Без рекуррента.
    После оплаты пакет кладётся на счётчик user.pack_stories_remaining."""
    amount, partner, discount_pct = await compute_subscription_price(user_id, "pack")
    n = config.pack_stories_count
    if discount_pct > 0 and partner is not None:
        title = f"Пакет {n} сказок (−{discount_pct}%)"
        item_label = f"Пакет {n} сказок — −{discount_pct}% от {partner.name}"
        description = (
            f"Пакет {n} сказок (одна сказка в день). С озвучкой и обложкой.\n"
            f"🎁 Скидка партнёра: {amount/100:.0f} ₽ вместо "
            f"{config.price_pack_kopecks/100:.0f} ₽."
        )
        receipt_desc = f"Пакет {n} сказок — скидка −{discount_pct}%"
    else:
        title = f"Пакет {n} сказок"
        item_label = f"Пакет {n} сказок «Сказка»"
        description = (
            f"Пакет {n} сказок (одна в день) — экономия 34% против штучной цены.\n"
            f"С озвучкой и обложкой. Срока годности у пакета нет."
        )
        receipt_desc = f"Пакет {n} сказок «Сказка»"

    payload = f"{PAYLOAD_PACK}:{user_id}:{uuid.uuid4().hex[:12]}"
    await _send_invoice(
        bot, chat_id,
        title=title, description=description, payload=payload,
        amount_kopecks=amount, receipt_desc=receipt_desc,
        save_payment_method=False, item_label=item_label,
    )


async def create_monthly_invoice(bot: Bot, chat_id: int, user_id: int) -> None:
    """Инвойс на месячную подписку 1485 ₽ (одна сказка в день, −50% от штучной).
    Рекуррент: при первой оплате save_payment_method=True."""
    amount, partner, discount_pct = await compute_subscription_price(user_id, "monthly")
    if discount_pct > 0 and partner is not None:
        title = f"Подписка «Сказка» 1 месяц (−{discount_pct}%)"
        item_label = f"Подписка 1 месяц — −{discount_pct}% от {partner.name}"
        description = (
            f"Сказка каждый день на месяц. С озвучкой и обложкой.\n"
            f"🎁 Скидка партнёра: первый месяц {amount/100:.0f} ₽ вместо "
            f"{config.price_monthly_kopecks/100:.0f} ₽. Дальше — обычная цена "
            f"{config.price_monthly_kopecks/100:.0f} ₽/мес. Отмена в любой момент."
        )
        receipt_desc = f"Подписка «Сказка» 1 месяц — скидка −{discount_pct}%"
    else:
        title = "Подписка «Сказка» 1 месяц"
        item_label = "Подписка «Сказка» 1 месяц"
        description = (
            "Сказка каждый день на месяц — экономия 50% против штучной цены.\n"
            "С озвучкой и обложкой. Отмена в любой момент."
        )
        receipt_desc = "Подписка «Сказка» 1 месяц"

    payload = f"{PAYLOAD_MONTHLY}:{user_id}:{uuid.uuid4().hex[:12]}"
    await _send_invoice(
        bot, chat_id,
        title=title, description=description, payload=payload,
        amount_kopecks=amount, receipt_desc=receipt_desc,
        save_payment_method=True, item_label=item_label,
    )


# ─────────────────── Legacy функции (deprecated, не использовать в новом коде) ───────────────────

async def create_subscription_invoice(bot: Bot, chat_id: int, user_id: int) -> None:
    """LEGACY: алиас на новый месячный invoice. Оставлен для backward compat
    handler-кода, который ещё не перерисован. Новый код должен звать
    create_monthly_invoice напрямую."""
    await create_monthly_invoice(bot, chat_id, user_id)


async def create_gift_invoice(bot: Bot, chat_id: int, user_id: int, recipient: str) -> None:
    """Подарочная сказка 199 ₽ — без рекуррента, отдельный товар."""
    payload = f"{PAYLOAD_GIFT}:{user_id}:{uuid.uuid4().hex[:12]}"
    await _send_invoice(
        bot, chat_id,
        title=f"Подарочная сказка для {recipient}",
        description="Готовая сказка с озвучкой и обложкой — отправим ссылкой",
        payload=payload,
        amount_kopecks=config.price_gift_kopecks,
        receipt_desc=f"Подарочная сказка для {recipient}",
        save_payment_method=False,
        item_label=f"Подарочная сказка для {recipient}",
    )


# ─────────────────── Обработка successful_payment ───────────────────

async def process_successful_payment(
    *,
    telegram_user_id: int,
    payload: str,
    total_amount: int,
    telegram_charge_id: str,
    provider_charge_id: str,
) -> tuple[PaymentKind, User, Payment]:
    """Обрабатывает successful_payment update. Возвращает (kind, user, payment).

    Разветвляется по payload prefix:
      sng → single_story (ничего на стороне юзера не меняем — он просто получит
            следующую сказку из обычного flow story.py, проверка _can_make_story
            увидит свежую оплату single_story)
      pck → pack_15      (+15 к user.pack_stories_remaining)
      mon → monthly_sub  (активируем subscription_until +30 дней + сохраняем
                          payment_method_id для рекуррента)
      gift → gift        (legacy подарок)
      sub  → legacy subscription (исторический payload, оставлен для on-the-fly
                                  обработки если очередь была накоплена до миграции)
    """
    parts = payload.split(":", 2)
    kind_str = parts[0] if parts else ""
    kind_map: dict[str, PaymentKind] = {
        PAYLOAD_SINGLE: PaymentKind.single_story,
        PAYLOAD_PACK: PaymentKind.pack_15,
        PAYLOAD_MONTHLY: PaymentKind.monthly_sub,
        PAYLOAD_GIFT: PaymentKind.gift,
        PAYLOAD_SUB_LEGACY: PaymentKind.subscription,
    }
    kind = kind_map.get(kind_str, PaymentKind.single_story)

    async with Session() as s:
        user = (
            await s.execute(select(User).where(User.telegram_id == telegram_user_id))
        ).scalar_one()

        # Защита от replay: один telegram_payment_charge_id = один Payment.
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

        # Активация в зависимости от типа покупки
        now = dt.datetime.now(dt.timezone.utc)

        if kind == PaymentKind.pack_15:
            # Добавляем 15 сказок на счётчик (или config.pack_stories_count если меняли)
            user.pack_stories_remaining = (user.pack_stories_remaining or 0) + config.pack_stories_count
            user.pack_purchased_at = now
            logger.info("Pack: user=%s +%d stories (total=%d)",
                        telegram_user_id, config.pack_stories_count, user.pack_stories_remaining)

        elif kind in (PaymentKind.monthly_sub, PaymentKind.subscription):
            # Активируем месячную подписку. Для нового monthly_sub и для legacy subscription —
            # одинаково: +30 дней, save payment_method для рекуррента.
            if config.yookassa_secret_key and config.yookassa_shop_id:
                try:
                    pm_id = await _fetch_yookassa_payment_method(provider_charge_id)
                    if pm_id:
                        user.yookassa_payment_method_id = pm_id
                except Exception as e:
                    logger.exception("Не удалось получить payment_method_id: %s", e)

            user.subscription_status = SubscriptionStatus.active
            base = (
                user.subscription_until
                if user.subscription_until and user.subscription_until > now
                else now
            )
            user.subscription_until = base + timedelta(days=30)
            logger.info("Monthly sub: user=%s active until %s",
                        telegram_user_id, user.subscription_until)

        elif kind == PaymentKind.single_story:
            # Разовая покупка → +1 к счётчику неиспользованных сказок. При генерации
            # списывается без daily-лимита (юзер платит каждый раз).
            user.single_stories_remaining = (user.single_stories_remaining or 0) + 1
            logger.info("Single story purchased: user=%s (total unused=%d)",
                        telegram_user_id, user.single_stories_remaining)

        elif kind == PaymentKind.gift:
            logger.info("Gift purchased: user=%s", telegram_user_id)

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
    """Достаёт payment_method.id из платежа ЮKassa (для рекуррента подписки)."""
    url = f"{YOOKASSA_BASE}/payments/{payment_id}"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(url, auth=(config.yookassa_shop_id, config.yookassa_secret_key))
        if resp.status_code != 200:
            logger.error("YooKassa fetch payment failed %s: %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        return (data.get("payment_method") or {}).get("id")


async def create_recurring_payment(user: User) -> tuple[bool, Payment | None, User | None]:
    """Ежемесячное автосписание для monthly_sub. Использует сохранённый
    payment_method_id. Списываем по текущей цене 1485 ₽.

    Возвращает (succeeded, payment, user). Используется для регистрации
    партнёрской комиссии в renewal_worker.
    """
    if not user.yookassa_payment_method_id:
        return False, None, None
    if not (config.yookassa_secret_key and config.yookassa_shop_id):
        return False, None, None

    amount_kopecks = config.price_monthly_kopecks
    url = f"{YOOKASSA_BASE}/payments"
    idem = uuid.uuid4().hex
    payload = {
        "amount": {"value": f"{amount_kopecks/100:.2f}", "currency": "RUB"},
        "payment_method_id": user.yookassa_payment_method_id,
        "capture": True,
        "description": "Продление подписки «Сказка» на 1 месяц",
        "receipt": {
            "customer": {"email": f"tg{user.telegram_id}@skazka.bot"},
            "items": [{
                "description": "Подписка «Сказка» 1 месяц",
                "quantity": "1.00",
                "amount": {"value": f"{amount_kopecks/100:.2f}", "currency": "RUB"},
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
            kind=PaymentKind.monthly_renewal,
            amount_kopecks=amount_kopecks,
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
