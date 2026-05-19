"""Партнёрские команды (для самих партнёров, не для админа).

Партнёр (блогер, у которого мы покупаем рекламу с revenue share) приходит
в бота и логинится своим секретным токеном:

    /partner_login <secret_token>

После этого бот запоминает его telegram_id, и доступны команды:

    /my_stats     — сводка: сколько привёл, сколько начислено, к выплате
    /my_payments  — последние 50 строк его ledger'а с детализацией по каждой
                    оплате (payment_id, дата, сумма, начислено, выплачено)
    /my_link      — его deep-link для размещения

Это даёт партнёру прозрачность — он в любой момент видит свои цифры в реалтайме
и может cross-check'нуть их по своему знанию аудитории. Если ему нужна выгрузка
для бухгалтерии — он пишет админу, тот делает /export_csv commissions.

Безопасность:
- secret_token генерируется через secrets.token_urlsafe(24) — 192 бита энтропии,
  непрактично подобрать.
- После логина токен НЕ удаляется (можно перелогиниться) и НЕ показывается
  обратно. Если телеграм-аккаунт партнёра скомпрометировали — админ генерирует
  новый Partner (со старым кодом не работает) и инвалидирует старого через
  /partner_disable (см. ниже)."""
from __future__ import annotations

import datetime as dt
import logging
import secrets

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy import select

from ..db import Partner, Session, SubscriptionStatus, User
from ..services import (
    find_partner_by_telegram_id,
    find_partner_by_token,
    get_partner_summary,
    list_partner_commissions,
)

logger = logging.getLogger(__name__)
router = Router(name="partner")


def _kop_to_rub(k: int | None) -> str:
    if not k:
        return "0 ₽"
    return f"{k/100:,.0f} ₽".replace(",", " ")


@router.message(Command("partner_login"))
async def cmd_login(message: Message, command: CommandObject) -> None:
    """`/partner_login <token>` — связываем telegram_id юзера с partner.id."""
    token = (command.args or "").strip()
    if not token:
        await message.answer(
            "Использование: <code>/partner_login &lt;ваш_токен&gt;</code>\n\n"
            "Токен дал админ при создании партнёрства. Он одноразовый только в "
            "том смысле, что после привязки телеграма повторный логин не нужен."
        )
        return
    partner = await find_partner_by_token(token)
    if not partner:
        await message.answer(
            "❌ Токен не подошёл. Перепроверьте — он должен быть длинным, "
            "примерно 32 символа букв/цифр. Если что-то не так, напишите админу."
        )
        return

    async with Session() as s:
        p = await s.get(Partner, partner.id)
        p.partner_telegram_id = message.from_user.id

        # Активируем партнёру безлимитный доступ к боту (10 лет).
        # Это обещание из маркетингового оффера: "бот вам в личное пользование".
        u = (await s.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )).scalar_one_or_none()
        if not u:
            # /partner_login пришёл как самое первое сообщение боту (до /start) —
            # создаём User-запись inline, чтобы дальше зачислить безлимит.
            u = User(
                telegram_id=message.from_user.id,
                username=message.from_user.username,
                first_name=message.from_user.first_name,
                language_code=message.from_user.language_code,
                referral_code=secrets.token_urlsafe(8),
            )
            s.add(u)
            await s.flush()

        far_future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=365 * 10)
        # Не сокращаем активную подписку, если она уже дальше — берём максимум
        current_until = u.subscription_until
        if not current_until or current_until < far_future:
            u.subscription_until = far_future
        u.subscription_status = SubscriptionStatus.active
        granted_access = True

        await s.commit()

    me = await message.bot.get_me()
    deep_link = f"https://t.me/{me.username}?start={p.code}"

    access_line = ""
    if granted_access:
        access_line = (
            "🎁 <b>Вам активирована безлимитная подписка на 10 лет</b> — "
            "пользуйтесь ботом сами и тестируйте на ребёнке.\n\n"
        )

    await message.answer(
        f"✅ Добро пожаловать, <b>{p.name}</b>!\n\n"
        f"{access_line}"
        f"Ваш партнёрский код: <code>{p.code}</code>\n"
        f"Шеринг: <b>{p.revenue_share_pct}%</b> от каждой оплаты пожизненно.\n"
        f"Скидка для аудитории: {p.promo_discount_pct}%\n\n"
        f"<b>Ваша ссылка для размещения:</b>\n"
        f"<code>{deep_link}</code>\n\n"
        f"<b>Команды:</b>\n"
        f"/my_stats — сводка\n"
        f"/my_payments — все ваши начисления (immutable ledger)\n"
        f"/my_link — ваша ссылка\n\n"
        f"Каждый раз, когда юзер по вашей ссылке оплатит — придёт уведомление "
        f"со свежей цифрой комиссии. Выплаты — по запросу через админа."
    )


@router.message(Command("my_stats"))
async def cmd_my_stats(message: Message) -> None:
    partner = await find_partner_by_telegram_id(message.from_user.id)
    if not partner:
        return  # не партнёр — игнорим, чтоб не светить наличие команды

    summary = await get_partner_summary(partner.id)
    me = await message.bot.get_me()
    deep_link = f"https://t.me/{me.username}?start={partner.code}"

    await message.answer(
        f"📊 <b>{partner.name}</b> — ваши цифры\n\n"
        f"Привели юзеров: <b>{summary['total_users_brought']}</b>\n"
        f"Из них платных: <b>{summary['paying_users']}</b>\n"
        f"Конверсия в платных: "
        f"{(summary['paying_users']/summary['total_users_brought']*100 if summary['total_users_brought'] else 0):.1f}%\n\n"
        f"Всего оплат от вашей аудитории: {summary['n_payments']}\n"
        f"Всего начислено комиссии: <b>{_kop_to_rub(summary['total_commission_kop'])}</b>\n"
        f"  ↳ Уже выплачено: {_kop_to_rub(summary['paid_out_kop'])}\n"
        f"  ↳ <b>К выплате: {_kop_to_rub(summary['pending_kop'])}</b>\n\n"
        f"Ваш % шеринга: <b>{partner.revenue_share_pct}%</b> от каждой оплаты\n\n"
        f"Все детали по каждой оплате: /my_payments\n"
        f"Ссылка: <code>{deep_link}</code>"
    )


@router.message(Command("my_payments"))
async def cmd_my_payments(message: Message) -> None:
    """Последние 50 строк ledger'а с детализацией по каждой оплате.

    Каждая строка содержит payment_id — партнёр может сравнить с тем, что
    он видит у себя (если ведёт учёт). Сумма комиссии = (валовая сумма)
    × (% на момент платежа). Это immutable: после создания строка не
    меняется (кроме статуса выплаты)."""
    partner = await find_partner_by_telegram_id(message.from_user.id)
    if not partner:
        return

    commissions = await list_partner_commissions(partner.id, limit=50)
    if not commissions:
        await message.answer(
            f"<b>{partner.name}</b> — пока 0 операций.\n\n"
            f"Когда юзер по вашей ссылке (?start={partner.code}) оплатит подписку — "
            f"строка появится здесь сразу."
        )
        return

    pending_total = sum(c.commission_kopecks for c in commissions if not c.paid_out)
    paid_total = sum(c.commission_kopecks for c in commissions if c.paid_out)

    lines = [
        f"<b>📜 Ledger — {partner.name}</b>",
        f"Показано последних {len(commissions)} операций\n",
        f"К выплате: <b>{_kop_to_rub(pending_total)}</b>",
        f"Выплачено (в этой выборке): {_kop_to_rub(paid_total)}\n",
        f"<i>Сумма = (валовая оплата) × {partner.revenue_share_pct}%. "
        f"Каждая строка содержит payment_id из ЮKassa — это уникальный референс, "
        f"его можно cross-check'нуть.</i>\n",
    ]
    for c in commissions[:30]:  # в TG лимит 4096 символов — показываем 30
        date = c.created_at.strftime("%d.%m %H:%M")
        gross_rub = c.gross_amount_kopecks / 100
        com_rub = c.commission_kopecks / 100
        status = "✅" if c.paid_out else "⏳"
        lines.append(
            f"{status} <code>{date}</code> · pid={c.payment_id} · "
            f"{gross_rub:.0f}₽ × {c.share_pct_snapshot}% = <b>{com_rub:.0f}₽</b>"
        )
    if len(commissions) > 30:
        lines.append(f"\n<i>... и ещё {len(commissions) - 30} строк раньше</i>")
    await message.answer("\n".join(lines))


@router.message(Command("my_link"))
async def cmd_my_link(message: Message) -> None:
    partner = await find_partner_by_telegram_id(message.from_user.id)
    if not partner:
        return
    me = await message.bot.get_me()
    await message.answer(
        f"<b>Ваша партнёрская ссылка:</b>\n\n"
        f"<code>https://t.me/{me.username}?start={partner.code}</code>\n\n"
        f"Все юзеры, перешедшие по ней, навсегда привязаны к вам. "
        f"Любая их оплата сейчас и в будущем = ваши {partner.revenue_share_pct}%."
    )
