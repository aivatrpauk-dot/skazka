"""Озвучка сказки через ElevenLabs Turbo v2.5. Только для платных."""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

import httpx

from ..config import config

logger = logging.getLogger(__name__)

ELEVENLABS_BASE = "https://api.elevenlabs.io/v1/text-to-speech"


async def synthesize_speech(text: str, voice_id: str | None = None) -> Path | None:
    """Озвучивает текст. Кэширует mp3 по хэшу. Возвращает путь к файлу или None если выключено."""
    if not config.elevenlabs_api_key:
        logger.warning("ELEVENLABS_API_KEY не задан — пропускаю озвучку")
        return None

    voice = voice_id or config.elevenlabs_voice_id
    digest = hashlib.sha256(f"{voice}:{config.elevenlabs_model}:{text}".encode()).hexdigest()[:32]
    cache_dir = Path(config.audio_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / f"{digest}.mp3"
    if out.exists():
        return out

    url = f"{ELEVENLABS_BASE}/{voice}"
    headers = {
        "xi-api-key": config.elevenlabs_api_key,
        "accept": "audio/mpeg",
        "content-type": "application/json",
    }
    payload = {
        "text": text,
        "model_id": config.elevenlabs_model,
        "voice_settings": {
            "stability": 0.55,
            "similarity_boost": 0.75,
            "style": 0.25,
            "use_speaker_boost": True,
        },
        "output_format": "mp3_44100_128",
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            logger.error("ElevenLabs error %s: %s", resp.status_code, resp.text[:200])
            return None
        out.write_bytes(resp.content)
    return out
