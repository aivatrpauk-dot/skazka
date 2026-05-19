"""Async-сессия SQLAlchemy + инициализация схемы при первом старте."""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ..config import config
from .models import Base

logger = logging.getLogger(__name__)

engine = create_async_engine(config.db_url, echo=False, pool_pre_ping=True)
Session: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)


# Лёгкие inline-миграции для колонок, которые добавлены после первой версии.
# Это НЕ замена Alembic — это страховка чтобы прод не падал при доливе кода.
# Каждый ALTER идемпотентен (IF NOT EXISTS).
INLINE_MIGRATIONS = [
    'ALTER TABLE stories ADD COLUMN IF NOT EXISTS next_episode_teaser TEXT',
    # Партнёрка и атрибуция (May 2026)
    'ALTER TABLE users ADD COLUMN IF NOT EXISTS partner_id INTEGER',
    'ALTER TABLE users ADD COLUMN IF NOT EXISTS utm_source VARCHAR(64)',
    'CREATE INDEX IF NOT EXISTS ix_users_partner_id ON users(partner_id)',
    'CREATE INDEX IF NOT EXISTS ix_users_utm_source ON users(utm_source)',
    # Audit trail для юр.документов (152-ФЗ + оферта)
    'ALTER TABLE users ADD COLUMN IF NOT EXISTS tos_accepted_at TIMESTAMP WITH TIME ZONE',
    # Защита от replay-платежей: один telegram_payment_charge_id = одна запись Payment.
    # Если Telegram вдруг доставит successful_payment повторно (после рестарта бота
    # или сбоя), повторный INSERT упадёт с unique violation, дубль не пройдёт.
    'CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_telegram_charge '
    'ON payments(telegram_payment_charge_id) WHERE telegram_payment_charge_id IS NOT NULL',
]


async def init_db() -> None:
    """На холодном старте создаём таблицы + догоняем inline-миграции."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for sql in INLINE_MIGRATIONS:
            try:
                await conn.execute(text(sql))
            except Exception as e:
                logger.warning("Inline migration failed (probably ok): %s — %s", sql, e)
