"""Подарок другу — пакет «персональная сказка под имя ребёнка близкого человека».
Цена 199 ₽. FSM: имя получателя → возраст → герой → тема → личное послание → оплата.

После оплаты бот:
1. Генерирует сказку с использованием SYSTEM_GIFT_STORYTELLER
2. Озвучивает её через ElevenLabs
3. Рисует обложку через FAL/FusionBrain
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
from ..keyboards import age_kb, hero_kb, main_menu_kb, theme_kb
from ..prompts import THEME_CHOICES
from ..services import (
    create_gift_invoice,
    extract_scene,
    generate_cover,
    generate_gift_story,
    synthesize_speech,
)
from ..states import GiftWizard
from ..utils import (
    accusative,
    dative,
    genitive,
    hero_accusative,
    hero_instrumental,
    instrumental,
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
    await state.set_state(GiftWizard.waiting_recipient_name)
    await call.message.edit_text(
        "🎁 <b>Сказка в подарок</b>\n\n"
        "Сделаем персональную сказку для близкого ребёнка — с его именем, "
        "любимым героем и вашим тёплым посланием. Стоимость — <b>199 ₽</b>.\n\n"
        "Для какого ребёнка делаем подарок? Напишите его имя."
    )
    await call.answer()


@router.message(GiftWizard.waiting_recipient_name)
async def m_recipient_name(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()[:32]
    if not raw or not raw.replace("-", "").replace(" ", "").isalpha():
        await message.answer("Имя только буквами, до 32 символов. Попробуйте ещё раз.")
        return
    name = normalize_name(raw)
    await state.update_data(recipient_name=name)
    await message.answer(f"Сколько лет {dative(name)}?", reply_markup=age_kb())
    await state.set_state(GiftWizard.waiting_recipient_age)


@router.callback_query(GiftWizard.waiting_recipient_age, F.data.startswith("age:"))
async def cb_recipient_age(call: CallbackQuery, state: FSMContext) -> None:
    age = int(call.data.split(":", 1)[1])
    await state.update_data(recipient_age=age)
    data = await state.get_data()
    await call.message.edit_text(
        f"Кто будет главным героем сказки рядом с {instrumental(data['recipient_name'])}?",
        reply_markup=hero_kb(),
    )
    await state.set_state(GiftWizard.waiting_hero)
    await call.answer()


@router.callback_query(GiftWizard.waiting_hero, F.data.startswith("hero:"))
async def cb_gift_hero(call: CallbackQuery, state: FSMContext) -> None:
    raw = call.data.split(":", 1)[1]
    if raw == "custom":
        await call.message.edit_text(
            "Напишите героя. Например: «дельфинёнок», «робот-садовник»."
        )
        await state.update_data(_await_custom_hero=True)
        await call.answer()
        return
    await state.update_data(hero=raw, _await_custom_hero=False)
    await call.message.edit_text("Какая тема сказки?", reply_markup=theme_kb())
    await state.set_state(GiftWizard.waiting_theme)
    await call.answer()


@router.message(GiftWizard.waiting_hero)
async def m_gift_custom_hero(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("_await_custom_hero"):
        return
    hero = (message.text or "").strip()[:48]
    if not hero:
        await message.answer("Напишите имя или название героя, до 48 символов.")
        return
    await state.update_data(hero=hero, _await_custom_hero=False)
    await message.answer("Какая тема сказки?", reply_markup=theme_kb())
    await state.set_state(GiftWizard.waiting_theme)


@router.callback_query(GiftWizard.waiting_theme, F.data.startswith("theme:"))
async def cb_gift_theme(call: CallbackQuery, state: FSMContext) -> None:
    theme_key = call.data.split(":", 1)[1]
    if theme_key not in THEME_CHOICES:
        await call.answer("Тема недоступна")
        return
    await state.update_data(theme_key=theme_key)
    await call.message.edit_text(
        "Напишите личное послание дарителя — что-то тёплое или поздравление. "
        "Бот вплетёт смысл в сказку, не цитируя дословно.\n\n"
        "<i>Например: «С днём рождения, моя любимая принцесса! Желаю смелости и волшебных открытий».</i>"
    )
    await state.set_state(GiftWizard.waiting_personal_note)
    await call.answer()


@router.message(GiftWizard.waiting_personal_note)
async def m_personal_note(message: Message, state: FSMContext) -> None:
    note = (message.text or "").strip()[:500]
    if len(note) < 5:
        await message.answer("Напишите послание подлиннее — хотя бы 5 символов.")
        return
    await state.update_data(personal_note=note)
    data = await state.get_data()

    theme_label = THEME_CHOICES[data["theme_key"]][0]
    summary = (
        "🎁 <b>Подарок готов к оплате</b>\n\n"
        f"Для кого: <b>{data['recipient_name']}</b> ({data['recipient_age']} лет)\n"
        f"Главный герой: <b>{data['hero']}</b>\n"
        f"Тема: <b>{theme_label}</b>\n"
        f"Послание от вас:\n<i>{note}</i>\n\n"
        "После оплаты бот сгенерирует персональную сказку с озвучкой и обложкой "
        "и пришлёт её сюда — вы перешлёте близким.\n\n"
        "Стоимость: <b>199 ₽</b>."
    )
    await message.answer(summary, reply_markup=_gift_summary_kb())


@router.callback_query(GiftWizard.waiting_personal_note, F.data == "gift:cancel")
@router.callback_query(F.data == "gift:cancel")
async def cb_gift_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    _pending_gifts.pop(call.from_user.id, None)
    await call.message.edit_text("Хорошо, отменил. Возвращаемся в меню.", reply_markup=main_menu_kb())
    await call.answer()


@router.callback_query(F.data == "gift:pay")
async def cb_gift_pay(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    required = {"recipient_name", "recipient_age", "hero", "theme_key", "personal_note"}
    if not required.issubset(data.keys()):
        await call.answer("Параметры не заполнены, начните заново", show_alert=True)
        return

    # Сохраняем параметры для использования после оплаты
    _pending_gifts[call.from_user.id] = {
        "recipient_name": data["recipient_name"],
        "recipient_age": data["recipient_age"],
        "hero": data["hero"],
        "theme_key": data["theme_key"],
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
                "Не нашёл данные вашего подарка. Это редкая ошибка — напишите /support, оформим вручную.",
            )
        except Exception:
            pass
        return

    chat_id = telegram_user_id
    try:
        await bot.send_chat_action(chat_id, "typing")
        text = await generate_gift_story(
            recipient_name=params["recipient_name"],
            recipient_age=int(params["recipient_age"]),
            hero=params["hero"],
            theme_key=params["theme_key"],
            personal_note=params["personal_note"],
        )
    except Exception as e:
        logger.exception("Подарочная сказка не сгенерилась: %s", e)
        await bot.send_message(
            chat_id,
            "Что-то пошло не так у сказочника. Напишите /support — вернём деньги или сделаем вручную.",
        )
        return

    # Параллельно — озвучка и обложка
    audio_task = asyncio.create_task(synthesize_speech(text))
    scene_task = asyncio.create_task(extract_scene(text))

    # Шапка для пересылки близким
    await bot.send_message(
        chat_id,
        f"🎁 <b>Сказка в подарок для {params['recipient_name']} готова</b>\n\n"
        "Пересылайте всё ниже близким — текст, картинку и аудио — обычным форвардом Telegram.",
    )

    # 1) Картинка
    try:
        scene = await scene_task
    except Exception:
        scene = None
    try:
        img = await generate_cover(params["hero"], params["theme_key"], scene_description=scene)
        if isinstance(img, Path):
            await bot.send_photo(chat_id, FSInputFile(str(img)))
    except Exception as e:
        logger.warning("Подарочная картинка не вышла: %s", e)

    # 2) Текст — отдаём БЕЗ эмо-маркеров (они только для озвучки)
    from .story import _split_for_telegram
    from ..utils import strip_emo_markers
    display_text = strip_emo_markers(text)
    for part in _split_for_telegram(display_text):
        await bot.send_message(chat_id, part)

    # 3) Аудио
    try:
        audio = await audio_task
        if isinstance(audio, Path):
            await bot.send_audio(
                chat_id,
                FSInputFile(str(audio)),
                title=f"Сказка для {genitive(params['recipient_name'])}",
                performer=config.bot_brand,
            )
    except Exception as e:
        logger.warning("Подарочное аудио не вышло: %s", e)

    # Сохраняем в архив покупателя как gift
    async with Session() as s:
        buyer = (await s.execute(select(User).where(User.telegram_id == telegram_user_id))).scalar_one_or_none()
        if buyer:
            s.add(Story(
                user_id=buyer.id,
                child_name=params["recipient_name"],
                child_age=int(params["recipient_age"]),
                hero=params["hero"],
                theme=params["theme_key"],
                length="medium",
                # В БД сохраняем уже без маркеров (для /library)
                text=display_text,
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
            await call.answer("Это не ваша сказка", show_alert=True)
            return

    await call.answer()
    await call.message.answer(
        f"🎁 <b>Перешлите эту сказку близким</b>\n\n"
        "Удерживайте каждое сообщение ниже → «Переслать» → выберите чат. "
        "Картинка, текст и аудио — всё в одной сказке."
    )
    if st.image_path:
        try:
            await call.message.answer_photo(FSInputFile(st.image_path))
        except Exception:
            pass
    await call.message.answer(st.text)
    if st.audio_path:
        try:
            await call.message.answer_audio(
                FSInputFile(st.audio_path),
                title=f"Сказка для {genitive(st.child_name)}",
            )
        except Exception:
            pass
