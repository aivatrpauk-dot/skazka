"""Rate-limiting для дорогих операций (генерация сказки).

Защита от:
- «Школьник нажимает Создать в цикле» — сжигает квоту Gemini/FAL/ElevenLabs
- Боты, пытающиеся набрать бесплатных сказок через /delete_me + повторная регистрация
- Просто слишком быстрые клики у нетерпеливого юзера

Реализация — In-memory sliding window. Достаточно для одного процесса бота
(у нас один контейнер). Если когда-нибудь захочется масштабировать на
несколько инстансов — заменяем на Redis. Не делаем сейчас, чтобы не плодить
зависимости.

Лимиты:
- 3 сказки за 60 секунд (защита от спама)
- 10 сказок в час (защита от продвинутого фарминга)

Если юзер админ — лимиты обходятся (для тестов и QA).
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from ..config import config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Limit:
    """Описание лимита: max_count операций за window_seconds секунд."""
    max_count: int
    window_seconds: int
    label: str  # для сообщения юзеру: «не больше 3 сказок в минуту»


# Конфигурация лимитов на создание сказки.
# Применяются ВСЕ — превышение любого = отказ.
STORY_LIMITS: tuple[Limit, ...] = (
    Limit(max_count=3,  window_seconds=60,    label="3 сказки в минуту"),
    Limit(max_count=10, window_seconds=3600,  label="10 сказок в час"),
    Limit(max_count=50, window_seconds=86400, label="50 сказок в сутки"),
)

# Хранилище: telegram_id → deque[timestamp]. Один deque на юзера.
# Старые записи (за пределами максимального окна) подчищаются автоматически
# при каждом обращении.
_HITS: dict[int, deque[float]] = defaultdict(deque)


def _max_window() -> int:
    return max(l.window_seconds for l in STORY_LIMITS)


def check_story_limit(telegram_id: int) -> tuple[bool, str | None]:
    """Можно ли сейчас этому юзеру сгенерить сказку?

    Возвращает (allowed, error_message).
    Если allowed=False, error_message — текст для юзера.

    Админы пропускаются без ограничений.
    """
    if telegram_id in config.admin_ids:
        return True, None

    now = time.time()
    hits = _HITS[telegram_id]

    # Чистим старые попадания
    cutoff = now - _max_window()
    while hits and hits[0] < cutoff:
        hits.popleft()

    # Проверяем каждый лимит
    for limit in STORY_LIMITS:
        window_start = now - limit.window_seconds
        count_in_window = sum(1 for t in hits if t >= window_start)
        if count_in_window >= limit.max_count:
            # Считаем сколько ждать до следующей попытки
            relevant_hits = [t for t in hits if t >= window_start]
            oldest_in_window = min(relevant_hits)
            seconds_until_free = int(oldest_in_window + limit.window_seconds - now) + 1
            wait_human = (
                f"{seconds_until_free} сек" if seconds_until_free < 60
                else f"{seconds_until_free // 60} мин"
            )
            msg = (
                f"🕯 Чуть помедленнее, пожалуйста.\n\n"
                f"Лимит — {limit.label}. Попробуйте через {wait_human}.\n\n"
                f"Если это ошибка — напишите в /support."
            )
            logger.info("Rate limit hit: user=%s limit=%s wait=%ds",
                        telegram_id, limit.label, seconds_until_free)
            return False, msg

    # Лимиты не превышены — фиксируем попадание
    hits.append(now)
    return True, None


def reset_user_limits(telegram_id: int) -> None:
    """Сброс лимитов конкретного юзера. Используется в /delete_me (hard-delete)
    и админскими командами при необходимости."""
    _HITS.pop(telegram_id, None)
