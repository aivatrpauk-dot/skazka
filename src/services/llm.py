"""Генерация текста сказки.

Provider выбирается через LLM_PROVIDER:
  • anthropic — Claude Sonnet 4.6 с prompt caching (премиум, ~2.8 ₽/сказка)
  • gemini    — Gemini 2.5 Flash (бэкап / эконом, ~0.7 ₽/сказка)

Prompt caching (только Anthropic):
  Системный промпт SYSTEM_STORYTELLER состоит из статической части (правила,
  стиль, табу — ~1900 токенов, идентично для всех юзеров) и динамической
  (имя ребёнка, герой, тема, контекст антологии). Кладём первую часть
  с cache_control=ephemeral — она кэшируется на 5 минут (TTL) на стороне
  Anthropic, при повторном вызове идёт по цене $0.30/M вместо $3/M
  (10x дешевле). Это даёт экономию ~60% на input-токенах при потоковой
  нагрузке (один pod, одна сессия).

Служебные мини-вызовы extract_scene и summarize_story всегда идут через
Gemini Flash-Lite — они короткие (~200 токенов), Anthropic для них дорог.
Если Gemini ключ не задан — мини-функции возвращают None (бот работает,
картинка генерится по fallback, антология без контекста)."""
from __future__ import annotations

import logging
import re
from typing import Any

from ..config import config
from ..prompts import (
    EXTRACT_SCENE_PROMPT,
    SERIES_CONTEXT_TEMPLATE,
    SYSTEM_GIFT_STORYTELLER,
    SYSTEM_STORYTELLER,
    THEME_CHOICES,
)

logger = logging.getLogger(__name__)


# ─────────────────── Опциональные импорты SDK ───────────────────
# Каждый SDK импортируем lazy — это даёт чистый старт без обязательных ключей.

_gemini_initialized = False
def _ensure_gemini() -> Any | None:
    """Lazy-init Gemini SDK. Возвращает модуль genai или None если ключа нет."""
    global _gemini_initialized
    if not config.gemini_api_key:
        return None
    import google.generativeai as genai
    if not _gemini_initialized:
        genai.configure(api_key=config.gemini_api_key)
        _gemini_initialized = True
    return genai


_anthropic_client = None
def _ensure_anthropic():
    """Lazy-init Anthropic SDK. Возвращает AsyncAnthropic или None если ключа нет."""
    global _anthropic_client
    if not config.anthropic_api_key:
        return None
    if _anthropic_client is None:
        from anthropic import AsyncAnthropic
        _anthropic_client = AsyncAnthropic(api_key=config.anthropic_api_key)
    return _anthropic_client


# ─────────────────── Очистка вывода LLM от технических символов ───────────────────
# Несмотря на жёсткие запреты в промпте, LLM периодически проскальзывает:
# - markdown эмфазис (*текст*, **текст**, _текст_)
# - кавычки-обратные `текст` или ```код```
# - сценические ремарки [sighs] / [шёпотом] / *задумчиво*
# - заголовки # / ## / ###
# - буллеты в начале строк

_STAGE_DIRECTION_RE = re.compile(r"\[[^\]]{0,40}\]")
_MD_BOLD_RE = re.compile(r"\*\*([^\*\n]+?)\*\*")
_MD_BOLD_UND_RE = re.compile(r"__([^_\n]+?)__")
_MD_ITALIC_AST_RE = re.compile(r"\*([^\*\n]+?)\*")
_MD_ITALIC_UND_RE = re.compile(r"(?<![а-яА-Яa-zA-Z])_([^_\n]+?)_(?![а-яА-Яa-zA-Z])")
_MD_CODE_RE = re.compile(r"`+([^`\n]+?)`+")
_MD_HEADER_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_BULLET_RE = re.compile(r"^[\s]*[\*\-•]\s+", re.MULTILINE)
_MULTI_BLANK_RE = re.compile(r"\n{3,}")


def _clean_story_text(text: str) -> str:
    """Удаляет markdown-разметку, сценические ремарки и прочий технический мусор."""
    if not text:
        return text
    text = _STAGE_DIRECTION_RE.sub("", text)
    text = _MD_HEADER_RE.sub("", text)
    text = _MD_BULLET_RE.sub("", text)
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_BOLD_UND_RE.sub(r"\1", text)
    text = _MD_ITALIC_AST_RE.sub(r"\1", text)
    text = _MD_ITALIC_UND_RE.sub(r"\1", text)
    text = _MD_CODE_RE.sub(r"\1", text)
    text = text.replace("```", "")
    text = _MULTI_BLANK_RE.sub("\n\n", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return text.strip()


# ─────────────────── Prompt caching: разрез static/dynamic ───────────────────
# Anthropic prompt caching работает по префиксу: всё, что идёт ДО блока с
# cache_control={"type":"ephemeral"} кэшируется на 5 минут. Если в системном
# промпте есть динамические плейсхолдеры — отформатированный текст уникален
# для каждого юзера → кэш не сработает.
#
# Решение: режем SYSTEM_STORYTELLER на static_prefix (до первого `{...}`) и
# dynamic_suffix (с первым плейсхолдером до конца). Static prefix идентичен
# для всех юзеров и попадает в кэш.

_PLACEHOLDER_RE = re.compile(r"\{[a-z_]+\}")


def _split_system_for_cache(system_template: str) -> tuple[str, str]:
    """Возвращает (static_prefix, dynamic_template).

    static_prefix — текст до первого плейсхолдера (можно кэшировать as-is).
    dynamic_template — остаток шаблона, его надо .format() с параметрами.

    Если плейсхолдеров нет — всё идёт в static, dynamic пустой.
    """
    match = _PLACEHOLDER_RE.search(system_template)
    if not match:
        return system_template, ""
    return system_template[:match.start()], system_template[match.start():]


# Минимальный размер кэшируемого блока — 1024 токена (требование Anthropic
# для Sonnet/Opus, для Haiku 2048). Для русского текста ~3 символа/токен →
# 1024 токена ≈ 3000 символов. Если static-префикс короче — кэшировать
# бессмысленно (write обходится дороже чем save на одном hit).
ANTHROPIC_CACHE_MIN_CHARS = 3000


# ─────────────────── Anthropic: главная генерация ───────────────────

async def _generate_anthropic(
    *,
    system_template: str,
    format_params: dict[str, Any],
    user_message: str,
    temperature: float = 0.85,
    max_tokens: int = 8000,
) -> str:
    """Генерация через Anthropic API с prompt caching.

    Логика:
      1. Режем system_template на static (cacheable) и dynamic (per-request).
      2. Если static >= ANTHROPIC_CACHE_MIN_CHARS — кладём с cache_control=ephemeral.
      3. Dynamic форматируется с параметрами юзера.
      4. user_message — короткая команда «напиши сказку».
    """
    client = _ensure_anthropic()
    if client is None:
        raise RuntimeError("ANTHROPIC_API_KEY не задан, но LLM_PROVIDER=anthropic")

    static_prefix, dynamic_template = _split_system_for_cache(system_template)
    dynamic_text = dynamic_template.format(**format_params) if dynamic_template else ""

    # Собираем system как массив блоков. Кэшируем только большой static-префикс.
    system_blocks: list[dict] = []
    if len(static_prefix) >= ANTHROPIC_CACHE_MIN_CHARS:
        system_blocks.append({
            "type": "text",
            "text": static_prefix,
            "cache_control": {"type": "ephemeral"},
        })
    else:
        # Static слишком короткий для кэша — просто кладём текстом
        if static_prefix:
            system_blocks.append({"type": "text", "text": static_prefix})
    if dynamic_text:
        system_blocks.append({"type": "text", "text": dynamic_text})

    # Защита от пустого system (теоретически невозможно, но lint видит):
    if not system_blocks:
        system_blocks = [{"type": "text", "text": "You are a helpful assistant."}]

    response = await client.messages.create(
        model=config.anthropic_model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_blocks,
        messages=[{"role": "user", "content": user_message}],
    )

    # Лог расхода токенов + cache stats — нужно для понимания экономики
    usage = response.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
    logger.info(
        "Anthropic: model=%s input=%d output=%d cache_read=%d cache_create=%d "
        "(stop=%s)",
        config.anthropic_model,
        usage.input_tokens,
        usage.output_tokens,
        cache_read,
        cache_create,
        response.stop_reason,
    )

    if response.stop_reason == "max_tokens":
        logger.warning("Anthropic: сказка обрезана по лимиту токенов")

    if not response.content:
        raise RuntimeError("Anthropic вернул пустой content")
    text_parts = [block.text for block in response.content if hasattr(block, "text")]
    return "".join(text_parts).strip()


# ─────────────────── Gemini: главная генерация ───────────────────

async def _generate_gemini(
    *,
    system_prompt: str,
    user_message: str,
    model_name: str,
    temperature: float = 0.85,
    max_tokens: int = 8000,
) -> str:
    """Генерация через Gemini. System форматируется заранее (Gemini не
    поддерживает prompt caching как у Anthropic, и для нашего объёма
    оно некритично)."""
    genai = _ensure_gemini()
    if genai is None:
        raise RuntimeError("GEMINI_API_KEY не задан, но используется Gemini-провайдер")

    model = genai.GenerativeModel(model_name, system_instruction=system_prompt)
    response = await model.generate_content_async(
        user_message,
        generation_config={"temperature": temperature, "max_output_tokens": max_tokens},
    )
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Gemini вернул пустой ответ")
    try:
        finish = response.candidates[0].finish_reason
        if str(finish).endswith("MAX_TOKENS"):
            logger.warning("Gemini: сказка обрезана по лимиту токенов: %d символов", len(text))
    except Exception:
        pass
    return text


# ─────────────────── Public API: generate_story ───────────────────

async def generate_story(
    *,
    child_name: str,
    child_age: int,
    hero: str,
    theme_key: str,
    length: str,
    paid_quality: bool,
    previous_summary: str | None = None,
) -> str:
    """Генерирует сказку. Возвращает чистый текст.

    Если переданы previous_summary — новая серия в той же вселенной с теми
    же героями (модель антологии, без обещаний продолжения сюжета).

    paid_quality — для Gemini выбор между Flash-Lite (дёшево) и Flash (лучше).
    Для Anthropic игнорируется (всегда полное качество Sonnet 4.6).
    """
    theme_label, theme_desc = THEME_CHOICES[theme_key]
    _ = length  # длина жёстко задана возрастом внутри промпта, оставлен для compat

    if previous_summary:
        series_context = SERIES_CONTEXT_TEMPLATE.format(
            previous_summary=previous_summary,
            child_name=child_name,
            hero=hero,
        )
    else:
        series_context = ""

    format_params = {
        "child_name": child_name,
        "child_age": child_age,
        "hero": hero,
        "theme": f"{theme_label} — {theme_desc}",
        "series_context": series_context,
    }
    user_message = "Напиши сказку по структуре. Завершённая концовка, никаких тизеров."

    provider = config.llm_provider
    text = ""
    try:
        if provider == "anthropic":
            text = await _generate_anthropic(
                system_template=SYSTEM_STORYTELLER,
                format_params=format_params,
                user_message=user_message,
            )
        else:
            model_name = config.gemini_model_paid if paid_quality else config.gemini_model_free
            full_system = SYSTEM_STORYTELLER.format(**format_params)
            text = await _generate_gemini(
                system_prompt=full_system,
                user_message=user_message,
                model_name=model_name,
            )
    except Exception as e:
        # Primary упал — пробуем fallback на другого провайдера если есть ключ
        logger.exception("LLM %s упал: %s", provider, e)
        if provider == "anthropic" and config.gemini_api_key:
            logger.warning("LLM fallback на Gemini Flash")
            full_system = SYSTEM_STORYTELLER.format(**format_params)
            text = await _generate_gemini(
                system_prompt=full_system,
                user_message=user_message,
                model_name=config.gemini_model_paid,
            )
        elif provider == "gemini" and config.anthropic_api_key:
            logger.warning("LLM fallback на Anthropic")
            text = await _generate_anthropic(
                system_template=SYSTEM_STORYTELLER,
                format_params=format_params,
                user_message=user_message,
            )
        else:
            raise

    if not text:
        raise RuntimeError("LLM вернул пустой ответ")
    return _clean_story_text(text)


async def generate_gift_story(
    *,
    recipient_name: str,
    recipient_age: int,
    hero: str,
    theme_key: str,
    personal_note: str,
) -> str:
    """Подарочная сказка через GIFT-промпт. Кэширование менее эффективно
    (юзер редко делает несколько подарков подряд), но logic та же."""
    theme_label, theme_desc = THEME_CHOICES[theme_key]
    format_params = {
        "recipient_name": recipient_name,
        "recipient_age": recipient_age,
        "hero": hero,
        "theme": f"{theme_label} — {theme_desc}",
        "personal_note": personal_note,
    }
    user_message = "Напиши сказку-подарок. Только текст сюжета."

    provider = config.llm_provider
    if provider == "anthropic":
        text = await _generate_anthropic(
            system_template=SYSTEM_GIFT_STORYTELLER,
            format_params=format_params,
            user_message=user_message,
            temperature=0.9,
        )
    else:
        full_system = SYSTEM_GIFT_STORYTELLER.format(**format_params)
        text = await _generate_gemini(
            system_prompt=full_system,
            user_message=user_message,
            model_name=config.gemini_model_paid,
            temperature=0.9,
        )

    if not text:
        raise RuntimeError("LLM вернул пустой ответ")
    return _clean_story_text(text)


# ─────────────────── Служебные мини-вызовы ───────────────────
# Эти функции всегда идут через Gemini Flash-Lite (дёшево, быстро).
# Если Gemini-ключа нет — возвращают None, бот работает без них:
#   - extract_scene  → image.py использует FALLBACK_SCENE_TEMPLATE
#   - summarize_story → антология идёт без контекста прошлой сказки

async def extract_scene(story_text: str) -> str | None:
    """Один лёгкий вызов для извлечения визуальной сцены под обложку.
    Возвращает строку на английском (15-25 слов) или None если не удалось."""
    genai = _ensure_gemini()
    if genai is None:
        logger.info("Gemini не настроен — пропускаю extract_scene, обложка по fallback")
        return None
    try:
        model = genai.GenerativeModel(config.gemini_model_free)
        response = await model.generate_content_async(
            EXTRACT_SCENE_PROMPT.format(story_text=story_text[:3500]),
            generation_config={"temperature": 0.3, "max_output_tokens": 200},
        )
        text = (response.text or "").strip().strip('"').strip("'")
        if 8 < len(text) < 400:
            return text
        return None
    except Exception as e:
        logger.warning("extract_scene failed: %s", e)
        return None


async def summarize_story(story_text: str) -> str | None:
    """Краткое содержание прошлой сказки — 1-2 предложения на русском.
    Контекст антологии для следующего эпизода."""
    genai = _ensure_gemini()
    if genai is None:
        logger.info("Gemini не настроен — пропускаю summarize_story, антология без контекста")
        return None
    try:
        model = genai.GenerativeModel(config.gemini_model_free)
        response = await model.generate_content_async(
            "Сократи эту сказку до 1-2 предложений на русском (где это происходило, "
            "что было главным событием, общая атмосфера). Без вступлений, только пересказ.\n\n"
            "---\n" + story_text[:3500],
            generation_config={"temperature": 0.3, "max_output_tokens": 300},
        )
        text = (response.text or "").strip()
        if 10 < len(text) < 600:
            return text
        return None
    except Exception as e:
        logger.warning("summarize_story failed: %s", e)
        return None
