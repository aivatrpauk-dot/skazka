"""FAQ — снимает 80% типовых вопросов без участия владельца."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..keyboards import main_menu_kb

router = Router(name="faq")

FAQ_TOPICS: dict[str, tuple[str, str]] = {
    "cancel": (
        "Как отменить подписку",
        "Команда /cancel_subscription. После отмены доступ к платным функциям остаётся "
        "до конца оплаченного периода, дальше списаний не будет.",
    ),
    "refund": (
        "Не понравилось — как вернуть деньги",
        "Команда /refund в первые 7 дней — возвращаем без вопросов. После 7 дней напишите /support.",
    ),
    "child": (
        "Поменять имя ребёнка",
        "При создании следующей сказки нажмите «Назад» на шаге выбора героя и введите новое имя — "
        "оно станет основным.",
    ),
    "audio": (
        "Не воспроизводится аудио",
        "Скачайте mp3-файл из чата и откройте плеером телефона. На Android иногда нужно открывать "
        "через «Файлы», а не из самого Telegram.",
    ),
    "share": (
        "Можно ли поделиться сказкой с близкими",
        "Конечно. После каждой сказки есть кнопка «Подарить сказку другу» — "
        "она пойдёт прямо в чат получателю.",
    ),
    "data": (
        "Что с моими данными",
        "Имена детей хранятся только в нашей базе для генерации сказок. Мы не делимся ими ни с кем.\n\n"
        "Команда /delete_me — стирает все персональные данные (имя ребёнка, никнейм, способ оплаты).\n"
        "Для полного удаления записи по 152-ФЗ — напишите /support, сделаем в течение 24 часов.",
    ),
    "languages": (
        "Когда будет английский",
        "Английская версия в работе. Если нужно прямо сейчас — напишите в /support, "
        "включим Вас в список бета-тестеров.",
    ),
    "support": (
        "Связь с поддержкой",
        "Команда /support, или просто напишите в этот чат, что не так — читаем все "
        "обращения в течение 24 часов.",
    ),
}


def _faq_kb():
    kb = InlineKeyboardBuilder()
    for key, (title, _) in FAQ_TOPICS.items():
        kb.button(text=title, callback_data=f"faq:show:{key}")
    kb.button(text="◀ В меню", callback_data="menu:main")
    kb.adjust(1)
    return kb.as_markup()


@router.callback_query(F.data == "faq:open")
async def cb_open(call: CallbackQuery) -> None:
    await call.message.edit_text(
        "🌙 Чем могу помочь? Выберите, что Вас интересует:",
        reply_markup=_faq_kb(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("faq:show:"))
async def cb_show(call: CallbackQuery) -> None:
    key = call.data.split(":")[2]
    title, body = FAQ_TOPICS.get(key, ("Не найдено", "Напишите в /support."))
    await call.message.edit_text(f"<b>{title}</b>\n\n{body}", reply_markup=_faq_kb())
    await call.answer()


@router.message(Command("delete_me"))
async def cmd_delete(message: Message) -> None:
    """Для обычных юзеров — soft-delete (защита от абуза с бесплатным триалом).
    Для админов из ADMIN_IDS — hard-delete: полностью удаляем запись, чтобы можно
    было пройти воронку заново как новый юзер (для тестов и QA)."""
    import secrets
    from sqlalchemy import select
    from ..config import config
    from ..db import Session, SubscriptionStatus, User

    is_admin = message.from_user.id in config.admin_ids

    async with Session() as s:
        u = (await s.execute(
            select(User).where(User.telegram_id == message.from_user.id)
        )).scalar_one_or_none()

        if not u:
            await message.answer(
                "🕯 Записи о Вас в нашем замке не нашлось. Начните с /start — "
                "и я Вас встречу."
            )
            return

        if is_admin:
            # Полный сброс — каскадом удалит и stories, и payments, и referrals
            await s.delete(u)
            await s.commit()
        else:
            u.username = None
            u.first_name = None
            u.language_code = None
            u.child_name = None
            u.child_age = None
            u.yookassa_payment_method_id = None
            u.subscription_status = SubscriptionStatus.cancelled
            u.referral_code = "deleted_" + secrets.token_urlsafe(8)
            await s.commit()

    if is_admin:
        await message.answer(
            "✅ [admin] Полный сброс: запись, сказки и платежи удалены.\n"
            "Жмите /start — заведётесь как новый юзер, можно прогнать демо заново."
        )
    else:
        await message.answer(
            "🕯 Все Ваши личные данные нами стёрты: имя малыша, "
            "имя пользователя, способ оплаты, реферальная ссылка — всё "
            "вычеркнуто из наших книг.\n\n"
            "Запись о Telegram-аккаунте сохраняется только во избежание "
            "повторного использования бесплатной первой сказки. Если нужно "
            "полное удаление по 152-ФЗ — напишите в /support, всё устроим "
            "в течение суток."
        )
