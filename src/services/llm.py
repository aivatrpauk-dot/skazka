"""Генерация текста сказки через Gemini API.
Для платных юзеров — Gemini 2.5 Flash (качественнее),
для триальщиков — Gemini 2.5 Flash-Lite (10x дешевле).

Также — служебные мини-вызовы для извлечения визуальной сцены
и краткого пересказа прошлой сказки (для контекста антологии)."""
from __future__ import annotations

import logging

import google.generativeai as genai

from ..config import config
from ..prompts import (
    EXTRACT_SCENE_PROMPT,
    LENGTH_HINTS,
    SERIES_CONTEXT_TEMPLATE,
    SYSTEM_GIFT_STORYTELLER,
    SYSTEM_STORYTELLER,
    THEME_CHOICES,
)

logger = logging.getLogger(__name__)
genai.configure(api_key=config.gemini_api_key)


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

    Если переданы previous_summary — это новая серия в той же вселенной с
    теми же героями. Никаких обещаний и продолжений сюжета — просто
    «знакомые герои в новом приключении» (модель антологии)."""
    theme_label, theme_desc = THEME_CHOICES[theme_key]
    target_length = LENGTH_HINTS.get(length, LENGTH_HINTS["medium"])

    if previous_summary:
        series_context = SERIES_CONTEXT_TEMPLATE.format(
            previous_summary=previous_summary,
            child_name=child_name,
            hero=hero,
        )
    else:
        series_context = ""

    system_prompt = SYSTEM_STORYTELLER.format(
        child_name=child_name,
        child_age=child_age,
        hero=hero,
        theme=f"{theme_label} — {theme_desc}",
        target_length=target_length,
        series_context=series_context,
    )

    model_name = config.gemini_model_paid if paid_quality else config.gemini_model_free
    model = genai.GenerativeModel(model_name, system_instruction=system_prompt)

    response = await model.generate_content_async(
        "Напиши сказку по структуре. Завершённая концовка, никаких тизеров.",
        generation_config={"temperature": 0.85, "max_output_tokens": 8000},
    )
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("LLM вернул пустой ответ")

    try:
        finish = response.candidates[0].finish_reason
        if str(finish).endswith("MAX_TOKENS"):
            logger.warning("Сказка обрезана по лимиту токенов: %d символов", len(text))
    except Exception:
        pass

    return text


async def generate_gift_story(
    *,
    recipient_name: str,
    recipient_age: int,
    hero: str,
    theme_key: str,
    personal_note: str,
) -> str:
    theme_label, theme_desc = THEME_CHOICES[theme_key]
    system_prompt = SYSTEM_GIFT_STORYTELLER.format(
        recipient_name=recipient_name,
        recipient_age=recipient_age,
        hero=hero,
        theme=f"{theme_label} — {theme_desc}",
        personal_note=personal_note,
    )
    model = genai.GenerativeModel(config.gemini_model_paid, system_instruction=system_prompt)
    response = await model.generate_content_async(
        "Напиши сказку-подарок. Только текст сюжета.",
        generation_config={"temperature": 0.9, "max_output_tokens": 8000},
    )
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("LLM вернул пустой ответ")
    try:
        finish = response.candidates[0].finish_reason
        if str(finish).endswith("MAX_TOKENS"):
            logger.warning("Подарочная сказка обрезана по лимиту токенов: %d символов", len(text))
    except Exception:
        pass
    return text


# ─────────────────── Служебные мини-вызовы ───────────────────

async def extract_scene(story_text: str) -> str | None:
    """Один лёгкий вызов Flash-Lite, чтобы вытащить визуальную сцену для картинки.
    Возвращает строку на английском (15-25 слов) или None если не удалось."""
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
    Используется как контекст антологии для следующего эпизода."""
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
