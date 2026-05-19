"""/legal — отображение юр.документов в боте.

Три документа лежат в `src/legal/*.md`. По нажатию кнопки — бот шлёт полный
текст соответствующего файла. Тексты намеренно держим в файлах, а не в коде,
чтобы их легко было обновлять при редакции (просто заменить .md и
пересобрать контейнер).

Дополнительно в этом же модуле — callback `tos:accept` для записи факта
согласия в `user.tos_accepted_at`. Используется в paywall перед оплатой
подписки (см. handlers/billing.py).
"""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from ..db import Session, User

logger = logging.getLogger(__name__)
router = Router(name="legal")


# Папка `src/legal/`, рядом с `src/handlers/`. Файлы маркдаун — в чате
# показываем как plain text (без HTML-парсинга, т.к. документы длинные
# и Telegram может не справиться с экзотической разметкой).
_LEGAL_DIR = Path(__file__).resolve().parent.parent / "legal"

LEGAL_DOCS: dict[str, tuple[str, str]] = {
    "privacy": ("Политика обработки персональных данных", "privacy_policy.md"),
    "offer":   ("Публичная оферта на услуги",              "public_offer.md"),
    "consent": ("Согласие на обработку (152-ФЗ)",          "data_consent.md"),
}


def _legal_menu_kb():
    kb = InlineKeyboardBuilder()
    for key, (title, _) in LEGAL_DOCS.items():
        kb.button(text=f"📄 {title}", callback_data=f"legal:show:{key}")
    kb.adjust(1)
    return kb.as_markup()


def _read_doc(filename: str) -> str:
    path = _LEGAL_DIR / filename
    try:
        return path.read_text(encoding="utf-8")
    except Exception as e:
        logger.exception("Не удалось прочитать %s: %s", path, e)
        return "Документ временно недоступен. Напишите в /support."


async def _send_long(message: Message, text: str) -> None:
    """Telegram режет сообщения длиннее 4096 символов. Бьём по абзацам."""
    LIMIT = 3800
    if len(text) <= LIMIT:
        await message.answer(text, disable_web_page_preview=True)
        return

    buf: list[str] = []
    size = 0
    for paragraph in text.split("\n\n"):
        block = paragraph + "\n\n"
        if size + len(block) > LIMIT and buf:
            await message.answer("".join(buf), disable_web_page_preview=True)
            buf = [block]
            size = len(block)
        else:
            buf.append(block)
            size += len(block)
    if buf:
        await message.answer("".join(buf), disable_web_page_preview=True)


@router.message(Command("legal"))
async def cmd_legal(message: Message) -> None:
    await message.answer(
        "<b>Юридические документы</b>\n\n"
        "Выберите документ — пришлю полный текст. Дата редакции и контакты "
        "указаны в каждом документе. Полные реквизиты ИП (адрес, ОГРНИП, "
        "ИНН) — в выписке ЕГРИП по запросу.\n\n"
        "Эти документы применяются автоматически при использовании бота "
        "и оформлении подписки.",
        reply_markup=_legal_menu_kb(),
    )


@router.callback_query(F.data.startswith("legal:show:"))
async def cb_show(call: CallbackQuery) -> None:
    key = call.data.split(":")[2]
    title, filename = LEGAL_DOCS.get(key, (None, None))
    if not filename:
        await call.answer("Документ не найден", show_alert=True)
        return
    await call.answer()
    text = _read_doc(filename)
    await call.message.answer(f"<b>{title}</b>")
    await _send_long(call.message, text)


@router.callback_query(F.data == "legal:open")
async def cb_open_from_paywall(call: CallbackQuery) -> None:
    """Открыть меню документов из других экранов (например, из paywall)."""
    await call.message.answer(
        "Выберите документ:",
        reply_markup=_legal_menu_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "tos:accept")
async def cb_accept_tos(call: CallbackQuery) -> None:
    """Юзер нажал «✅ Согласен с условиями» перед оплатой.
    Сохраняем tos_accepted_at в БД (audit trail) и продолжаем флоу оплаты."""
    from ..services import create_subscription_invoice

    async with Session() as s:
        u = (
            await s.execute(select(User).where(User.telegram_id == call.from_user.id))
        ).scalar_one_or_none()
        if u and not u.tos_accepted_at:
            u.tos_accepted_at = dt.datetime.now(dt.timezone.utc)
            await s.commit()
            logger.info("TOS accepted by user %s at %s",
                        call.from_user.id, u.tos_accepted_at)

    await call.answer("Спасибо! Открываю оплату…")
    await create_subscription_invoice(call.bot, call.message.chat.id, call.from_user.id)
