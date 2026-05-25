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
    EXTRACT_THREE_SCENES_PROMPT,
    SERIES_CONTEXT_TEMPLATE,
    SYSTEM_GIFT_STORYTELLER,
    SYSTEM_STORYTELLER,
    SYSTEM_STORYTELLER_SIMPLE_WONDER,
    SYSTEM_STORYTELLER_TODDLER,
    THEME_CHOICES,
    parse_story_marker,
    parse_story_title,
    pick_storyteller_prompt,
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
    temperature: float = 0.95,
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
    temperature: float = 0.95,
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
    hero: str = "",
    theme_key: str = "",
    length: str = "",
    paid_quality: bool = True,
    previous_summary: str | None = None,
    last_story_group: str | None = None,
    last_story_architecture: int | None = None,
    last_story_humor_register: int | None = None,
    last_story_category: str | None = None,
) -> tuple[str, str | None, int | None, int | None, str | None, str | None]:
    """Генерирует сказку. Возвращает кортеж:
    (чистый текст, группа, архитектура, регистр юмора, название, категория).

    Сказочник сам выбирает группу и архитектуру из 25 шаблонов. Чтобы он
    не повторял группу два дня подряд — мы храним `last_story_group` в БД
    юзера и передаём сюда. Если передано — модель получает явную подсказку
    «вчера была группа X, сегодня другая».

    Первой строкой модель пишет маркер «Группа В, архитектура 11 — …»,
    который мы парсим, сохраняем в БД и обрезаем от текста.

    hero / theme_key / length / previous_summary — оставлены для обратной
    совместимости с вызовами из старого UI, в новом промпте игнорируются.

    paid_quality — для Gemini выбор между Flash-Lite (дёшево) и Flash (лучше).
    Для Anthropic игнорируется (всегда полное качество Sonnet 4.6).
    """
    _ = hero, theme_key, length, previous_summary  # legacy, не используется

    # Выбираем промпт по возрасту + чередуем жанры (MP↔SW) для 5-6:
    # 3-4 → toddler (8 архитектур, проще, добрее).
    # 5-6 → ЧЕРЕДУЕМ: «Маленький принц» (MP) и «простое волшебство» (SW)
    #       выбирается на основе last_story_category. Логика в
    #       pick_storyteller_prompt(): если прошлая была MP → сейчас SW,
    #       и наоборот.
    storyteller_prompt, rotation_hint_template, this_category = pick_storyteller_prompt(
        child_age, last_story_category,
    )
    prompt_label = {
        SYSTEM_STORYTELLER_TODDLER: "toddler",
        SYSTEM_STORYTELLER: "MP (Маленький принц)",
        SYSTEM_STORYTELLER_SIMPLE_WONDER: "SW (простое волшебство)",
    }.get(storyteller_prompt, "unknown")
    logger.info(
        "Выбран промпт: %s для возраста %d (предыдущая категория: %s)",
        prompt_label, child_age, last_story_category or "—",
    )

    # Rotation hint работает ТОЛЬКО внутри одной категории. Если категория
    # сменилась (альтернация MP↔SW) — нумерация архитектур в новой
    # категории другая, старая подсказка бессмысленна, передаём пустую.
    category_unchanged = (
        last_story_category == this_category
        or (this_category == "TODDLER" and last_story_category in (None, "TODDLER"))
    )
    if category_unchanged and last_story_architecture and last_story_humor_register:
        rotation_hint = rotation_hint_template.format(
            last_group=last_story_group or "",
            last_architecture=last_story_architecture,
            last_humor_register=last_story_humor_register,
        )
    elif category_unchanged and last_story_architecture:
        # Legacy fallback — только архитектура известна, регистр свободен.
        rotation_hint = (
            f"Предыдущая сказка была по архитектуре №{last_story_architecture}. "
            "Сегодня — выбери другую архитектуру и любой регистр юмора."
        )
    else:
        # Первая сказка ИЛИ смена категории — подсказку не передаём.
        rotation_hint = ""

    format_params = {
        "child_name": child_name,
        "child_age": child_age,
        "rotation_hint": rotation_hint,
    }
    user_message = "Напиши сказку. Не забудь маркер архитектуры первой строкой."

    # Подсказка гендера для CIS/татарских/кавказских имён (Амина, Айдар,
    # Тимур, Айгуль и т.п.). LLM иногда ошибается в склонении нестандартных
    # имён — например, «Амину» как родительный множественного от «амины»
    # → «Амин». Если наш словарь знает гендер — явно скажем модели.
    from ..utils import detect_name_gender as _detect_gender
    _gender_hint = _detect_gender(child_name)
    if _gender_hint is not None:
        from petrovich.enums import Gender as _G
        _gender_text = "девочка" if _gender_hint == _G.FEMALE else "мальчик"
        user_message += (
            f" Важно: {child_name} — {_gender_text}. Склоняй имя правильно "
            f"в каждом падеже."
        )

    provider = config.llm_provider
    text = ""
    try:
        if provider == "anthropic":
            text = await _generate_anthropic(
                system_template=storyteller_prompt,
                format_params=format_params,
                user_message=user_message,
            )
        else:
            model_name = config.gemini_model_paid if paid_quality else config.gemini_model_free
            full_system = storyteller_prompt.format(**format_params)
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
            full_system = storyteller_prompt.format(**format_params)
            text = await _generate_gemini(
                system_prompt=full_system,
                user_message=user_message,
                model_name=config.gemini_model_paid,
            )
        elif provider == "gemini" and config.anthropic_api_key:
            logger.warning("LLM fallback на Anthropic")
            text = await _generate_anthropic(
                system_template=storyteller_prompt,
                format_params=format_params,
                user_message=user_message,
            )
        else:
            raise

    if not text:
        raise RuntimeError("LLM вернул пустой ответ")

    # Парсим маркер первой строки. Возвращает 5 значений:
    # (cleaned, group, architecture, humor_register, category).
    # category — "MP" / "SW" / "TODDLER" / None.
    cleaned, group, architecture, humor_register, category = parse_story_marker(text)
    if category is None:
        logger.warning(
            "LLM не вернул маркер категории/архитектуры. Альтернация не сработает на следующей сказке."
        )
    else:
        logger.info(
            "Сказка: категория %s, группа %s, архитектура %s, регистр юмора %s",
            category, group, architecture, humor_register,
        )

    # Парсим название во второй строке (в «ёлочках») и тоже обрезаем.
    cleaned, title = parse_story_title(cleaned)
    if title:
        logger.info("Название сказки: %s", title)
    else:
        logger.warning("LLM не вернул название сказки в «ёлочках». Используется fallback.")

    return _clean_story_text(cleaned), group, architecture, humor_register, title, category


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
    # THEME_CHOICES хранит 3 элемента: label, desc, title_phrase.
    # Для LLM-промпта нам нужны первые два, третий используется в PDF-заголовке.
    theme_label, theme_desc = THEME_CHOICES[theme_key][:2]
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


async def extract_three_scenes(story_text: str) -> dict[str, str] | None:
    """Извлекает opening/climax/ending сцены для 3 иллюстраций книжки.

    Возвращает dict {"opening": "...", "climax": "...", "ending": "..."}
    или None если не удалось распарсить.

    Использует Gemini Flash-Lite — дёшево, быстро, для структурированной задачи
    в самый раз. Если Gemini нет — возвращаем None, и handlers/story.py
    использует обычный extract_scene для одной картинки + FALLBACK для остальных.
    """
    genai = _ensure_gemini()
    if genai is None:
        logger.info("Gemini не настроен — пропускаю extract_three_scenes")
        return None
    try:
        model = genai.GenerativeModel(config.gemini_model_free)
        response = await model.generate_content_async(
            EXTRACT_THREE_SCENES_PROMPT.format(story_text=story_text[:4000]),
            generation_config={
                "temperature": 0.3,
                "max_output_tokens": 500,
                # response_mime_type для гарантии JSON. Gemini 2.5 поддерживает.
                "response_mime_type": "application/json",
            },
        )
        raw = (response.text or "").strip()
        if not raw:
            return None
        import json
        # На случай если модель всё-таки обернула в ```json ... ``` —
        # вырезаем codeblock-обёртку.
        cleaned = raw
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()
        data = json.loads(cleaned)
        # Валидация — все три ключа должны быть строками
        for key in ("opening", "climax", "ending"):
            v = data.get(key)
            if not isinstance(v, str) or not (8 < len(v) < 400):
                logger.warning("extract_three_scenes: невалидный ключ %s = %r", key, v)
                return None
        return {"opening": data["opening"], "climax": data["climax"], "ending": data["ending"]}
    except Exception as e:
        logger.warning("extract_three_scenes failed: %s", e)
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
