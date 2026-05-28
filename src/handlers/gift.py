"""Подарок другу — пакет «персональная сказка под имя ребёнка близкого человека».
Цена 199 ₽. FSM: имя получателя → пол → личное послание → оплата.

Gift-флоу упрощён в мае 2026: убраны шаги возраста, выбора героя и темы.
Дарителю остаются только три самые личные вещи (имя ребёнка, пол, своё
послание), всё остальное (герой, сюжет, мир) сказочник придумывает сам
через pick_storyteller_variant — так же как в основной сказке.

После оплаты бот:
1. Генерирует сказку с использованием pick_storyteller_variant + personal_note
2. Рисует 3 иллюстрации через Recraft (как в основном флоу)
3. Собирает PDF-книжку
4. Шлёт всё ПОКУПАТЕЛЮ (а не получателю) — покупатель перешлёт другу сам через Telegram

Параметры подарка временно хранятся в module-level dict, потому что между invoice
и payment в FSM может произойти что угодно. На рестарте бот забывает pending gifts —
если такое случится, юзер получит возврат через /support."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from ..config import config
from ..db import Session, Story, User
from ..keyboards import gender_kb, main_menu_kb
from ..services import (
    create_gift_invoice,
    generate_gift_story,
)
from ..services.image import generate_three_illustrations
from ..services.pdf_book import build_story_pdf
from ..states import GiftWizard
from ..utils import (
    genitive,
    normalize_name,
)

logger = logging.getLogger(__name__)
router = Router(name="gift")

# user_id (telegram) → собранные параметры подарка, ждём оплаты
_pending_gifts: dict[int, dict] = {}


def _gift_summary_kb() -> "InlineKeyboardBuilder":
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Оплатить 199 ₽", callback_data="gift:pay")
    kb.button(text="◀ Отмена", callback_data="gift:cancel")
    kb.adjust(1)
    return kb.as_markup()


# ─────────────────── Старт FSM ───────────────────

@router.callback_query(F.data == "gift:new")
async def cb_gift_new(call: CallbackQuery, state: FSMContext) -> None:
    """Старт gift-флоу: спрашиваем пол ПЕРЕД именем, потом имя, потом
    личное послание."""
    await call.message.edit_text(
        "🎁 <b>Сказка в подарок</b>\n\n"
        "Сложим персональную сказку — с именем ребёнка и Вашим тёплым "
        "посланием. Стоимость — <b>199 ₽</b>.\n\n"
        "Кому делаем подарок — мальчику или девочке?",
        reply_markup=gender_kb(),
    )
    await state.set_state(GiftWizard.waiting_recipient_gender)
    await call.answer()


@router.callback_query(GiftWizard.waiting_recipient_gender, F.data.startswith("gender:"))
async def cb_recipient_gender(call: CallbackQuery, state: FSMContext) -> None:
    gender = call.data.split(":", 1)[1]
    if gender not in ("male", "female"):
        await call.answer("Выберите мальчик или девочка", show_alert=True)
        return
    await state.update_data(recipient_gender=gender)
    # Дальше спрашиваем имя
    who = "мальчика" if gender == "male" else "девочку"
    await call.message.edit_text(
        f"🕯 А как зовут {who}? Напишите имя."
    )
    await state.set_state(GiftWizard.waiting_recipient_name)
    await call.answer()


@router.message(GiftWizard.waiting_recipient_name)
async def m_recipient_name(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()[:32]
    if not raw or not raw.replace("-", "").replace(" ", "").isalpha():
        await message.answer("🕯 Только буквы, до тридцати двух знаков. Попробуйте ещё раз.")
        return
    name = normalize_name(raw)
    await state.update_data(recipient_name=name)
    await message.answer(
        "🕯 Напишите личное послание от Вас — что-то тёплое или "
        "поздравление. Сказочник вплетёт его смысл в сказку, не "
        "цитируя дословно.\n\n"
        "<i>Например: «С днём рождения, моя любимая принцесса! "
        "Желаю смелости и волшебных открытий».</i>"
    )
    await state.set_state(GiftWizard.waiting_personal_note)


@router.message(GiftWizard.waiting_personal_note)
async def m_personal_note(message: Message, state: FSMContext) -> None:
    note = (message.text or "").strip()[:500]
    if len(note) < 5:
        await message.answer("🕯 Послание чуть длиннее, пожалуйста — хотя бы пять знаков.")
        return
    await state.update_data(personal_note=note)
    data = await state.get_data()

    gender_label = "мальчик" if data.get("recipient_gender") == "male" else "девочка"
    summary = (
        "🎁 <b>Подарок готов к оплате</b>\n\n"
        f"Для кого: <b>{data['recipient_name']}</b> ({gender_label})\n"
        f"Послание от Вас:\n<i>{note}</i>\n\n"
        "После оплаты сказочник сложит персональную PDF-книжечку с "
        "тремя иллюстрациями и пришлёт её сюда — Вы перешлёте близким.\n\n"
        "Стоимость: <b>199 ₽</b>."
    )
    await message.answer(summary, reply_markup=_gift_summary_kb())


@router.callback_query(GiftWizard.waiting_personal_note, F.data == "gift:cancel")
@router.callback_query(F.data == "gift:cancel")
async def cb_gift_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    _pending_gifts.pop(call.from_user.id, None)
    await call.message.edit_text("🕯 Хорошо, отменили. Возвращаемся в меню.", reply_markup=main_menu_kb())
    await call.answer()


@router.callback_query(F.data == "gift:pay")
async def cb_gift_pay(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    required = {"recipient_name", "recipient_gender", "personal_note"}
    if not required.issubset(data.keys()):
        await call.answer("Не хватает данных подарка — начните, пожалуйста, заново.", show_alert=True)
        return

    # Сохраняем параметры для использования после оплаты
    _pending_gifts[call.from_user.id] = {
        "recipient_name": data["recipient_name"],
        "recipient_gender": data["recipient_gender"],
        "personal_note": data["personal_note"],
    }
    await state.clear()
    await call.answer()
    await create_gift_invoice(
        bot, call.message.chat.id, call.from_user.id,
        recipient=data["recipient_name"],
    )


# ─────────────────── После успешной оплаты ───────────────────
# Вызывается из billing.py на_paid когда kind == PaymentKind.gift

async def complete_gift_after_payment(bot: Bot, telegram_user_id: int) -> None:
    """Достаём сохранённые параметры, генерируем сказку, шлём покупателю."""
    params = _pending_gifts.pop(telegram_user_id, None)
    if not params:
        logger.warning("No pending gift params for user=%s", telegram_user_id)
        try:
            await bot.send_message(
                telegram_user_id,
                "🕯 Не нашли данные Вашего подарка. Это редкая ошибка — напишите в /support, оформим вручную.",
            )
        except Exception:
            pass
        return

    chat_id = telegram_user_id
    try:
        await bot.send_chat_action(chat_id, "typing")
        text, story_title, scenes = await generate_gift_story(
            recipient_name=params["recipient_name"],
            recipient_gender=params["recipient_gender"],
            personal_note=params["personal_note"],
        )
    except Exception as e:
        logger.exception("Подарочная сказка не сгенерилась: %s", e)
        await bot.send_message(
            chat_id,
            "🕯 У сказочника что-то не сложилось. Напишите в /support — вернём деньги или сделаем вручную.",
        )
        return

    # Параллельно с подготовкой шапки и текста — генерим 3 иллюстрации.
    # Передаём scenes из самого сказочника (он их выдаёт в блоке ---SCENES---
    # по нашей инструкции в _SCENE_BLOCK_INSTRUCTIONS). hero и theme_key
    # пустые — generate_three_illustrations не использует их семантически,
    # стиль определяется натренированным STYLE_ID.
    illustrations_task = asyncio.create_task(
        generate_three_illustrations(
            "", "",
            scenes=scenes,
            child_name=params["recipient_name"],
        )
    )

    # Шапка для пересылки близким
    await bot.send_message(
        chat_id,
        f"🎁 <b>Сказка в подарок для {params['recipient_name']} готова</b>\n\n"
        "Перешлите всё ниже близким — текст, картинку и PDF-книжку — обычной пересылкой в Telegram.",
    )

    # Текст — отдаём БЕЗ эмо-маркеров (они были для озвучки в старом флоу,
    # теперь не используются, но llm.py их всё ещё может оставлять в выводе).
    from .story import _split_for_telegram
    from ..utils import strip_emo_markers
    display_text = strip_emo_markers(text)

    # Ждём картинки
    try:
        illustrations = await illustrations_task
    except Exception as e:
        logger.warning("Подарочные иллюстрации не вышли: %s", e)
        illustrations = {"opening": None, "climax": None, "ending": None}

    cover_path = illustrations.get("opening")

    # 1) Обложка как превью — сразу видно "что это"
    if cover_path and cover_path.exists():
        try:
            await bot.send_photo(chat_id, FSInputFile(str(cover_path)))
        except Exception as e:
            logger.warning("Подарочная обложка не отправилась: %s", e)

    # 2) Текст частями
    for part in _split_for_telegram(display_text):
        await bot.send_message(chat_id, part)

    # 3) PDF-книжка. Используем название из самой сказки если есть,
    # иначе fallback на «Сказка для X».
    if story_title:
        book_title = story_title
    else:
        book_title = f"Сказка для {genitive(params['recipient_name'])}"
    try:
        pdf_path = build_story_pdf(
            title=book_title,
            subtitle="",  # подзаголовок раньше брался из THEME_CHOICES, темы больше нет
            text=display_text,
            cover_image=cover_path,
            climax_image=illustrations.get("climax"),
            ending_image=illustrations.get("ending"),
        )
        if pdf_path and pdf_path.exists():
            await bot.send_document(
                chat_id,
                FSInputFile(str(pdf_path), filename=f"{book_title}.pdf"),
                caption=f"📖 Сказка для {genitive(params['recipient_name'])}",
            )
    except Exception as e:
        logger.exception("Подарочный PDF не собрался: %s", e)

    # Сохраняем в архив покупателя как gift. Колонки hero/theme в Story
    # nullable — оставляем None для новых подарков (старые записи NULL не
    # тревожим). child_age оставляем 6 как дефолт — колонка NOT NULL.
    async with Session() as s:
        buyer = (await s.execute(select(User).where(User.telegram_id == telegram_user_id))).scalar_one_or_none()
        if buyer:
            s.add(Story(
                user_id=buyer.id,
                child_name=params["recipient_name"],
                child_age=6,
                hero="",
                theme="",
                length="medium",
                # В БД сохраняем уже без маркеров (для /library)
                text=display_text,
                # Кладём обложку, чтобы share-флоу мог переотправить картинку.
                image_path=str(cover_path) if cover_path else None,
                is_paid_quality=True,
                is_gift=True,
                gift_recipient_name=params["recipient_name"],
            ))
            await s.commit()

    await bot.send_message(
        chat_id,
        "Готово. Спасибо, что выбрали «Сказку» как подарок 💛",
        reply_markup=main_menu_kb(),
    )


# ─────────────────── Поделиться существующей сказкой ───────────────────
# Эта кнопка появляется после каждой сказки — позволяет переслать её
# близким без отдельной оплаты (это уже своя сгенерированная сказка).

@router.callback_query(F.data.startswith("gift:share:"))
async def cb_gift_share(call: CallbackQuery) -> None:
    try:
        story_id = int(call.data.split(":")[2])
    except (ValueError, IndexError):
        await call.answer("Битая ссылка", show_alert=True)
        return

    async with Session() as s:
        st = await s.get(Story, story_id)
        if not st:
            await call.answer("Сказка не найдена", show_alert=True)
            return
        u = (await s.execute(select(User).where(User.telegram_id == call.from_user.id))).scalar_one_or_none()
        if not u or st.user_id != u.id:
            await call.answer("Эта сказка не из Вашего архива.", show_alert=True)
            return

    await call.answer()
    await call.message.answer(
        f"🎁 <b>Перешлите эту сказку близким</b>\n\n"
        "Удерживайте каждое сообщение ниже → «Переслать» → выберите чат. "
        "Картинка и текст — всё в одной сказке."
    )
    if st.image_path:
        try:
            await call.message.answer_photo(FSInputFile(st.image_path))
        except Exception:
            pass
    await call.message.answer(st.text)
