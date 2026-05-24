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
    # Обратная связь / критика (May 2026)
    'ALTER TABLE users ADD COLUMN IF NOT EXISTS feedback_given BOOLEAN DEFAULT FALSE',
    'ALTER TABLE users ADD COLUMN IF NOT EXISTS feedback_skipped_count INTEGER DEFAULT 0',
    'ALTER TABLE users ADD COLUMN IF NOT EXISTS feedback_asked_at TIMESTAMP WITH TIME ZONE',
    # Лимит 1 сказка в 24 часа (May 2026)
    'ALTER TABLE users ADD COLUMN IF NOT EXISTS last_story_at TIMESTAMP WITH TIME ZONE',
    # ─── Premium-стек миграция (May 2026): новые тарифы 99/999/1485 ───
    # Пакет «15 сказок за 999 ₽» — счётчик и метка времени покупки.
    'ALTER TABLE users ADD COLUMN IF NOT EXISTS pack_stories_remaining INTEGER DEFAULT 0',
    'ALTER TABLE users ADD COLUMN IF NOT EXISTS pack_purchased_at TIMESTAMP WITH TIME ZONE',
    'ALTER TABLE users ADD COLUMN IF NOT EXISTS single_stories_remaining INTEGER DEFAULT 0',
    # Новые значения enum payment_kind. ALTER TYPE ADD VALUE IF NOT EXISTS работает с PG 9.6+
    # и идемпотентен. Каждый ALTER — отдельным запросом (PostgreSQL не позволяет
    # добавить value и сразу использовать его в той же транзакции).
    "ALTER TYPE payment_kind ADD VALUE IF NOT EXISTS 'single_story'",
    "ALTER TYPE payment_kind ADD VALUE IF NOT EXISTS 'pack_15'",
    "ALTER TYPE payment_kind ADD VALUE IF NOT EXISTS 'monthly_sub'",
    "ALTER TYPE payment_kind ADD VALUE IF NOT EXISTS 'monthly_renewal'",
    # ─── Ротация архитектур сказки (May 2026) ───
    # last_story_group хранит букву группы (А/Б/В/Г) предыдущей сказки,
    # last_story_architecture — номер архитектуры (1..25). Парсятся из первой
    # строки ответа модели и передаются обратно в промпт следующего вызова.
    'ALTER TABLE users ADD COLUMN IF NOT EXISTS last_story_group VARCHAR(1)',
    'ALTER TABLE users ADD COLUMN IF NOT EXISTS last_story_architecture INTEGER',
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
