"""Конфигурация. Все настройки через переменные окружения (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Final

from dotenv import load_dotenv

load_dotenv()


def _env(key: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(key, default)
    if required and not value:
        raise RuntimeError(f"ENV {key} обязательна, но не задана")
    return value or ""


def _int(key: str, default: int) -> int:
    raw = os.getenv(key)
    return int(raw) if raw else default


def _float(key: str, default: float) -> float:
    raw = os.getenv(key)
    return float(raw) if raw else default


@dataclass(frozen=True)
class Config:
    # Telegram
    bot_token: str = field(default_factory=lambda: _env("BOT_TOKEN", required=True))
    bot_brand: str = field(default_factory=lambda: _env("BOT_BRAND", "Сказка"))
    admin_ids: tuple[int, ...] = field(
        default_factory=lambda: tuple(int(x) for x in _env("ADMIN_IDS", "").split(",") if x.strip())
    )

    # ЮKassa (provider token, получаем у @BotFather после привязки ЮKassa)
    yookassa_provider_token: str = field(default_factory=lambda: _env("YOOKASSA_PROVIDER_TOKEN", required=True))
    # Для рекуррентов — прямые API-ключи ЮKassa
    yookassa_shop_id: str = field(default_factory=lambda: _env("YOOKASSA_SHOP_ID", ""))
    yookassa_secret_key: str = field(default_factory=lambda: _env("YOOKASSA_SECRET_KEY", ""))

    # API ключи
    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY", required=True))
    gemini_model_free: str = field(default_factory=lambda: _env("GEMINI_MODEL_FREE", "gemini-2.5-flash-lite"))
    gemini_model_paid: str = field(default_factory=lambda: _env("GEMINI_MODEL_PAID", "gemini-2.5-flash"))

    elevenlabs_api_key: str = field(default_factory=lambda: _env("ELEVENLABS_API_KEY", ""))
    # Русский нежный голос — живой, выразительный, чуть «мультяшный» в хорошем смысле:
    # передаёт эмоции через интонацию, а не «дикторски-монотонно». Лучше зашёл по тестам,
    # чем «правильный мамский» Mariia (WfExDXCt2GBg6MI5KjQk) — тот красивый, но каменный.
    elevenlabs_voice_id: str = field(default_factory=lambda: _env("ELEVENLABS_VOICE_ID", "GN4wbsbejSnGSa1AzjH5"))
    elevenlabs_model: str = field(default_factory=lambda: _env("ELEVENLABS_MODEL", "eleven_turbo_v2_5"))

    fal_api_key: str = field(default_factory=lambda: _env("FAL_KEY", ""))
    fal_model: str = field(default_factory=lambda: _env("FAL_MODEL", "fal-ai/flux/schnell"))

    # FusionBrain / Kandinsky (российский провайдер картинок, платится рублями)
    fusionbrain_api_key: str = field(default_factory=lambda: _env("FUSIONBRAIN_API_KEY", ""))
    fusionbrain_secret_key: str = field(default_factory=lambda: _env("FUSIONBRAIN_SECRET_KEY", ""))

    # БД
    db_url: str = field(default_factory=lambda: _env("DB_URL", "postgresql+asyncpg://skazka:skazka@db:5432/skazka"))

    # Лимиты
    free_story_limit: int = field(default_factory=lambda: _int("FREE_STORY_LIMIT", 3))
    referral_bonus: int = field(default_factory=lambda: _int("REFERRAL_BONUS", 3))

    # Тарифы (копейки)
    price_sub_kopecks: int = field(default_factory=lambda: _int("PRICE_SUB_KOPECKS", 49000))   # 490 ₽
    price_gift_kopecks: int = field(default_factory=lambda: _int("PRICE_GIFT_KOPECKS", 19900))  # 199 ₽

    # Окружение
    env: str = field(default_factory=lambda: _env("ENV", "production"))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    sentry_dsn: str = field(default_factory=lambda: _env("SENTRY_DSN", ""))

    # Пути
    audio_cache_dir: str = field(default_factory=lambda: _env("AUDIO_CACHE_DIR", "./cache/audio"))
    image_cache_dir: str = field(default_factory=lambda: _env("IMAGE_CACHE_DIR", "./cache/images"))


config: Final = Config()
