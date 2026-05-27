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


def _bool(key: str, default: bool) -> bool:
    """Парсит булевый env: true/1/yes/on → True, false/0/no/off → False, иначе default."""
    raw = os.getenv(key, "").strip().lower()
    if raw in ("true", "1", "yes", "on"):
        return True
    if raw in ("false", "0", "no", "off"):
        return False
    return default


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

    # ─────────────── LLM-провайдер ───────────────
    # anthropic — Claude Sonnet 4.6 (премиум, prompt-caching, ~2.8 ₽/сказка)
    # gemini    — Gemini 2.5 Flash (бэкап, ~0.7 ₽/сказка, заметно проще языком)
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "anthropic"))

    # Anthropic (premium)
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY", ""))
    anthropic_model: str = field(default_factory=lambda: _env("ANTHROPIC_MODEL", "claude-sonnet-4-6"))

    # Gemini (fallback / эконом). Опциональный — если LLM_PROVIDER=anthropic, ключ не обязателен.
    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY", ""))
    gemini_model_free: str = field(default_factory=lambda: _env("GEMINI_MODEL_FREE", "gemini-2.5-flash-lite"))
    gemini_model_paid: str = field(default_factory=lambda: _env("GEMINI_MODEL_PAID", "gemini-2.5-flash"))

    # TTS (озвучка) удалена в мае 2026 — продукт стал «PDF-книжка, родитель
    # читает сам перед сном». Если когда-то будете возвращать озвучку,
    # подсмотрите в git-историю: tts.py, tts_azure.py, bg_music.py и блок
    # azure_*/yandex_*/elevenlabs_*/suno_* конфигов лежали тут.

    # ─────────────── Изображения ───────────────
    # IMAGE_MODEL: recraft-v3 (premium, ~3.6 ₽) | flux-pro-1.1 | flux-dev | flux-schnell (0.3 ₽).
    # Все идут через один FAL_KEY. Маппинг на endpoint — в services/image.py.
    image_model: str = field(default_factory=lambda: _env("IMAGE_MODEL", "recraft-v3"))
    fal_api_key: str = field(default_factory=lambda: _env("FAL_KEY", ""))

    # Recraft Direct API — ходим напрямую в api.recraft.ai, без FAL-обёртки.
    # FAL раньше использовали как универсальную обёртку под Flux/Recraft, но
    # теперь мы только на Recraft, и прямой API даёт полный набор фич (custom
    # styles!) и убирает посредника. RECRAFT_API_KEY получается на recraft.ai
    # → Profile → API. Используется для:
    #   1) тренировки custom style (scripts/create_recraft_style.py);
    #   2) ежедневной генерации картинок (image.py → _generate_recraft_direct).
    # Если RECRAFT_API_KEY пуст — image.py упадёт обратно на FAL (legacy).
    recraft_api_key: str = field(default_factory=lambda: _env("RECRAFT_API_KEY", ""))

    # Recraft Custom Style — наш натренированный приватный стиль.
    # Создаётся одноразово скриптом scripts/create_recraft_style.py
    # (тренируется на картинках из style_references/). Если задан —
    # image.py передаёт его в Recraft вместо встроенного preset'а
    # (digital_illustration/hand_drawn и т.п.) и модель рисует в нашем
    # натренированном книжном стиле. Если пусто — фолбэк на встроенный
    # preset (см. recraft_style_preset).
    recraft_style_id: str = field(default_factory=lambda: _env("RECRAFT_STYLE_ID", ""))

    # Recraft built-in preset (fallback когда recraft_style_id пуст). Можно
    # быстро менять через .env без редактирования кода. Варианты:
    #   digital_illustration/hand_drawn       — рисованный, иногда крайон-вайб
    #   digital_illustration/2d_art_poster    — плотный графичный, насыщенный
    #   digital_illustration/grain            — добавляет зернистую текстуру
    #   digital_illustration/storybook_rough  — атмосферная книжная иллюстрация
    #   digital_illustration/hand_drawn_outline — рисованный с подчёркнутым контуром
    #   digital_illustration                  — без подстиля (Netflix-glossy)
    # Дефолт hand_drawn оставляем для backward compat.
    recraft_style_preset: str = field(default_factory=lambda: _env(
        "RECRAFT_STYLE_PRESET", "digital_illustration/hand_drawn"
    ))

    # Legacy: если задан FAL_MODEL — переопределяет image_model (backward compat).
    fal_model_legacy: str = field(default_factory=lambda: _env("FAL_MODEL", ""))

    # FusionBrain / Kandinsky (legacy fallback на случай если FAL ляжет)
    fusionbrain_api_key: str = field(default_factory=lambda: _env("FUSIONBRAIN_API_KEY", ""))
    fusionbrain_secret_key: str = field(default_factory=lambda: _env("FUSIONBRAIN_SECRET_KEY", ""))

    # ─────────────── Канальная воронка (демонстрация продукта) ───────────────
    # Каждый день в 18:00 МСК бот публикует в указанный канал одну сказку
    # с одной обложкой и PDF — как пример своих возможностей. Каждый 3-й
    # пост содержит CTA-ссылку на сам бот для персонализированной сказки.
    # Включается флагом CHANNEL_PUBLISH_ENABLED; если выключено — scheduler
    # просто не стартует.
    channel_publish_enabled: bool = field(default_factory=lambda: _bool("CHANNEL_PUBLISH_ENABLED", False))
    # Канал куда постим. Формат: @username (для публичного) или -100... (для приватного).
    # Бот должен быть админом канала с правом «отправлять сообщения».
    channel_id: str = field(default_factory=lambda: _env("CHANNEL_ID", ""))
    # Каждый N-й пост содержит CTA. По умолчанию каждый 3-й.
    channel_cta_every_n: int = field(default_factory=lambda: _int("CHANNEL_CTA_EVERY_N", 3))
    # Час публикации по МСК. Дефолт 18:00.
    channel_publish_hour_msk: int = field(default_factory=lambda: _int("CHANNEL_PUBLISH_HOUR_MSK", 18))

    # БД
    db_url: str = field(default_factory=lambda: _env("DB_URL", "postgresql+asyncpg://skazka:skazka@db:5432/skazka"))

    # ─────────────── Лимиты и тарифы ───────────────
    # Новая бесплатная норма: ОДНА сказка триал. Дальше paywall.
    free_story_limit: int = field(default_factory=lambda: _int("FREE_STORY_LIMIT", 1))
    # Реферальный бонус: даётся приглашающему ТОЛЬКО когда приглашённый юзер
    # сделает первую успешную оплату (любого тарифа: single/pack/monthly).
    # Просто переход по ?start=ref_XXX бонуса больше не даёт.
    referral_bonus: int = field(default_factory=lambda: _int("REFERRAL_BONUS", 1))

    # Разовая сказка — 149 ₽ (премиум-позиционирование, май 2026)
    price_single_kopecks: int = field(default_factory=lambda: _int("PRICE_SINGLE_KOPECKS", 14900))
    # Пакет 15 сказок — УБРАН ИЗ UI (handler оставлен в billing.py на случай
    # возврата). Поля сохранены для backward-compat с уже существующей
    # колонкой БД pack_stories_remaining.
    price_pack_kopecks: int = field(default_factory=lambda: _int("PRICE_PACK_KOPECKS", 99900))
    pack_stories_count: int = field(default_factory=lambda: _int("PACK_STORIES_COUNT", 15))
    # Месячная подписка (одна в день, рекуррент) — 2990 ₽
    # При 30 сказках в месяце выходит ~99₽ за сказку, плюс защита от
    # «забыл купить» для ежедневного ритуала.
    price_monthly_kopecks: int = field(default_factory=lambda: _int("PRICE_MONTHLY_KOPECKS", 299000))
    # Подарочная сказка — 199 ₽ (оставляем для /gift)
    price_gift_kopecks: int = field(default_factory=lambda: _int("PRICE_GIFT_KOPECKS", 19900))

    # Legacy: старая цена подписки 490 ₽ — оставляем поле на случай ссылок в коде/логах,
    # но в новой логике не используется.
    price_sub_kopecks: int = field(default_factory=lambda: _int("PRICE_SUB_KOPECKS", 0))

    # Окружение
    env: str = field(default_factory=lambda: _env("ENV", "production"))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    sentry_dsn: str = field(default_factory=lambda: _env("SENTRY_DSN", ""))

    # Пути
    audio_cache_dir: str = field(default_factory=lambda: _env("AUDIO_CACHE_DIR", "./cache/audio"))
    image_cache_dir: str = field(default_factory=lambda: _env("IMAGE_CACHE_DIR", "./cache/images"))


config: Final = Config()
