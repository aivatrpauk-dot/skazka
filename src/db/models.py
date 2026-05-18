"""Модели БД. SQLAlchemy 2.0 async, типизированные."""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class SubscriptionStatus(str, enum.Enum):
    none = "none"
    trial = "trial"
    active = "active"
    cancelled = "cancelled"
    past_due = "past_due"


class PaymentKind(str, enum.Enum):
    subscription = "subscription"
    gift = "gift"
    renewal = "renewal"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    language_code: Mapped[str | None] = mapped_column(String(8))

    # реферал
    referred_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    referral_code: Mapped[str] = mapped_column(String(32), unique=True, index=True)

    # лимиты и бонусы
    free_stories_used: Mapped[int] = mapped_column(Integer, default=0)
    bonus_stories: Mapped[int] = mapped_column(Integer, default=0)

    # подписка
    subscription_status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus, name="subscription_status"), default=SubscriptionStatus.none
    )
    subscription_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    yookassa_payment_method_id: Mapped[str | None] = mapped_column(String(128))

    # дефолтные поля ребёнка (можно менять)
    child_name: Mapped[str | None] = mapped_column(String(64))
    child_age: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    stories: Mapped[list["Story"]] = relationship(back_populates="user", cascade="all,delete-orphan")
    payments: Mapped[list["Payment"]] = relationship(back_populates="user", cascade="all,delete-orphan")


class Story(Base):
    __tablename__ = "stories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    child_name: Mapped[str] = mapped_column(String(64))
    child_age: Mapped[int] = mapped_column(Integer)
    hero: Mapped[str] = mapped_column(String(128))
    theme: Mapped[str] = mapped_column(String(64))
    length: Mapped[str] = mapped_column(String(16))  # short/medium

    text: Mapped[str] = mapped_column(Text)
    audio_path: Mapped[str | None] = mapped_column(String(256))
    image_path: Mapped[str | None] = mapped_column(String(256))

    is_paid_quality: Mapped[bool] = mapped_column(Boolean, default=False)
    is_gift: Mapped[bool] = mapped_column(Boolean, default=False)
    gift_recipient_name: Mapped[str | None] = mapped_column(String(64))

    # «Тизер» в конце сказки — 1-2 предложения про то, что произойдёт в следующей серии.
    # На следующий день показываем кнопку «🔮 Что было дальше с {hero}?» и эта строка
    # уходит как контекст в промпт продолжения.
    next_episode_teaser: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship(back_populates="stories")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[PaymentKind] = mapped_column(Enum(PaymentKind, name="payment_kind"))
    amount_kopecks: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    provider_payment_charge_id: Mapped[str | None] = mapped_column(String(128))
    telegram_payment_charge_id: Mapped[str | None] = mapped_column(String(128))
    yookassa_payment_id: Mapped[str | None] = mapped_column(String(128))
    succeeded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship(back_populates="payments")


class Referral(Base):
    __tablename__ = "referrals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    inviter_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    invited_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    bonus_granted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
