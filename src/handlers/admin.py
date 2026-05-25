"""Админ-команды.

Все команды защищены проверкой `config.admin_ids`. Сторонний юзер их не увидит:
просто будет «команда не найдена» (обрабатывается catch-all support handler).

Команды:
  /stats                      — мини-дашборд (юзеры, выручка, источники, конверсии)
  /partners                   — список всех партнёров с pending-комиссиями
  /partner_add CODE NAME ...  — создать партнёра (помощник в команде)
  /partner_stats CODE         — детали по партнёру
  /partner_payout CODE METHOD [REF]  — пометить все pending как выплаченные
  /partner_link CODE          — собрать deep-link для размещения
  /export_csv WHAT            — выгрузить CSV (users | payments | commissions)
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message
from sqlalchemy import desc, func, select

from ..config import config
from ..db import (
    Feedback,
    Partner,
    PartnerCommission,
    Payment,
    PaymentKind,
    Session,
    Story,
    SubscriptionStatus,
    User,
)
from ..services import (
    create_partner,
    find_partner_by_code,
    get_partner_summary,
    list_partner_commissions,
    mark_commissions_paid,
)


# Предзаданный пул кандидатов для bulk-создания (команда /seed_partners).
# Формат: (code, display_name, greeting_name, platform, profile_url, followers_label, niche)
# greeting_name — то, с чего начинается DM ("Здравствуйте, {greeting_name}!").
# Если имени нет (корп-аккаунт типа Karpikau Family) — оставь пустую строку,
# тогда DM начнётся просто "Здравствуйте!".
SEED_PARTNERS: list[tuple[str, str, str, str, str, str, str]] = [
    ("pati", "Pati", "Pati", "Instagram", "https://www.instagram.com/mom_pati/",
     "~167к", "детский сон"),
    ("surkova", "Лариса Суркова", "Лариса", "Instagram",
     "https://www.instagram.com/larangsovet/", "~2М",
     "психолог, мама 7 детей, семейная психология"),
    ("mamasaays", "MamaSaays", "", "Instagram",
     "https://www.instagram.com/mamasaays/", "ниша",
     "частный консультант по детскому сну"),
    ("svefly", "Света Гончарова", "Света", "Telegram", "https://t.me/sve_flymama",
     "средний", "детская психология"),
    ("storymom", "Алёна (Story Mother)", "Алёна", "Telegram", "https://t.me/storymother",
     "5–15к", "сказки на ночь"),
    ("vashumat", "Настя (Вашу мать!)", "Настя", "Telegram", "https://t.me/vashumat",
     "премиум", "иронично про материнство"),
    ("karpikau", "Karpikau Family", "", "Instagram",
     "https://www.instagram.com/karpikau/", "~1М", "семейный блог"),
    ("masanya", "Оксана (Материнство и детство)", "Оксана", "Instagram",
     "https://www.instagram.com/oksana_masanya/", "~109к", "материнство"),
    ("novitskay", "Юлия Новицкая", "Юлия", "Instagram",
     "https://www.instagram.com/novitskay_yuliya/", "~151к", "многодетная мама"),
    ("parental", "Психолог детских душ", "", "Telegram", "https://t.me/parental_control",
     "средний", "детская психология"),
]

logger = logging.getLogger(__name__)
router = Router(name="admin")


def _is_admin(user_id: int) -> bool:
    return user_id in config.admin_ids


def _kop_to_rub(k: int | None) -> str:
    if not k:
        return "0 ₽"
    return f"{k/100:,.0f} ₽".replace(",", " ")


# ============================================================================
# /stats — мини-дашборд
# ============================================================================

@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return

    now = dt.datetime.now(dt.timezone.utc)
    d1 = now - dt.timedelta(days=1)
    d7 = now - dt.timedelta(days=7)
    d30 = now - dt.timedelta(days=30)

    async with Session() as s:
        # юзеры
        total_users = (await s.execute(select(func.count(User.id)))).scalar() or 0
        new_24h = (await s.execute(
            select(func.count(User.id)).where(User.created_at >= d1)
        )).scalar() or 0
        new_7d = (await s.execute(
            select(func.count(User.id)).where(User.created_at >= d7)
        )).scalar() or 0
        new_30d = (await s.execute(
            select(func.count(User.id)).where(User.created_at >= d30)
        )).scalar() or 0
        active_7d = (await s.execute(
            select(func.count(User.id)).where(User.last_active_at >= d7)
        )).scalar() or 0

        # подписки
        active_subs = (await s.execute(
            select(func.count(User.id)).where(
                User.subscription_status == SubscriptionStatus.active
            )
        )).scalar() or 0
        paid_once = (await s.execute(
            select(func.count(func.distinct(Payment.user_id))).where(
                Payment.succeeded.is_(True),
                Payment.kind.in_([PaymentKind.subscription, PaymentKind.renewal]),
            )
        )).scalar() or 0
        cancelled = (await s.execute(
            select(func.count(User.id)).where(
                User.subscription_status == SubscriptionStatus.cancelled
            )
        )).scalar() or 0

        # сказки
        stories_today = (await s.execute(
            select(func.count(Story.id)).where(Story.created_at >= d1)
        )).scalar() or 0
        stories_7d = (await s.execute(
            select(func.count(Story.id)).where(Story.created_at >= d7)
        )).scalar() or 0
        stories_total = (await s.execute(select(func.count(Story.id)))).scalar() or 0

        # выручка
        rev_24h = (await s.execute(
            select(func.coalesce(func.sum(Payment.amount_kopecks), 0)).where(
                Payment.succeeded.is_(True), Payment.created_at >= d1
            )
        )).scalar() or 0
        rev_7d = (await s.execute(
            select(func.coalesce(func.sum(Payment.amount_kopecks), 0)).where(
                Payment.succeeded.is_(True), Payment.created_at >= d7
            )
        )).scalar() or 0
        rev_30d = (await s.execute(
            select(func.coalesce(func.sum(Payment.amount_kopecks), 0)).where(
                Payment.succeeded.is_(True), Payment.created_at >= d30
            )
        )).scalar() or 0
        rev_total = (await s.execute(
            select(func.coalesce(func.sum(Payment.amount_kopecks), 0)).where(
                Payment.succeeded.is_(True)
            )
        )).scalar() or 0

        # источники
        source_rows = (await s.execute(
            select(
                User.utm_source,
                func.count(User.id),
                func.count(func.distinct(Payment.user_id)),
            )
            .outerjoin(Payment, (Payment.user_id == User.id) & (Payment.succeeded.is_(True)))
            .group_by(User.utm_source)
            .order_by(desc(func.count(User.id)))
            .limit(15)
        )).all()

        # партнёрские pending-комиссии
        pending_total = (await s.execute(
            select(func.coalesce(func.sum(PartnerCommission.commission_kopecks), 0))
            .where(PartnerCommission.paid_out.is_(False))
        )).scalar() or 0
        n_partners = (await s.execute(
            select(func.count(Partner.id)).where(Partner.active.is_(True))
        )).scalar() or 0

    conv_paid_total = (paid_once / total_users * 100) if total_users else 0
    avg_stories = (stories_total / total_users) if total_users else 0

    # рендерим
    src_block = ""
    for src, n_users, n_paying in source_rows:
        label = src or "organic"
        cv = (n_paying / n_users * 100) if n_users else 0
        src_block += f"  <code>{label:<14}</code> {n_users:>4} → {n_paying} платн. ({cv:.0f}%)\n"
    if not src_block:
        src_block = "  <i>пока ничего</i>\n"

    text = (
        f"📊 <b>Сказка — Dashboard</b>\n"
        f"<i>{now.strftime('%Y-%m-%d %H:%M UTC')}</i>\n\n"
        f"<b>👥 Пользователи</b>\n"
        f"  Всего:        <b>{total_users}</b>\n"
        f"  За 24ч:       +{new_24h}\n"
        f"  За 7д:        +{new_7d}\n"
        f"  За 30д:       +{new_30d}\n"
        f"  Активных 7д:  {active_7d}\n\n"
        f"<b>💸 Подписки</b>\n"
        f"  Активных:     <b>{active_subs}</b>\n"
        f"  Платили хоть раз: {paid_once}\n"
        f"  Отменили:     {cancelled}\n"
        f"  Конверсия free→paid: {conv_paid_total:.1f}%\n\n"
        f"<b>📚 Сказки</b>\n"
        f"  Сегодня:      {stories_today}\n"
        f"  За 7д:        {stories_7d}\n"
        f"  Всего:        {stories_total}\n"
        f"  Средн./юзер:  {avg_stories:.1f}\n\n"
        f"<b>💰 Выручка</b>\n"
        f"  24ч:          <b>{_kop_to_rub(rev_24h)}</b>\n"
        f"  7д:           {_kop_to_rub(rev_7d)}\n"
        f"  30д:          {_kop_to_rub(rev_30d)}\n"
        f"  Всего:        <b>{_kop_to_rub(rev_total)}</b>\n\n"
        f"<b>📈 Источники (UTM)</b>\n"
        f"{src_block}\n"
        f"<b>🤝 Партнёры</b>\n"
        f"  Активных:     {n_partners}\n"
        f"  К выплате:    <b>{_kop_to_rub(pending_total)}</b>\n"
        f"  Команды: /partners /partner_payout"
    )
    await message.answer(text)


# ============================================================================
# /partners — список всех
# ============================================================================

@router.message(Command("partners"))
async def cmd_partners(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    async with Session() as s:
        partners = list((
            await s.execute(select(Partner).order_by(Partner.created_at))
        ).scalars().all())
    if not partners:
        await message.answer(
            "Партнёров пока нет.\n\n"
            "Чтобы добавить: <code>/partner_add CODE НАЗВАНИЕ [ШЕРИНГ%] [СКИДКА%]</code>\n"
            "Пример: <code>/partner_add pati Pati Instagram 30 50</code>"
        )
        return
    # Считаем сводку по логин-статусам для шапки
    logged_in_count = sum(1 for p in partners if p.partner_telegram_id)
    not_logged_in_count = len(partners) - logged_in_count

    lines = [
        f"<b>🤝 Партнёры ({len(partners)})</b>",
        f"🔐 Залогинились: <b>{logged_in_count}</b>  •  "
        f"⏳ Ждём логина: <b>{not_logged_in_count}</b>\n",
    ]
    for p in partners:
        summary = await get_partner_summary(p.id)
        pending = summary.get("pending_kop", 0)
        total = summary.get("total_commission_kop", 0)
        users = summary.get("total_users_brought", 0)
        paying = summary.get("paying_users", 0)
        active_mark = "✅" if p.active else "⏸"
        login_mark = "🔐" if p.partner_telegram_id else "⏳"
        lines.append(
            f"{active_mark} {login_mark} <b>{p.name}</b> "
            f"(<code>{p.code}</code>) — {p.revenue_share_pct}%\n"
            f"  Привёл: {users} (из них платных: {paying})\n"
            f"  Начислено: {_kop_to_rub(total)} / к выплате: <b>{_kop_to_rub(pending)}</b>\n"
            f"  Подробно: <code>/partner_stats {p.code}</code>\n"
        )

    # Telegram режет сообщения > 4096 символов; для 13 партнёров может не влезть.
    # Бьём по ~3500 символов.
    full = "\n".join(lines)
    LIMIT = 3500
    if len(full) <= LIMIT:
        await message.answer(full)
        return

    buf: list[str] = []
    size = 0
    for line in lines:
        block = line + "\n"
        if size + len(block) > LIMIT and buf:
            await message.answer("".join(buf))
            buf = [block]
            size = len(block)
        else:
            buf.append(block)
            size += len(block)
    if buf:
        await message.answer("".join(buf))


# ============================================================================
# /partner_add — создать
# ============================================================================

@router.message(Command("partner_add"))
async def cmd_partner_add(message: Message, command: CommandObject) -> None:
    if not _is_admin(message.from_user.id):
        return
    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer(
            "Использование:\n"
            "<code>/partner_add CODE НАЗВАНИЕ [SHARE%] [DISCOUNT%]</code>\n\n"
            "Пример:\n"
            "<code>/partner_add pati Pati_Instagram 30 50</code>\n\n"
            "По умолчанию: 30% шеринг, 50% скидка."
        )
        return
    code = args[0]
    name = args[1]
    share = int(args[2]) if len(args) > 2 else 30
    discount = int(args[3]) if len(args) > 3 else 50
    try:
        p = await create_partner(
            code=code,
            name=name,
            revenue_share_pct=share,
            promo_discount_pct=discount,
        )
    except ValueError as e:
        await message.answer(f"❌ {e}")
        return

    me = await message.bot.get_me()
    deep_link = f"https://t.me/{me.username}?start={p.code}"
    await message.answer(
        f"✅ Партнёр создан\n\n"
        f"<b>{p.name}</b> (<code>{p.code}</code>)\n"
        f"Шеринг: <b>{p.revenue_share_pct}%</b> от валового\n"
        f"Скидка для аудитории: {p.promo_discount_pct}%\n\n"
        f"<b>Ссылка для размещения:</b>\n"
        f"<code>{deep_link}</code>\n\n"
        f"<b>Секретный токен партнёра (выдать лично):</b>\n"
        f"<code>{p.secret_token}</code>\n\n"
        f"Партнёр у себя в боте делает:\n"
        f"<code>/partner_login {p.secret_token}</code>\n"
        f"После этого ему доступны /my_stats и /my_payments."
    )


# ============================================================================
# /partner_stats CODE — детали по партнёру
# ============================================================================

@router.message(Command("partner_stats"))
async def cmd_partner_stats(message: Message, command: CommandObject) -> None:
    if not _is_admin(message.from_user.id):
        return
    code = (command.args or "").strip().lower()
    if not code:
        await message.answer("Использование: <code>/partner_stats CODE</code>")
        return
    async with Session() as s:
        p = (await s.execute(
            select(Partner).where(Partner.code == code)
        )).scalar_one_or_none()
    if not p:
        await message.answer(f"Партнёр <code>{code}</code> не найден.")
        return

    summary = await get_partner_summary(p.id)
    me = await message.bot.get_me()
    deep_link = f"https://t.me/{me.username}?start={p.code}"

    await message.answer(
        f"<b>{p.name}</b> (<code>{p.code}</code>)\n"
        f"{'✅ активен' if p.active else '⏸ отключён'}\n\n"
        f"Шеринг: <b>{p.revenue_share_pct}%</b> / Скидка: {p.promo_discount_pct}%\n"
        f"Контакт: {p.contact or '—'}\n"
        f"Связан с Telegram: "
        f"{'да (id=' + str(p.partner_telegram_id) + ')' if p.partner_telegram_id else 'нет'}\n\n"
        f"<b>Метрики</b>\n"
        f"  Привёл юзеров: {summary['total_users_brought']}\n"
        f"  Из них платных: {summary['paying_users']}\n"
        f"  Всего оплат: {summary['n_payments']}\n"
        f"  Всего начислено: <b>{_kop_to_rub(summary['total_commission_kop'])}</b>\n"
        f"  Выплачено: {_kop_to_rub(summary['paid_out_kop'])}\n"
        f"  К выплате: <b>{_kop_to_rub(summary['pending_kop'])}</b>\n\n"
        f"<b>Ссылка:</b>\n<code>{deep_link}</code>\n\n"
        f"<b>Выплатить:</b>\n<code>/partner_payout {p.code} СБП TX-123</code>"
    )


# ============================================================================
# /partner_link CODE — собрать ссылку
# ============================================================================

@router.message(Command("partner_link"))
async def cmd_partner_link(message: Message, command: CommandObject) -> None:
    if not _is_admin(message.from_user.id):
        return
    code = (command.args or "").strip().lower()
    if not code:
        await message.answer("Использование: <code>/partner_link CODE</code>")
        return
    async with Session() as s:
        p = (await s.execute(
            select(Partner).where(Partner.code == code)
        )).scalar_one_or_none()
    if not p:
        await message.answer(f"Партнёр <code>{code}</code> не найден.")
        return
    me = await message.bot.get_me()
    await message.answer(
        f"<b>{p.name}</b> — ссылка для размещения:\n\n"
        f"<code>https://t.me/{me.username}?start={p.code}</code>"
    )


# ============================================================================
# /partner_payout CODE METHOD [REF] — пометить как выплаченные
# ============================================================================

@router.message(Command("partner_payout"))
async def cmd_partner_payout(message: Message, command: CommandObject) -> None:
    if not _is_admin(message.from_user.id):
        return
    args = (command.args or "").split(maxsplit=2)
    if len(args) < 2:
        await message.answer(
            "Использование: <code>/partner_payout CODE МЕТОД [REF]</code>\n\n"
            "Пример: <code>/partner_payout pati СБП TX-PAYOUT-001</code>"
        )
        return
    code = args[0].strip().lower()
    method = args[1].strip()
    reference = args[2].strip() if len(args) > 2 else None

    async with Session() as s:
        p = (await s.execute(
            select(Partner).where(Partner.code == code)
        )).scalar_one_or_none()
    if not p:
        await message.answer(f"Партнёр <code>{code}</code> не найден.")
        return

    n, total_kop = await mark_commissions_paid(p.id, method=method, reference=reference)
    if n == 0:
        await message.answer("К выплате 0 строк — ничего не сделано.")
        return
    await message.answer(
        f"✅ Помечено как выплачено: <b>{n}</b> строк на <b>{_kop_to_rub(total_kop)}</b>\n"
        f"Метод: {method}\n"
        f"Референс: {reference or '—'}\n\n"
        f"Партнёр в /my_payments увидит свои строки уже отмеченными как выплаченные."
    )

    # Уведомим партнёра
    if p.partner_telegram_id:
        try:
            await message.bot.send_message(
                p.partner_telegram_id,
                f"💵 Вам выплачено <b>{_kop_to_rub(total_kop)}</b> ({n} операций)\n"
                f"Способ: {method}\n"
                f"Референс: {reference or '—'}\n\n"
                f"Все детали в /my_payments — там строки уже помечены ✅"
            )
        except Exception:
            pass


# ============================================================================
# /seed_partners — создать всех 10 кандидатов разом + выдать готовые DM
# ============================================================================

def _dm_for_partner(
    *,
    greeting_name: str,
    niche: str,
    code: str,
    token: str,
    bot_username: str,
) -> str:
    """Готовый DM-текст для конкретного партнёра. Готов к копи-пасту.
    Нейтрально-вежливый тон (на «вы»), без bullet-листов и панибратства."""
    deep_link = f"https://t.me/{bot_username}?start={code}"
    greeting = f"Здравствуйте, {greeting_name}!" if greeting_name else "Здравствуйте!"
    return (
        f"{greeting}\n\n"
        f"Я Денис, делаю Telegram-бот «Сказка» (@{bot_username}) — он за минуту пишет "
        f"персональную сказку на ночь под имя ребёнка, с озвучкой и обложкой. У моей "
        f"дочки укладывание раньше занимало час, теперь минут 10–15, поэтому решил "
        f"собрать в продукт.\n\n"
        f"Заметил ваш блог — у нас сильно пересекается аудитория ({niche}). Хочу "
        f"предложить партнёрство: вам бот в личное пользование бесплатно + 30% от "
        f"каждой оплаты по вашей ссылке, пожизненно (включая все продления). Без "
        f"обязательств и сроков — упоминаете когда удобно, если зайдёт. Аудитории "
        f"по вашей ссылке автоматически даётся −50% на первый месяц.\n\n"
        f"Ваша ссылка для размещения: {deep_link}\n\n"
        f"Чтобы посмотреть как работает — зайдите в @{bot_username}, первые 3 сказки "
        f"бесплатно. Чтобы привязать партнёрский кабинет и видеть свои начисления "
        f"в реальном времени (каждая оплата отдельной строкой с уникальным "
        f"payment_id из ЮKassa) — отправьте боту команду:\n\n"
        f"/partner_login {token}\n\n"
        f"После этого станут доступны /my_stats, /my_payments и /my_link.\n\n"
        f"Готов ответить на любые вопросы или созвониться на 15 минут — покажу всё "
        f"вживую.\n\n"
        f"— Денис, @Denis_1_0"
    )


@router.message(Command("seed_partners"))
async def cmd_seed_partners(message: Message) -> None:
    """Bulk-создаёт всех 10 предзаданных партнёров и отправляет одно сообщение
    на партнёра с готовым DM-текстом + ссылкой на профиль. Идемпотентно:
    если партнёр с таким code уже есть — переиспользуем его (не падаем)."""
    if not _is_admin(message.from_user.id):
        return

    me = await message.bot.get_me()
    bot_username = me.username

    created = 0
    reused = 0

    await message.answer(
        f"🔧 Создаю {len(SEED_PARTNERS)} партнёров… "
        f"После этого пришлю {len(SEED_PARTNERS)} отдельных сообщений — каждое содержит "
        f"готовый текст DM, ссылку на профиль и токен для копипасты."
    )

    for code, name, greeting, platform, profile_url, followers, niche in SEED_PARTNERS:
        existing = await find_partner_by_code(code)
        if existing:
            partner = existing
            reused += 1
        else:
            try:
                partner = await create_partner(
                    code=code,
                    name=name,
                    revenue_share_pct=30,
                    promo_discount_pct=50,
                    contact=profile_url,
                    notes=f"{platform} · {followers} · {niche}",
                )
                created += 1
            except Exception as e:
                await message.answer(f"❌ {code}: {e}")
                continue

        dm = _dm_for_partner(
            greeting_name=greeting,
            niche=niche,
            code=partner.code,
            token=partner.secret_token,
            bot_username=bot_username,
        )

        # Заголовок — отдельным сообщением. По нему НЕ копируем — оно служебное.
        header = (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>{partner.name}</b>\n"
            f"<i>{platform} · {followers} · {niche}</i>\n"
            f"🔗 Профиль: {profile_url}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Готовый DM — следующее сообщение. Долгое нажатие → «Копировать»:"
        )
        await message.answer(header)
        # Сам DM — обычным сообщением (без <pre>), чтобы не было «синей полоски»
        # и текст выглядел как живое сообщение, а не вырезка. Долгое нажатие на
        # сообщение в TG копирует его целиком.
        await message.answer(dm, disable_web_page_preview=True)

    await message.answer(
        f"✅ Готово.\n\n"
        f"Создано новых: <b>{created}</b>\n"
        f"Переиспользовано существующих: <b>{reused}</b>\n\n"
        f"Алгоритм работы:\n"
        f"1. Открой профиль из сообщения (ссылка кликабельная)\n"
        f"2. Если это Instagram — открывай DM, если Telegram — пиши прямо там\n"
        f"3. Скопируй блок DM из <code>&lt;pre&gt;</code>-сообщения, вставь в чат, отправь\n"
        f"4. Через 7 дней — /partners покажет, кто авторизовался"
    )


# ============================================================================
# /export_csv WHAT — выгрузка для аудита
# ============================================================================

@router.message(Command("export_csv"))
async def cmd_export_csv(message: Message, command: CommandObject) -> None:
    if not _is_admin(message.from_user.id):
        return
    what = (command.args or "").strip().lower()
    if what not in {"users", "payments", "commissions"}:
        await message.answer(
            "Что выгрузить?\n"
            "<code>/export_csv users</code>\n"
            "<code>/export_csv payments</code>\n"
            "<code>/export_csv commissions</code>"
        )
        return

    buf = io.StringIO()
    writer = csv.writer(buf)

    async with Session() as s:
        if what == "users":
            rows = (await s.execute(
                select(User.id, User.telegram_id, User.username, User.first_name,
                       User.partner_id, User.utm_source, User.subscription_status,
                       User.subscription_until, User.created_at)
                .order_by(User.id)
            )).all()
            writer.writerow([
                "id", "telegram_id", "username", "first_name", "partner_id",
                "utm_source", "subscription_status", "subscription_until", "created_at",
            ])
            for r in rows:
                writer.writerow(r)
        elif what == "payments":
            rows = (await s.execute(
                select(Payment.id, Payment.user_id, Payment.kind, Payment.amount_kopecks,
                       Payment.succeeded, Payment.yookassa_payment_id,
                       Payment.provider_payment_charge_id, Payment.created_at)
                .order_by(Payment.id)
            )).all()
            writer.writerow([
                "id", "user_id", "kind", "amount_kopecks", "succeeded",
                "yookassa_payment_id", "provider_charge_id", "created_at",
            ])
            for r in rows:
                writer.writerow(r)
        else:  # commissions
            rows = (await s.execute(
                select(PartnerCommission.id, PartnerCommission.partner_id,
                       PartnerCommission.user_id, PartnerCommission.payment_id,
                       PartnerCommission.gross_amount_kopecks,
                       PartnerCommission.commission_kopecks,
                       PartnerCommission.share_pct_snapshot,
                       PartnerCommission.paid_out, PartnerCommission.paid_out_at,
                       PartnerCommission.paid_out_method, PartnerCommission.paid_out_reference,
                       PartnerCommission.created_at)
                .order_by(PartnerCommission.id)
            )).all()
            writer.writerow([
                "id", "partner_id", "user_id", "payment_id",
                "gross_amount_kopecks", "commission_kopecks", "share_pct_snapshot",
                "paid_out", "paid_out_at", "paid_out_method", "paid_out_reference",
                "created_at",
            ])
            for r in rows:
                writer.writerow(r)

    data = buf.getvalue().encode("utf-8-sig")  # BOM для Excel
    file = BufferedInputFile(
        data,
        filename=f"skazka_{what}_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d_%H%M')}.csv",
    )
    await message.answer_document(file, caption=f"CSV: {what} ({len(data)} bytes)")


# ============================================================================
# /generate_ambient <N> — генерация пула фоновых треков через Suno
# /list_ambient        — посмотреть какие треки в пуле
# /clear_ambient       — удалить весь пул (для regen с нуля)
# ============================================================================

@router.message(Command("generate_ambient"))
async def cmd_generate_ambient(message: Message, command: CommandObject) -> None:
    """Генерирует N фоновых инструментальных треков через Suno V5 (kie.ai)
    и сохраняет в cache/ambient/. Каждый запрос Suno даёт 2 клипа,
    то есть реальный размер пула ~= N × 2.

    Пример: /generate_ambient 15 → ~30 треков в пуле, ~165 ₽ разово."""
    if not _is_admin(message.from_user.id):
        return
    raw = (command.args or "").strip()
    if not raw or not raw.isdigit():
        await message.answer(
            "Использование: <code>/generate_ambient N</code>\n\n"
            "Например: <code>/generate_ambient 15</code> — Suno сгенерит 15 запросов "
            "по ~2 трека = ~30 mp3 в пуле. Стоимость ~165 ₽ разово.\n\n"
            "Каждый трек ~2-3 минуты, инструментальная колыбельная "
            "(piano/music box/harp). Сохраняется в cache/ambient/, "
            "используется случайно при микшировании сказок."
        )
        return
    n = int(raw)
    if n < 1 or n > 50:
        await message.answer("N должно быть от 1 до 50.")
        return

    from ..services.bg_music import generate_bg_pool

    await message.answer(
        f"🎵 Запускаю генерацию {n} запросов Suno V5 "
        f"(~{n*2} треков в пуле).\n\n"
        f"Это займёт ~{n} минут (последовательно, чтоб не упереться в "
        f"rate-limit kie.ai). Можно не ждать — сообщу когда закончу."
    )

    try:
        succeeded, failed = await generate_bg_pool(n)
        await message.answer(
            f"✅ Готово.\n"
            f"Успешных запросов: <b>{succeeded}</b>\n"
            f"Ошибок: <b>{failed}</b>\n\n"
            f"Посмотреть пул: /list_ambient"
        )
    except Exception as e:
        logger.exception("generate_bg_pool failed")
        await message.answer(f"❌ Ошибка: {e}")


@router.message(Command("list_ambient"))
async def cmd_list_ambient(message: Message) -> None:
    """Список всех файлов в cache/ambient/."""
    if not _is_admin(message.from_user.id):
        return
    from ..services.bg_music import list_bg_tracks

    tracks = list_bg_tracks()
    if not tracks:
        await message.answer(
            "Пул пустой. Сгенерируй через "
            "<code>/generate_ambient 15</code>."
        )
        return
    lines = [f"🎵 <b>В пуле {len(tracks)} треков:</b>\n"]
    total_size = 0
    for p in tracks[:50]:
        size_kb = p.stat().st_size / 1024
        total_size += size_kb
        lines.append(f"  • {p.name} ({size_kb:.0f} КБ)")
    if len(tracks) > 50:
        lines.append(f"  … и ещё {len(tracks) - 50}")
    lines.append(f"\n<b>Общий размер:</b> {total_size/1024:.1f} МБ")
    await message.answer("\n".join(lines))


@router.message(Command("clear_ambient"))
async def cmd_clear_ambient(message: Message) -> None:
    """Удалить все треки из пула. Используется перед regen."""
    if not _is_admin(message.from_user.id):
        return
    from ..services.bg_music import clear_bg_pool

    deleted = clear_bg_pool()
    await message.answer(
        f"🗑 Удалено треков: <b>{deleted}</b>\n\n"
        f"Теперь сгенерируй новые через "
        f"<code>/generate_ambient 15</code>."
    )


# ============================================================================
# /feedback — посмотреть критику от юзеров
# ============================================================================

@router.message(Command("feedback"))
async def cmd_feedback(message: Message, command: CommandObject) -> None:
    """Показать последние отзывы-критику.
    Использование:
      /feedback              — последние 20
      /feedback all          — все
      /feedback <telegram_id> — все от конкретного юзера
    """
    if not _is_admin(message.from_user.id):
        return

    args = (command.args or "").strip()
    limit = 20
    user_filter_tg_id: int | None = None

    if args == "all":
        limit = 500
    elif args.isdigit():
        user_filter_tg_id = int(args)
        limit = 100

    async with Session() as s:
        q = select(Feedback, User).join(User, User.id == Feedback.user_id)
        if user_filter_tg_id:
            q = q.where(User.telegram_id == user_filter_tg_id)
        q = q.order_by(desc(Feedback.created_at)).limit(limit)
        rows = (await s.execute(q)).all()

    if not rows:
        await message.answer(
            "Пока ни одной критики не получено.\n\n"
            "Юзерам предлагается дать критику после первой демо-сказки. "
            "Подожди немного, либо проверь что фича задеплоилась."
        )
        return

    total = len(rows)
    header = f"📬 <b>Критика от юзеров ({total})</b>\n"
    if user_filter_tg_id:
        header = f"📬 <b>Критика от юзера {user_filter_tg_id} ({total})</b>\n"

    lines = [header]
    for fb, u in rows:
        username = f"@{u.username}" if u.username else "(без юзернейма)"
        when = fb.created_at.strftime("%d.%m %H:%M")
        text_short = fb.text[:300] + ("…" if len(fb.text) > 300 else "")
        lines.append(
            f"\n— <b>{when}</b> · {username} (tg:<code>{u.telegram_id}</code>) · "
            f"ребёнок {u.child_name or '?'} {u.child_age or '?'}л\n"
            f"<i>{text_short}</i>"
        )

    full = "\n".join(lines)
    # Telegram лимит 4096 символов — разбиваем
    LIMIT = 3800
    if len(full) <= LIMIT:
        await message.answer(full)
        return

    buf: list[str] = []
    size = 0
    for line in lines:
        block = line + "\n"
        if size + len(block) > LIMIT and buf:
            await message.answer("".join(buf))
            buf = [block]
            size = len(block)
        else:
            buf.append(block)
            size += len(block)
    if buf:
        await message.answer("".join(buf))


# ============================================================================
# /give_stories <telegram_id> <N>  — выдать бонусные сказки юзеру
# ============================================================================

@router.message(Command("give_stories"))
async def cmd_give_stories(message: Message, command: CommandObject) -> None:
    """Выдать юзеру N бонусных сказок (без оплаты, без daily-лимита).

    Использование:
      /give_stories 1234567890 5    — выдать 5 бонусных сказок юзеру с этим tg_id
      /give_stories 1234567890      — выдать 1 бонусную сказку (по умолчанию)

    Бонусные сказки списываются ПЕРВЫМИ при следующих генерациях. У них нет
    daily-лимита 1/сутки — юзер может сделать несколько подряд, пока бонусы
    не кончатся.
    """
    if not _is_admin(message.from_user.id):
        return

    args = (command.args or "").split()
    if not args or not args[0].isdigit():
        await message.answer(
            "Использование: <code>/give_stories TG_ID [N]</code>\n\n"
            "Пример: <code>/give_stories 1275991975 5</code>\n"
            "Если N не указано — выдаётся 1 сказка."
        )
        return

    target_tg_id = int(args[0])
    count = 1
    if len(args) > 1 and args[1].isdigit():
        count = max(1, min(100, int(args[1])))  # лимит 1..100, чтоб не вкатать миллион

    async with Session() as s:
        user = (await s.execute(
            select(User).where(User.telegram_id == target_tg_id)
        )).scalar_one_or_none()
        if not user:
            await message.answer(
                f"Юзер с tg_id <code>{target_tg_id}</code> не найден в БД. "
                f"Он должен сначала хотя бы раз нажать /start у бота."
            )
            return
        before = user.bonus_stories or 0
        user.bonus_stories = before + count
        username = f"@{user.username}" if user.username else "(без юзернейма)"
        child = user.child_name or "?"
        await s.commit()

    await message.answer(
        f"✅ Выдал <b>{count}</b> бонусных сказок\n"
        f"Юзер: {username} · tg:<code>{target_tg_id}</code>\n"
        f"Ребёнок: {child}\n"
        f"Было: {before} → стало: {before + count}"
    )


# ============================================================================
# /reset_user <telegram_id>  — сбросить ротацию и daily-лимит юзера
# ============================================================================

@router.message(Command("reset_user"))
async def cmd_reset_user(message: Message, command: CommandObject) -> None:
    """Сбросить юзера: daily-лимит, ротацию архитектур, last_story_*.
    Использовать когда нужно дать юзеру возможность сгенерить сегодня снова
    (например, прошлая сказка не понравилась и хочется попробовать ещё раз).

    Что СБРАСЫВАЕТСЯ:
      - last_story_at  → юзер может сгенерить сегодня снова (daily-лимит сброшен)
      - last_story_group / architecture / humor_register / category → ротация
        стартует с чистого листа (модель свободно выбирает архитектуру и жанр)

    Что НЕ трогаем:
      - bonus_stories / free_stories_used / pack_stories_remaining /
        single_stories_remaining / subscription_status — счётчики и оплаты целы
      - child_name / child_age — данные ребёнка не теряются
      - Все прошлые сказки в /library остаются на месте

    Использование:
      /reset_user 1234567890
    """
    if not _is_admin(message.from_user.id):
        return

    args = (command.args or "").strip()
    if not args.isdigit():
        await message.answer(
            "Использование: <code>/reset_user TG_ID</code>\n\n"
            "Пример: <code>/reset_user 1275991975</code>\n\n"
            "Сбрасывает daily-лимит и ротацию архитектур у юзера. Юзер сможет "
            "сегодня сгенерить ещё одну сказку, и она пойдёт «с нуля» по выбору "
            "жанра и архитектуры."
        )
        return

    target_tg_id = int(args)

    async with Session() as s:
        user = (await s.execute(
            select(User).where(User.telegram_id == target_tg_id)
        )).scalar_one_or_none()
        if not user:
            await message.answer(
                f"Юзер с tg_id <code>{target_tg_id}</code> не найден в БД."
            )
            return

        was = (
            f"last_story_at={user.last_story_at}, "
            f"category={user.last_story_category}, "
            f"group={user.last_story_group}, "
            f"arch={user.last_story_architecture}, "
            f"humor={user.last_story_humor_register}"
        )

        user.last_story_at = None
        user.last_story_category = None
        user.last_story_group = None
        user.last_story_architecture = None
        user.last_story_humor_register = None

        username = f"@{user.username}" if user.username else "(без юзернейма)"
        child = user.child_name or "?"
        await s.commit()

    logger.info("Admin reset user tg=%s: %s", target_tg_id, was)

    await message.answer(
        f"✅ Сбросил юзера\n"
        f"Юзер: {username} · tg:<code>{target_tg_id}</code>\n"
        f"Ребёнок: {child}\n\n"
        f"Сброшено: daily-лимит, last_story_category, group, architecture, humor.\n"
        f"Юзер может сгенерить сегодня ещё раз, ротация — с чистого листа.\n\n"
        f"Счётчики (bonus/pack/subscription) и /library — целы."
    )


# ============================================================================
# /admin  — справочник всех админских команд
# ============================================================================

@router.message(Command("admin"))
async def cmd_admin_help(message: Message) -> None:
    """Справочник всех админских команд бота."""
    if not _is_admin(message.from_user.id):
        return

    text = (
        "🔧 <b>Админские команды</b>\n\n"
        "<b>📊 Аналитика</b>\n"
        "<code>/stats</code> — мини-дашборд: юзеры, выручка, конверсии, источники\n"
        "<code>/export_csv users|payments|commissions</code> — выгрузка CSV\n\n"
        "<b>👤 Юзер: посмотреть и сбросить</b>\n"
        "<code>/user_info TG_ID</code> — детали юзера (счётчики, сказки, подписка)\n"
        "<code>/reset_user TG_ID</code> — сбросить daily-лимит и ротацию (счётчики целы)\n"
        "<code>/clear_stories TG_ID</code> — удалить ВСЕ сказки юзера из /library\n"
        "<code>/give_stories TG_ID [N]</code> — выдать N бонусных сказок\n\n"
        "<b>💌 Обратная связь</b>\n"
        "<code>/feedback</code> — последние 20 критик от юзеров\n"
        "<code>/feedback all</code> — все критики\n"
        "<code>/feedback TG_ID</code> — все от конкретного юзера\n\n"
        "<b>🤝 Партнёрка</b>\n"
        "<code>/partners</code> — список партнёров и pending-комиссии\n"
        "<code>/partner_add CODE NAME ПРОЦЕНТ TG_ID</code> — создать партнёра\n"
        "<code>/partner_stats CODE</code> — детали по партнёру\n"
        "<code>/partner_link CODE</code> — deep-link для размещения\n"
        "<code>/partner_payout CODE METHOD [REF]</code> — пометить pending как выплаченные\n"
        "<code>/seed_partners</code> — посеять тестовых партнёров\n\n"
        "<b>🎵 Фоновая музыка (legacy, музыку выпилили)</b>\n"
        "<code>/generate_ambient N</code>, <code>/list_ambient</code>, <code>/clear_ambient</code> — "
        "оставлены на случай возврата к озвучке. В новом flow PDF без музыки.\n\n"
        "<i>Все команды видишь только ты (по списку ADMIN_IDS в .env). "
        "Обычный юзер их не видит и не может выполнить.</i>"
    )
    await message.answer(text)


# ============================================================================
# /user_info TG_ID  — детали юзера
# ============================================================================

@router.message(Command("user_info"))
async def cmd_user_info(message: Message, command: CommandObject) -> None:
    """Показать детальное состояние юзера: счётчики, подписку, последние
    сказки, ротацию архитектур и т.д. Полезно перед /reset_user или
    /give_stories, чтобы понять что у него и в каком состоянии."""
    if not _is_admin(message.from_user.id):
        return

    args = (command.args or "").strip()
    if not args.isdigit():
        await message.answer(
            "Использование: <code>/user_info TG_ID</code>\n\n"
            "Пример: <code>/user_info 1275991975</code>"
        )
        return

    target_tg_id = int(args)

    async with Session() as s:
        u = (await s.execute(
            select(User).where(User.telegram_id == target_tg_id)
        )).scalar_one_or_none()
        if not u:
            await message.answer(
                f"Юзер с tg_id <code>{target_tg_id}</code> не найден в БД."
            )
            return

        # Последние 5 сказок
        stories = (await s.execute(
            select(Story.id, Story.child_name, Story.created_at)
            .where(Story.user_id == u.id)
            .order_by(desc(Story.created_at))
            .limit(5)
        )).all()

        # Уникальные имена детей
        names = (await s.execute(
            select(Story.child_name, func.count(Story.id).label("cnt"))
            .where(Story.user_id == u.id)
            .group_by(Story.child_name)
            .order_by(desc("cnt"))
        )).all()

    username = f"@{u.username}" if u.username else "(без юзернейма)"
    sub_until = u.subscription_until.strftime("%d.%m.%Y") if u.subscription_until else "—"
    last_at = u.last_story_at.strftime("%d.%m %H:%M") if u.last_story_at else "—"

    lines = [
        f"👤 <b>Юзер tg:<code>{u.telegram_id}</code></b>\n",
        f"Username: {username}",
        f"Имя: {u.first_name or '—'}",
        f"Зарегистрирован: {u.created_at.strftime('%d.%m.%Y')}",
        f"Последняя активность: {u.last_active_at.strftime('%d.%m %H:%M')}",
        f"",
        f"<b>Ребёнок (последний выбранный):</b>",
        f"  Имя: {u.child_name or '—'}, возраст: {u.child_age or '—'}",
        f"",
        f"<b>Счётчики сказок:</b>",
        f"  free_stories_used: {u.free_stories_used or 0} / лимит {config.free_story_limit}",
        f"  bonus_stories: {u.bonus_stories or 0}",
        f"  single_stories_remaining: {u.single_stories_remaining or 0}",
        f"  pack_stories_remaining: {u.pack_stories_remaining or 0}",
        f"  subscription: {u.subscription_status.value if u.subscription_status else '—'} до {sub_until}",
        f"  last_story_at: {last_at}",
        f"",
        f"<b>Ротация / альтернация (последняя сказка):</b>",
        f"  категория: {u.last_story_category or '—'}",
        f"  группа: {u.last_story_group or '—'}",
        f"  архитектура: {u.last_story_architecture or '—'}",
        f"  регистр юмора: {u.last_story_humor_register or '—'}",
    ]

    if names:
        lines.append("")
        lines.append("<b>Дети (по сказкам):</b>")
        for name, cnt in names:
            lines.append(f"  {name}: {cnt} сказок")

    if stories:
        lines.append("")
        lines.append("<b>Последние 5 сказок:</b>")
        for sid, child, created in stories:
            lines.append(f"  #{sid} · {child} · {created.strftime('%d.%m %H:%M')}")

    await message.answer("\n".join(lines))


# ============================================================================
# /clear_stories TG_ID  — удалить ВСЕ сказки юзера
# ============================================================================

@router.message(Command("clear_stories"))
async def cmd_clear_stories(message: Message, command: CommandObject) -> None:
    """Удалить ВСЕ записи Story для юзера (история сказок в /library обнулится).

    НЕ ТРОГАЕТ:
      - сам User (имя ребёнка, счётчики, подписка)
      - платежи
      - партнёрские атрибуции

    Это «полная очистка библиотеки». Бэкап делается автоматически в БД
    (см. backup service, дамп каждые 6 часов).

    Использование:
      /clear_stories 1275991975
    """
    if not _is_admin(message.from_user.id):
        return

    args = (command.args or "").strip()
    if not args.isdigit():
        await message.answer(
            "Использование: <code>/clear_stories TG_ID</code>\n\n"
            "Удалит все сказки юзера из /library. Счётчики и подписка целы.\n"
            "Бэкап БД делается автоматически каждые 6 часов."
        )
        return

    target_tg_id = int(args)

    async with Session() as s:
        u = (await s.execute(
            select(User).where(User.telegram_id == target_tg_id)
        )).scalar_one_or_none()
        if not u:
            await message.answer(
                f"Юзер с tg_id <code>{target_tg_id}</code> не найден."
            )
            return

        count_q = select(func.count(Story.id)).where(Story.user_id == u.id)
        count_before = (await s.execute(count_q)).scalar() or 0

        if count_before == 0:
            await message.answer(
                f"У юзера {u.first_name or target_tg_id} нет сказок в БД. "
                f"Очищать нечего."
            )
            return

        # Удаляем все Story для этого юзера
        from sqlalchemy import delete
        await s.execute(delete(Story).where(Story.user_id == u.id))
        await s.commit()

        username = f"@{u.username}" if u.username else "(без юзернейма)"
        child = u.child_name or "?"

    logger.warning(
        "Admin %s cleared %d stories for user tg=%s",
        message.from_user.id, count_before, target_tg_id,
    )

    await message.answer(
        f"✅ Удалил <b>{count_before}</b> сказок\n"
        f"Юзер: {username} · tg:<code>{target_tg_id}</code>\n"
        f"Ребёнок: {child}\n\n"
        f"Счётчики, подписка и платежи целы. Бэкап БД лежит в /backups/."
    )
