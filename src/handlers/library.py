"""Архив «Мои сказки»."""
from __future__ import annotations

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
            "Пока нет сохранённых сказок. Создайте первую — её можно будет переслушать в любой момент.",
            reply_markup=main_menu_kb(),
        )
        await call.answer()
        return
    items = [(st.id, f"{st.created_at:%d.%m %H:%M} — {st.child_name}, {st.hero}") for st in rows]
    await call.message.edit_text("Ваш архив сказок:", reply_markup=library_kb(items))
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
            await call.answer("Эта сказка не из вашего архива", show_alert=True)
            return
    await call.message.answer(st.text)
    if st.image_path:
        try:
            await call.message.answer_photo(FSInputFile(st.image_path))
        except Exception:
            pass
    if st.audio_path:
        try:
            await call.message.answer_audio(
                FSInputFile(st.audio_path),
                title=f"Сказка для {genitive(st.child_name)}",
            )
        except Exception:
            pass
    await call.answer()
