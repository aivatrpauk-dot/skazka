"""Озвучка сказки через ElevenLabs Turbo v2.5 + микс с фоновой колыбельной музыкой.

Финальный mp3 = голос диктора + случайный ambient-трек на тихом фоне.

Это поднимает воспринимаемое качество с уровня «AI-озвучка» до уровня
аудиосказок в YouTube/Telegram-каналах типа @skazkiaudio. Реализация:
1. ElevenLabs генерит голос (.voice.mp3)
2. Из cache/ambient/ берётся случайный mp3
3. ffmpeg микширует: голос 0 dB + фон −18 dB, фон зацикливается до длины голоса,
   на старте 2 сек fade-in, на конце 3 сек fade-out
4. Итоговый файл — один mp3, как раньше

Если cache/ambient/ пустая или ffmpeg недоступен — отдаём голую озвучку
без фона (graceful fallback)."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import shutil
from pathlib import Path

import httpx

from ..config import config

logger = logging.getLogger(__name__)

ELEVENLABS_BASE = "https://api.elevenlabs.io/v1/text-to-speech"

# Громкость фоновой музыки относительно голоса (в децибелах).
# -22 dB = музыка очень тихим фоном, голос явно главный (перцептивно ~в 2 раза
# тише голоса). Хорошо для сказок-засыпашек: ребёнок концентрируется на голосе,
# музыка работает как уютная атмосфера, не отвлекая.
# Если хочется чуть громче (для бодрых сказок) — поставь -18 или -16.
BACKGROUND_DB = -22

# Fade-in голоса — короткий мягкий вход
FADE_IN_SEC = 2
# Fade-out голоса — на самом конце, чтобы не обрывалось резко
FADE_OUT_SEC = 3

# Музыка плавно угасает на этой ДОЛЕ от общей длительности.
# 0.5 = последняя половина сказки музыка постепенно уходит в тишину.
# 0.4 = угасание начинается ближе к концу (на 60% длительности).
# 0.6 = угасание начинается уже на середине (40%) — для совсем уютных сказок.
MUSIC_FADE_OUT_FRACTION = 0.5


def _ambient_dir() -> Path:
    """cache/ambient/ — папка с фоновыми треками. Лежит рядом с cache/audio/."""
    return Path(config.audio_cache_dir).parent / "ambient"


def _pick_ambient() -> Path | None:
    """Случайный mp3 из cache/ambient/. None если папка пустая."""
    d = _ambient_dir()
    if not d.exists():
        return None
    tracks = list(d.glob("*.mp3"))
    if not tracks:
        return None
    return random.choice(tracks)


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


async def _get_audio_duration(path: Path) -> float:
    """Возвращает длительность аудиофайла в секундах через ffprobe.
    Если не удалось определить — возвращает 0."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    try:
        return float(stdout.decode().strip())
    except (ValueError, TypeError):
        return 0.0


async def _mix_with_ambient(voice_path: Path, ambient_path: Path, out_path: Path) -> bool:
    """Микшируем голос + ambient через ffmpeg. Возвращает True если успешно.

    Структура:
    - [0:a] голос: 2 сек fade-in в начале + 3 сек fade-out в конце
    - [1:a] музыка: понижаем до BACKGROUND_DB, fade-in на старте,
            ДОЛГИЙ fade-out на последней половине сказки (плавно уходит в тишину
            — ребёнок засыпает под угасающую музыку)
    - amix: микс, длительность = длина голоса
    """
    duration = await _get_audio_duration(voice_path)
    if duration < 5:
        # Слишком короткая сказка (демо/тест) — простой микс без долгого fade
        music_fade_in = 0.5
        music_fade_out_start = max(0, duration - 2)
        music_fade_out_duration = 2
    else:
        # Музыка плавно появляется за 3 секунды и УГАСАЕТ на последней половине
        # длительности. Если сказка 4 минуты — последние 2 минуты музыка
        # постепенно стихает до нуля.
        music_fade_in = 3
        music_fade_out_duration = duration * MUSIC_FADE_OUT_FRACTION
        music_fade_out_start = duration - music_fade_out_duration

    voice_fade_out_start = max(0, duration - FADE_OUT_SEC)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(voice_path),
        "-stream_loop", "-1", "-i", str(ambient_path),
        "-filter_complex",
        # Голос: короткий fade-in + fade-out в самом конце
        f"[0:a]afade=t=in:st=0:d={FADE_IN_SEC},"
        f"afade=t=out:st={voice_fade_out_start}:d={FADE_OUT_SEC}[voice];"
        # Музыка: понижаем громкость, плавный вход, долгое угасание
        f"[1:a]volume={BACKGROUND_DB}dB,"
        f"afade=t=in:st=0:d={music_fade_in},"
        f"afade=t=out:st={music_fade_out_start}:d={music_fade_out_duration}[bg];"
        # Микс
        f"[voice][bg]amix=inputs=2:duration=first:dropout_transition=0",
        "-c:a", "libmp3lame", "-b:a", "128k",
        str(out_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.error("ffmpeg mix failed (code=%s): %s",
                     proc.returncode, stderr.decode(errors="replace")[-500:])
        return False
    logger.info("Mix: duration=%.1fs, music_fade_out=%.1fs from t=%.1fs",
                duration, music_fade_out_duration, music_fade_out_start)
    return True


async def synthesize_speech(text: str, voice_id: str | None = None) -> Path | None:
    """Озвучивает текст, миксует с фоновой колыбельной (если есть треки).
    Кэширует итоговый mp3 по хэшу. Возвращает путь к файлу или None если отключено."""
    if not config.elevenlabs_api_key:
        logger.warning("ELEVENLABS_API_KEY не задан — пропускаю озвучку")
        return None

    voice = voice_id or config.elevenlabs_voice_id
    digest = hashlib.sha256(f"{voice}:{config.elevenlabs_model}:{text}".encode()).hexdigest()[:32]
    cache_dir = Path(config.audio_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Финальный микс кэшируется отдельно от голой озвучки,
    # чтобы один и тот же текст давал стабильный результат
    # (для конкретной выбранной фоновой дорожки).
    out_mixed = cache_dir / f"{digest}.mp3"
    if out_mixed.exists():
        return out_mixed

    # Шаг 1: получаем голос от ElevenLabs (в отдельный файл, чтобы потом смикшировать)
    voice_path = cache_dir / f"{digest}_voice.mp3"
    if not voice_path.exists():
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
            voice_path.write_bytes(resp.content)

    # Шаг 2: пытаемся смикшировать с фоном. Если что-то не так — возвращаем чистый голос.
    ambient = _pick_ambient()
    if not ambient:
        logger.info("Фоновая папка пустая — отдаю голую озвучку. "
                    "Добавь mp3 в %s чтобы включить микс.", _ambient_dir())
        # Переименовываем voice → out, чтобы кэширование сработало
        voice_path.replace(out_mixed)
        return out_mixed

    if not _ffmpeg_available():
        logger.warning("ffmpeg не установлен — отдаю голую озвучку без фона")
        voice_path.replace(out_mixed)
        return out_mixed

    ok = await _mix_with_ambient(voice_path, ambient, out_mixed)
    if not ok:
        logger.warning("Не смикшировалось — отдаю голую озвучку без фона")
        voice_path.replace(out_mixed)
        return out_mixed

    # Микс готов — удаляем голую копию, оставляем только итог
    voice_path.unlink(missing_ok=True)
    logger.info("Сказка озвучена с фоном %s", ambient.name)
    return out_mixed
