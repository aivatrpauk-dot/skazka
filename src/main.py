"""Точка входа. Поднимает aiogram + cron-задачу для рекуррентных списаний."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import sys
from contextlib import suppress
from pathlib import Path

import sentry_sdk
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select

from .bot_setup import setup_bot_profile
from .config import config
from .db import Session, Story, SubscriptionStatus, User, init_db
from .handlers import setup_routers
from .services import create_recurring_payment

logger = logging.getLogger(__name__)


async def renewal_worker(bot: Bot) -> None:
    """Раз в час смотрим, у кого истекает подписка завтра. Списываем заранее."""
    while True:
        try:
            now = dt.datetime.now(dt.timezone.utc)
            window_to = now + dt.timedelta(hours=24)
            async with Session() as s:
                rows = (await s.execute(
                    select(User).where(
                        User.subscription_status == SubscriptionStatus.active,
                        User.subscription_until.isnot(None),
                        User.subscription_until <= window_to,
                        User.subscription_until > now,
                        User.yookassa_payment_method_id.isnot(None),
                    )
                )).scalars().all()
            for u in rows:
                ok = await create_recurring_payment(u)
                if not ok:
                    with suppress(Exception):
                        await bot.send_message(
                            u.telegram_id,
                            "Не получилось продлить подписку — не хватило денег или банк отклонил. "
                            "Попробуй оплатить вручную: /start → Подписка.",
                        )
        except Exception as e:
            logger.exception("renewal_worker tick failed: %s", e)
        await asyncio.sleep(3600)  # раз в час


async def cache_cleaner_worker() -> None:
    """Раз в сутки чистим audio/image кеш от файлов, на которые больше не ссылается
    ни одна сказка в БД. Это бывает после /delete_me, а также когда сказка вытеснена
    из лимита архива.

    Дополнительно — удаляем «осиротевшие» файлы старше 30 дней (если по какой-то
    причине БД не отслеживает связь). Безопасно: новые файлы регенерируются по запросу."""
    while True:
        try:
            await asyncio.sleep(86400)  # раз в сутки
            await _clean_orphan_cache()
        except Exception as e:
            logger.exception("cache_cleaner tick failed: %s", e)


async def _clean_orphan_cache() -> None:
    """Собираем все используемые audio_path/image_path из БД и удаляем всё остальное."""
    used_audio: set[str] = set()
    used_images: set[str] = set()
    async with Session() as s:
        rows = (await s.execute(
            select(Story.audio_path, Story.image_path)
        )).all()
    for audio_path, image_path in rows:
        if audio_path:
            used_audio.add(Path(audio_path).resolve().as_posix())
        if image_path:
            used_images.add(Path(image_path).resolve().as_posix())

    def _sweep(cache_dir: Path, used: set[str], min_age_hours: int = 2) -> tuple[int, int]:
        """Удаляет файлы из cache_dir, которых нет в used и которые старше N часов
        (защита от удаления файла, который только что сгенерили, но Story ещё не сохранена)."""
        if not cache_dir.exists():
            return 0, 0
        removed = 0
        kept = 0
        cutoff = dt.datetime.now().timestamp() - min_age_hours * 3600
        for f in cache_dir.iterdir():
            if not f.is_file():
                continue
            try:
                if f.resolve().as_posix() in used:
                    kept += 1
                    continue
                if f.stat().st_mtime > cutoff:
                    kept += 1
                    continue  # слишком свежий, возможно в процессе генерации
                f.unlink()
                removed += 1
            except Exception as e:
                logger.debug("cache_cleaner skip %s: %s", f, e)
        return removed, kept

    audio_rm, audio_keep = _sweep(Path(config.audio_cache_dir), used_audio)
    image_rm, image_keep = _sweep(Path(config.image_cache_dir), used_images)
    if audio_rm or image_rm:
        logger.info(
            "cache_cleaner: audio удалено %d (оставлено %d), images удалено %d (оставлено %d)",
            audio_rm, audio_keep, image_rm, image_keep,
        )


async def main() -> None:
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    if config.sentry_dsn:
        sentry_sdk.init(dsn=config.sentry_dsn, traces_sample_rate=0.05)

    await init_db()

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(setup_routers())

    # Применяем настройки профиля (name, description, commands) при каждом старте.
    # Аватарку всё равно ставить руками в @BotFather: /setuserpic
    await setup_bot_profile(bot)

    asyncio.create_task(renewal_worker(bot))
    asyncio.create_task(cache_cleaner_worker())
    logger.info("Bot starting…")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
