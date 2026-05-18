"""Поддержка через форвардинг.
Юзер шлёт свободный текст → бот пересылает админу с заголовком.
Админ отвечает через бот командой /reply <user_id> <текст>.
Личный аккаунт админа юзеры не видят."""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from ..config import config

logger = logging.getLogger(__name__)
router = Router(name="support")


@router.message(Command("support"))
async def cmd_support(message: Message) -> None:
    await message.answer(
        "Напишите прямо в этот чат — что случилось.\n"
        "Поддержка прочитает в течение 24 часов и ответит сюда же.\n\n"
        "Если ответ есть в FAQ — будет быстрее: откройте «Помощь» в меню."
    )


@router.message(Command("reply"))
async def cmd_reply(message: Message, command: CommandObject, bot: Bot) -> None:
    """Только для админа: ответить юзеру через бот.
    Использование: /reply 123456789 текст ответа"""
    if message.from_user.id not in config.admin_ids:
        return
    raw = command.args or ""
    parts = raw.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /reply &lt;telegram_id&gt; &lt;текст&gt;")
        return
    try:
        target_id = int(parts[0])
    except ValueError:
        await message.answer("Первым аргументом должен быть числовой telegram_id")
        return
    text = parts[1]
    try:
        await bot.send_message(target_id, f"💬 <b>Ответ поддержки:</b>\n\n{text}")
        await message.answer(f"✅ Отправлено пользователю {target_id}")
    except Exception as e:
        await message.answer(f"❌ Не удалось: {e}")


# Перехватчик свободного текста — форвард админу.
# ВАЖНО: этот router должен быть подключён ПОСЛЕДНИМ в setup_routers(),
# иначе он перехватит FSM-сообщения мастера сказки.
@router.message(F.text & ~F.text.startswith("/"))
async def forward_to_admin(message: Message, state: FSMContext, bot: Bot) -> None:
    # Юзер в активной FSM (имя ребёнка, кастомный герой и т.п.) — не трогаем
    if await state.get_state():
        return
    # Сам админ что-то пишет в бот — не форвардим себе же
    if message.from_user.id in config.admin_ids:
        return
    if not config.admin_ids:
        logger.warning("Нет ADMIN_IDS в .env — сообщение поддержки потеряно")
        return

    user = message.from_user
    username = f"@{user.username}" if user.username else "—"
    header = (
        f"📩 <b>Сообщение в поддержку</b>\n"
        f"От: {user.first_name or '—'} ({username})\n"
        f"ID: <code>{user.id}</code>\n"
        f"Ответить: <code>/reply {user.id} ваш текст</code>"
    )
    for admin_id in config.admin_ids:
        try:
            await bot.send_message(admin_id, header)
            await bot.forward_message(
                chat_id=admin_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
        except Exception as e:
            logger.exception("Не удалось переслать админу %s: %s", admin_id, e)

    await message.answer(
        "Спасибо, поддержка прочитает в течение 24 часов и ответит сюда же. "
        "Если срочно — посмотрите сначала /faq, там есть готовые ответы на типовые вопросы."
    )
