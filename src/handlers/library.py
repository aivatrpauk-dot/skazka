"""Архив «Мои сказки» — отдаёт PDF-книжки, сохранённые при генерации."""
from __future__ import annotations

import os

from aiogram import F, Router
from aiogram.types import CallbackQuery, FSInputFile
from sqlalchemy import desc, select

from ..db import Session, Story, User
from ..keyboards import library_kb, main_menu_kb
from ..utils import genitive

router = Router(name="library")


@router.callback_query(F.data == "lib:open")
async def cb_open(call: CallbackQuery) -> None:
    async with Session() as s:
        u = (await s.execute(select(User).where(User.telegram_id == call.from_user.id))).scalar_one()
        rows = (
            await s.execute(
                select(Story).where(Story.user_id == u.id).order_by(desc(Story.created_at)).limit(20)
            )
        ).scalars().all()
    if not rows:
        await call.message.edit_text(
            "📚 Полка пока пуста — здесь будут жить Ваши сказки. "
            "Давайте сложим первую, и я бережно сохраню её сюда навсегда.",
            reply_markup=main_menu_kb(),
        )
        await call.answer()
        return
    # Названия в формате «Сказка для Маши, 24 мая». Героя/тему больше не
    # упоминаем — сказочник их выбирает сам, юзеру это не нужно знать.
    items = [
        (st.id, f"Сказка для {genitive(st.child_name)}, {st.created_at:%d.%m}")
        for st in rows
    ]
    await call.message.edit_text(
        "📚 Ваша книжная полка. Нажмите любую — пришлю PDF, который "
        "можно открыть и читать снова:",
        reply_markup=library_kb(items),
    )
    await call.answer()


@router.callback_query(F.data.startswith("lib:show:"))
async def cb_show(call: CallbackQuery) -> None:
    story_id = int(call.data.split(":")[2])
    async with Session() as s:
        st = await s.get(Story, story_id)
        if not st:
            await call.answer("Сказка не найдена", show_alert=True)
            return
        u = (await s.execute(select(User).where(User.telegram_id == call.from_user.id))).scalar_one()
        if st.user_id != u.id:
            await call.answer("Эта сказка не из Вашего архива.", show_alert=True)
            return

    # Приоритет: PDF. Если есть и файл на диске — отдаём.
    if st.pdf_path and os.path.exists(st.pdf_path):
        safe_name = f"Сказка для {genitive(st.child_name)} {st.created_at:%d-%m}.pdf"
        try:
            await call.message.answer_document(
                FSInputFile(st.pdf_path, filename=safe_name),
                caption=f"📖 Сказка для {genitive(st.child_name)}",
            )
            await call.answer()
            return
        except Exception:
            pass

    # Fallback для старых сказок без сохранённого PDF (до миграции). Отдаём
    # текст — лучше так, чем ничего.
    await call.message.answer(
        f"📖 Сказка для {genitive(st.child_name)}\n\n{st.text}"
    )
    await call.answer()
