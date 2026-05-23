"""Azure Neural TTS — премиум-провайдер для озвучки сказок.

Голос ru-RU-SvetlanaNeural с style="affectionate" — тёплая «мама перед сном»,
заметно живее Яндекс-alena. Тариф Neural (Standard S0 в Azure): $16/M символов
≈ 7 ₽ за сказку 5000 символов.

Чем отличается от Яндекса:
  • SSML mstts:express-as даёт стилизованный «любящий» тон, у Яндекса только
    три базовые эмоции (good/neutral/evil).
  • Лимит запроса 10 минут синтеза вместо 5000 символов у Яндекса —
    обычно укладываемся в один request без дробления.
  • Stress-управление через <prosody rate="-8%"> работает мягче чем
    Яндекс speed=0.95 (тот растягивает фонемы, Azure пересинтезирует).

Микс с фоновой музыкой делает services.tts._mix_with_ambient (общий код),
здесь только синтез голоса в чистый mp3."""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import httpx

from ..config import config

logger = logging.getLogger(__name__)

# Azure лимиты: 10 минут синтеза за один запрос. 5000 символов русского текста
# = ~4-5 минут речи. Один запрос почти всегда хватает. Дробим только если
# текст сильно за 12000 символов (редко).
AZURE_MAX_CHARS_PER_REQUEST = 12000

# Тайминги пауз в SSML (миллисекунды)
PAUSE_PARAGRAPH_MS = 500   # пауза между абзацами
PAUSE_SENTENCE_MS = 250    # пауза между предложениями внутри абзаца

# Регэксп разбиения на предложения, копия из tts.py (тот же подход)
_SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+(?=[А-ЯA-Z—«"])')


def _azure_endpoint() -> str:
    """REST-endpoint синтеза для региона из конфига."""
    region = config.azure_speech_region
    return f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"


def _escape_ssml(s: str) -> str:
    """Экранирование спецсимволов XML для SSML."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _to_ssml(text: str) -> str:
    """Превращает сырой текст сказки в SSML для Azure Neural TTS.

    Структура:
      <speak xmlns="..." xml:lang="ru-RU">
        <voice name="...">
          <mstts:express-as style="affectionate" styledegree="1.2">
            <prosody rate="-8%">
              <p>
                <s>Жил-был котёнок.</s>
                <s>Звали его Барсик.</s>
              </p>
              <break time="500ms"/>
              <p>...</p>
            </prosody>
          </mstts:express-as>
        </voice>
      </speak>

    Без mstts:express-as голос звучит ровно как новости — нужен стиль для
    «сказочной интонации». styledegree=1.2 = чуть усиленный стиль (1.0 — норма).
    """
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        paragraphs = [text]

    body_parts: list[str] = []
    for i, para in enumerate(paragraphs):
        sentences = _SENTENCE_SPLIT_RE.split(para)
        body_parts.append("<p>")
        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue
            body_parts.append(f"<s>{_escape_ssml(sent)}</s>")
            # Короткая пауза между предложениями — выразительнее чем дефолт
            body_parts.append(f'<break time="{PAUSE_SENTENCE_MS}ms"/>')
        body_parts.append("</p>")
        # Дополнительная пауза между абзацами кроме последнего
        if i < len(paragraphs) - 1:
            body_parts.append(f'<break time="{PAUSE_PARAGRAPH_MS}ms"/>')

    body = "".join(body_parts)

    voice = config.azure_tts_voice
    style = config.azure_tts_style or "affectionate"
    rate = config.azure_tts_rate or "-8%"

    # ВАЖНО: namespace mstts: обязателен для express-as.
    # styledegree допустимые значения 0.01..2 (1.0 default).
    return (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        'xmlns:mstts="http://www.w3.org/2001/mstts" xml:lang="ru-RU">'
        f'<voice name="{voice}">'
        f'<mstts:express-as style="{style}" styledegree="1.2">'
        f'<prosody rate="{rate}">'
        f'{body}'
        '</prosody>'
        '</mstts:express-as>'
        '</voice>'
        '</speak>'
    )


def _split_for_azure(text: str, max_chars: int = AZURE_MAX_CHARS_PER_REQUEST) -> list[str]:
    """Делит текст на куски по абзацам если он длиннее лимита.
    Для большинства сказок (<10к символов) возвращает [text] — единый запрос."""
    if len(text) <= max_chars:
        return [text]
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current = ""
    for p in paragraphs:
        if len(current) + len(p) + 2 > max_chars:
            if current:
                chunks.append(current.strip())
            current = p
        else:
            current = current + "\n\n" + p if current else p
    if current:
        chunks.append(current.strip())
    return chunks


async def _synthesize_chunk(text: str) -> bytes | None:
    """Один запрос к Azure REST API. Возвращает байты mp3 или None при ошибке.

    Формат audio-16khz-128kbitrate-mono-mp3 — хороший компромисс размера и
    качества для голоса. 24kHz/48kHz даёт чуть лучше, но в 2 раза больше файл.
    """
    if not config.azure_speech_key:
        logger.error("AZURE_SPEECH_KEY не задан")
        return None

    ssml = _to_ssml(text)
    headers = {
        "Ocp-Apim-Subscription-Key": config.azure_speech_key,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": "audio-24khz-160kbitrate-mono-mp3",
        "User-Agent": "skazka-bot",
    }

    url = _azure_endpoint()
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(url, headers=headers, content=ssml.encode("utf-8"))
        if resp.status_code != 200:
            # Логируем body для диагностики (Azure возвращает понятные тексты ошибок)
            err = resp.text[:400] if resp.text else "<empty>"
            logger.error(
                "Azure TTS error %s on region=%s voice=%s: %s",
                resp.status_code,
                config.azure_speech_region,
                config.azure_tts_voice,
                err,
            )
            # 401 — невалидный ключ; 403 — превышен лимит; 400 — битый SSML.
            # 400 чаще всего из-за того что голос не поддерживает style.
            # Пробуем ретрай без express-as обёртки.
            if resp.status_code == 400 and "<mstts:express-as" in ssml:
                logger.warning("Azure: пробую без mstts:express-as (style не поддержан?)")
                fallback_ssml = _to_ssml_no_style(text)
                resp = await client.post(url, headers=headers, content=fallback_ssml.encode("utf-8"))
                if resp.status_code == 200:
                    return resp.content
                logger.error("Azure fallback тоже упал: %s %s",
                             resp.status_code, resp.text[:200])
            return None
        return resp.content


def _to_ssml_no_style(text: str) -> str:
    """Fallback SSML без mstts:express-as — для голосов которые не поддерживают
    стиль (например DariyaNeural). Голос будет нейтральным."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        paragraphs = [text]

    body_parts: list[str] = []
    for i, para in enumerate(paragraphs):
        sentences = _SENTENCE_SPLIT_RE.split(para)
        body_parts.append("<p>")
        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue
            body_parts.append(f"<s>{_escape_ssml(sent)}</s>")
            body_parts.append(f'<break time="{PAUSE_SENTENCE_MS}ms"/>')
        body_parts.append("</p>")
        if i < len(paragraphs) - 1:
            body_parts.append(f'<break time="{PAUSE_PARAGRAPH_MS}ms"/>')

    body = "".join(body_parts)
    return (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        'xml:lang="ru-RU">'
        f'<voice name="{config.azure_tts_voice}">'
        f'<prosody rate="{config.azure_tts_rate or "-8%"}">'
        f'{body}'
        '</prosody>'
        '</voice>'
        '</speak>'
    )


async def synthesize_azure(text: str, out_path: Path, cache_dir: Path, digest: str) -> bool:
    """Главная entry point Azure-провайдера.

    Синтезирует речь в out_path. Если текст длинный — дробит на куски,
    озвучивает параллельно, склеивает через ffmpeg (используем общую
    функцию _ffmpeg_concat_mp3 из tts.py).

    Возвращает True при успехе, False при любой ошибке (тогда tts.py
    переключится на fallback yandex / elevenlabs).
    """
    chunks = _split_for_azure(text)
    logger.info("Azure TTS: %d символов → %d кусков (voice=%s)",
                len(text), len(chunks), config.azure_tts_voice)

    if len(chunks) == 1:
        audio = await _synthesize_chunk(chunks[0])
        if not audio:
            return False
        out_path.write_bytes(audio)
        return True

    # Длинный текст: параллельно озвучиваем и склеиваем
    audio_results = await asyncio.gather(
        *[_synthesize_chunk(c) for c in chunks],
        return_exceptions=True,
    )
    parts: list[Path] = []
    for i, result in enumerate(audio_results):
        if isinstance(result, Exception) or not result:
            logger.error("Azure TTS: кусок %d не озвучен", i)
            # Чистим уже сохранённые куски
            for p in parts:
                p.unlink(missing_ok=True)
            return False
        part_path = cache_dir / f"{digest}_azure_part{i}.mp3"
        part_path.write_bytes(result)
        parts.append(part_path)

    # Используем общую concat-функцию из tts.py
    from .tts import _ffmpeg_concat_mp3
    ok = await _ffmpeg_concat_mp3(parts, out_path)
    for p in parts:
        p.unlink(missing_ok=True)
    return ok
