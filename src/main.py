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
from .services import attribute_payment_to_partner, create_recurring_payment

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
                ok, payment, fresh_u = await create_recurring_payment(u)
                if ok and payment and fresh_u:
                    # Регистрируем партнёрскую комиссию (если применимо).
                    # Идемпотентно — повторный вызов на тот же payment_id не дублирует.
                    await attribute_payment_to_partner(payment, fresh_u, bot)
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


async def _retry(fn, *, name: str, attempts: int = 5, base_delay: int = 5) -> None:
    """Выполняет async-функцию с экспоненциальным бэк-оффом.
    После исчерпания попыток — пробрасывает последнее исключение,
    чтобы Docker рестартовал контейнер (последняя линия защиты)."""
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            await fn()
            return
        except Exception as e:
            last_exc = e
            delay = min(60, base_delay * (2 ** i))
            logger.warning("%s failed (attempt %d/%d): %s — retry in %ds",
                           name, i + 1, attempts, e, delay)
            await asyncio.sleep(delay)
    if last_exc:
        raise last_exc


async def main() -> None:
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    if config.sentry_dsn:
        sentry_sdk.init(dsn=config.sentry_dsn, traces_sample_rate=0.05)

    # init_db с ретраем — db может ещё подниматься
    await _retry(init_db, name="init_db", attempts=10, base_delay=3)

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(setup_routers())

    # Применяем настройки профиля (name, description, commands) при каждом старте.
    # Аватарку всё равно ставить руками в @BotFather: /setuserpic
    # Ретраим — на холодном старте Telegram может быть недоступен пару секунд.
    await _retry(lambda: setup_bot_profile(bot), name="setup_bot_profile",
                 attempts=10, base_delay=5)

    asyncio.create_task(renewal_worker(bot))
    asyncio.create_task(cache_cleaner_worker())

    # Основной polling-цикл с авто-восстановлением.
    # aiogram сам ретраит сетевые ошибки внутри long-polling, но если что-то
    # совсем экстремальное (например, токен временно отвалился, прокси упал) —
    # перехватим тут и попробуем ещё раз через 30 сек. Если уж совсем плохо —
    # Docker рестартанёт контейнер (restart: unless-stopped).
    while True:
        try:
            logger.info("Bot starting polling…")
            await dp.start_polling(bot)
            logger.info("Polling stopped gracefully")
            break
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            logger.exception("Polling crashed, restart in 30s: %s", e)
            await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(main())
