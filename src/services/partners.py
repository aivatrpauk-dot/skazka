"""Логика партнёрки: атрибуция, начисление комиссий, выплаты.

Принципы:
1. **Immutable ledger**: каждая комиссия — отдельная строка в `partner_commissions`,
   привязанная к `payments.id` (уникальный FK). После создания строка не редактируется
   (кроме одной операции «выплата» — это меняет только 4 поля, факт начисления
   сохраняется).
2. **Snapshot %**: на момент создания комиссии фиксируется `share_pct_snapshot`.
   Если потом партнёру повысили/понизили %, старые комиссии не пересчитываются.
3. **Идемпотентность**: уникальный constraint на `payment_id` гарантирует, что
   одна оплата не сгенерирует две комиссии (даже если автору хендлера пришло
   `successful_payment` дважды — например, после рестарта).
4. **Прозрачность партнёру**: партнёр может в любой момент попросить выгрузку
   всех своих строк (commissions) — там есть `payment_id` каждой оплаты,
   который он может cross-check'нуть со своим знанием публики (количество
   подписчиков / время поста).
"""
from __future__ import annotations

import logging
import secrets

from sqlalchemy import select

from ..db import Partner, PartnerCommission, Payment, Session

logger = logging.getLogger(__name__)


async def find_partner_by_code(code: str) -> Partner | None:
    """Ищет активного партнёра по коду из deep-link."""
    code = (code or "").strip().lower()
    if not code:
        return None
    async with Session() as s:
        return (
            await s.execute(select(Partner).where(Partner.code == code, Partner.active.is_(True)))
        ).scalar_one_or_none()


async def find_partner_by_token(token: str) -> Partner | None:
    """Аутентификация партнёра по секретному токену (для /partner_login)."""
    token = (token or "").strip()
    if not token:
        return None
    async with Session() as s:
        return (
            await s.execute(select(Partner).where(Partner.secret_token == token))
        ).scalar_one_or_none()


async def find_partner_by_telegram_id(telegram_id: int) -> Partner | None:
    async with Session() as s:
        return (
            await s.execute(select(Partner).where(Partner.partner_telegram_id == telegram_id))
        ).scalar_one_or_none()


async def create_partner(
    *,
    code: str,
    name: str,
    revenue_share_pct: int = 30,
    promo_discount_pct: int = 50,
    contact: str | None = None,
    notes: str | None = None,
) -> Partner:
    """Создаёт партнёра. Возвращает с secret_token внутри — выдать партнёру лично."""
    code = code.strip().lower()
    if not code or not name:
        raise ValueError("code и name обязательны")
    async with Session() as s:
        existing = (
            await s.execute(select(Partner).where(Partner.code == code))
        ).scalar_one_or_none()
        if existing:
            raise ValueError(f"Партнёр с кодом '{code}' уже существует (id={existing.id})")
        p = Partner(
            code=code,
            name=name,
            contact=contact,
            revenue_share_pct=revenue_share_pct,
            promo_discount_pct=promo_discount_pct,
            secret_token=secrets.token_urlsafe(24),
            notes=notes,
            active=True,
        )
        s.add(p)
        await s.commit()
        await s.refresh(p)
        return p


async def register_commission(payment: Payment, partner_id: int) -> PartnerCommission | None:
    """Регистрирует комиссию по конкретной оплате.

    Идемпотентно: если уже есть строка с этим payment.id — ничего не делает.

    Возвращает созданную (или None если уже была)."""
    async with Session() as s:
        existing = (
            await s.execute(
                select(PartnerCommission).where(PartnerCommission.payment_id == payment.id)
            )
        ).scalar_one_or_none()
        if existing:
            logger.info("Commission for payment.id=%s already exists (id=%s)", payment.id, existing.id)
            return None

        partner = await s.get(Partner, partner_id)
        if not partner or not partner.active:
            logger.warning("Partner id=%s неактивен или не найден — пропускаем комиссию", partner_id)
            return None

        gross = payment.amount_kopecks
        share_pct = partner.revenue_share_pct
        commission = gross * share_pct // 100

        c = PartnerCommission(
            partner_id=partner.id,
            user_id=payment.user_id,
            payment_id=payment.id,
            gross_amount_kopecks=gross,
            commission_kopecks=commission,
            share_pct_snapshot=share_pct,
        )
        s.add(c)
        await s.commit()
        await s.refresh(c)
        logger.info(
            "Commission registered: partner=%s user=%s payment=%s gross=%d commission=%d (%d%%)",
            partner.code, payment.user_id, payment.id, gross, commission, share_pct,
        )
        return c


async def get_partner_summary(partner_id: int) -> dict:
    """Сводка по партнёру: сколько юзеров привёл, оплат, комиссий
    (всего / выплачено / ждёт выплаты)."""
    from sqlalchemy import func as sa_func
    from ..db import User

    async with Session() as s:
        partner = await s.get(Partner, partner_id)
        if not partner:
            return {}
        total_users = (await s.execute(
            select(sa_func.count(User.id)).where(User.partner_id == partner_id)
        )).scalar() or 0
        paying_users = (await s.execute(
            select(sa_func.count(sa_func.distinct(PartnerCommission.user_id)))
            .where(PartnerCommission.partner_id == partner_id)
        )).scalar() or 0
        total_commissions = (await s.execute(
            select(sa_func.coalesce(sa_func.sum(PartnerCommission.commission_kopecks), 0))
            .where(PartnerCommission.partner_id == partner_id)
        )).scalar() or 0
        paid_out = (await s.execute(
            select(sa_func.coalesce(sa_func.sum(PartnerCommission.commission_kopecks), 0))
            .where(
                PartnerCommission.partner_id == partner_id,
                PartnerCommission.paid_out.is_(True),
            )
        )).scalar() or 0
        pending = total_commissions - paid_out
        n_payments = (await s.execute(
            select(sa_func.count(PartnerCommission.id))
            .where(PartnerCommission.partner_id == partner_id)
        )).scalar() or 0

        return {
            "partner": partner,
            "total_users_brought": total_users,
            "paying_users": paying_users,
            "n_payments": n_payments,
            "total_commission_kop": total_commissions,
            "paid_out_kop": paid_out,
            "pending_kop": pending,
        }


async def list_partner_commissions(partner_id: int, limit: int = 200) -> list[PartnerCommission]:
    """Все комиссии партнёра — новые сверху. Для /my_payments."""
    from sqlalchemy import desc as sa_desc
    async with Session() as s:
        return list((
            await s.execute(
                select(PartnerCommission)
                .where(PartnerCommission.partner_id == partner_id)
                .order_by(sa_desc(PartnerCommission.created_at))
                .limit(limit)
            )
        ).scalars().all())


async def attribute_payment_to_partner(payment: Payment, user, bot=None) -> None:
    """Высокоуровневая обёртка: вызывается из ЛЮБОГО места успешной оплаты
    (handler билинга, renewal_worker и т.п.).

    Делает: 1) проверяет что у юзера есть partner_id, 2) идемпотентно создаёт
    запись комиссии, 3) если передали bot и у партнёра есть telegram_id —
    шлёт ему уведомление с цифрами.

    Безопасна при любых ошибках — ловит исключения и логирует, не валит
    основной флоу платежа.
    """
    if not user.partner_id:
        return
    try:
        commission = await register_commission(payment, user.partner_id)
        if not commission:
            return  # уже была зарегистрирована или партнёр выключен

        if not bot:
            return

        # Подтянем партнёра ещё раз — нам нужны его telegram_id и сводка
        summary = await get_partner_summary(user.partner_id)
        partner = summary.get("partner")
        if not partner or not partner.partner_telegram_id:
            return

        rub_now = commission.commission_kopecks / 100
        pending_rub = summary.get("pending_kop", 0) / 100
        try:
            await bot.send_message(
                partner.partner_telegram_id,
                (
                    f"💰 <b>Новая комиссия: {rub_now:,.0f} ₽</b>\n"
                    f"<i>({commission.share_pct_snapshot}% от {commission.gross_amount_kopecks/100:.0f} ₽)</i>\n\n"
                    f"К выплате накопилось: <b>{pending_rub:,.0f} ₽</b>\n"
                    f"Подробная история: /my_payments\n"
                    f"Сводка: /my_stats"
                ),
            )
        except Exception as e:
            logger.warning("Не удалось уведомить партнёра %s: %s", partner.code, e)
    except Exception:
        logger.exception("attribute_payment_to_partner failed for payment_id=%s user_id=%s",
                         getattr(payment, "id", None), getattr(user, "id", None))


async def mark_commissions_paid(
    partner_id: int,
    *,
    method: str,
    reference: str | None = None,
) -> tuple[int, int]:
    """Помечает все pending-комиссии партнёра как выплаченные.

    Возвращает (количество строк, общая сумма в копейках)."""
    import datetime as dt
    from sqlalchemy import update

    async with Session() as s:
        # Сначала найдём, какие будем апдейтить (нужны для подсчёта суммы)
        rows = (
            await s.execute(
                select(PartnerCommission).where(
                    PartnerCommission.partner_id == partner_id,
                    PartnerCommission.paid_out.is_(False),
                )
            )
        ).scalars().all()
        if not rows:
            return 0, 0
        total = sum(r.commission_kopecks for r in rows)
        ids = [r.id for r in rows]
        now = dt.datetime.now(dt.timezone.utc)
        await s.execute(
            update(PartnerCommission)
            .where(PartnerCommission.id.in_(ids))
            .values(paid_out=True, paid_out_at=now, paid_out_method=method, paid_out_reference=reference)
        )
        await s.commit()
        return len(ids), total
