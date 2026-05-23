"""Генерация фоновой инструментальной музыки для сказок через Suno V5 (kie.ai).

Сценарий использования:
1. Админ командой /generate_ambient <N> запускает разовую генерацию N треков
2. Треки сохраняются в cache/ambient/ (туда же где раньше лежали ручные mp3)
3. tts.py при микшировании сказки случайно берёт один из них через _pick_ambient()

Стоимость: ~11 ₽ за трек (Suno V5 на kie.ai). 30 треков = ~330 ₽ разово.
После этого пул переиспользуется бесконечно. Никакого Suno-расхода на каждую сказку.

Промпты подобраны для детской колыбельной эстетики: piano + music box + harp,
спокойно, без вокала, slow tempo. 12 разных стилей-вариаций для разнообразия пула.
"""
from __future__ import annotations

import asyncio
import logging
import random
import uuid
from pathlib import Path

import httpx

from ..config import config

logger = logging.getLogger(__name__)

KIE_BASE = "https://api.kie.ai/api/v1"
POLL_INTERVAL_SEC = 5
MAX_WAIT_SEC = 240  # Suno V5 обычно укладывается в 60-120 сек


class BgMusicGenError(Exception):
    pass


# ─────────────────────── Стилистические пресеты ───────────────────────
# 12 вариаций промптов для разнообразия пула. Все — спокойные,
# инструментальные, для засыпания ребёнка.
LULLABY_STYLES = [
    {
        "title": "Soft Piano Lullaby",
        "prompt": "soft gentle piano lullaby for children, slow calming melody, peaceful and dreamy, perfect for falling asleep",
        "style": "piano lullaby, calm, sleepy, instrumental",
    },
    {
        "title": "Music Box Magic",
        "prompt": "delicate music box melody, twinkling fairy tale theme, magical and gentle, slow tempo, lullaby for children",
        "style": "music box, fairy tale, calm, instrumental",
    },
    {
        "title": "Harp Dreams",
        "prompt": "soothing harp arpeggios, dreamy ambient lullaby for kids before sleep, soft and peaceful",
        "style": "harp, ambient lullaby, peaceful, instrumental",
    },
    {
        "title": "Glockenspiel Stars",
        "prompt": "twinkling glockenspiel and bells, starlight melody, magical bedtime atmosphere, slow and soft",
        "style": "glockenspiel, bells, magical, calm, instrumental",
    },
    {
        "title": "Warm Strings",
        "prompt": "warm gentle strings, soothing orchestral lullaby, slow and dreamy, perfect for bedtime",
        "style": "strings, orchestral lullaby, slow, instrumental",
    },
    {
        "title": "Celesta Snow",
        "prompt": "soft celesta and chimes, winter night lullaby, peaceful magical atmosphere, very slow tempo",
        "style": "celesta, chimes, winter, peaceful, instrumental",
    },
    {
        "title": "Acoustic Cradle",
        "prompt": "gentle acoustic guitar fingerpicking, soft warm bedtime melody, calm and intimate, instrumental",
        "style": "acoustic guitar, fingerpicking, warm, instrumental",
    },
    {
        "title": "Dreamy Pads",
        "prompt": "soft ambient synth pads, dreamy floating melody, sleepy and warm, child-friendly bedtime music",
        "style": "ambient synth, dreamy, sleepy, instrumental",
    },
    {
        "title": "Twinkle Reverie",
        "prompt": "soft piano with twinkling glockenspiel accents, child lullaby, very gentle and slow, peaceful",
        "style": "piano, glockenspiel, lullaby, calm, instrumental",
    },
    {
        "title": "Forest Sleep",
        "prompt": "gentle nature-inspired melody, soft wooden flute and harp, calm forest bedtime atmosphere, slow",
        "style": "flute, harp, nature, calm, instrumental",
    },
    {
        "title": "Moon Cradle",
        "prompt": "slow melodic music box with soft strings underneath, moonlit bedtime lullaby, peaceful and dreamy",
        "style": "music box, strings, lullaby, instrumental",
    },
    {
        "title": "Sleepy Bells",
        "prompt": "soft chimes and warm piano, magical child bedtime melody, very slow tempo, comforting",
        "style": "chimes, piano, magical, slow, instrumental",
    },
]


def _ambient_dir() -> Path:
    """cache/ambient/ — та же папка что использует tts.py для микса."""
    return Path(config.audio_cache_dir).parent / "ambient"


async def _submit_task(payload: dict, headers: dict) -> str:
    """Отправляет задачу в kie.ai, возвращает taskId."""
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(f"{KIE_BASE}/generate", json=payload, headers=headers)
        if resp.status_code >= 400:
            raise BgMusicGenError(f"kie.ai вернул {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if data.get("code") != 200:
            raise BgMusicGenError(f"kie.ai: code={data.get('code')}, msg={data.get('msg')}")
        task_id = (data.get("data") or {}).get("taskId")
        if not task_id:
            raise BgMusicGenError(f"kie.ai не вернул taskId: {resp.text[:300]}")
        return task_id


async def _poll_task(task_id: str, headers: dict) -> list[dict]:
    """Поллит статус задачи до завершения. Возвращает список клипов с audioUrl."""
    waited = 0
    while waited < MAX_WAIT_SEC:
        await asyncio.sleep(POLL_INTERVAL_SEC)
        waited += POLL_INTERVAL_SEC
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{KIE_BASE}/generate/record-info",
                params={"taskId": task_id},
                headers=headers,
            )
            if resp.status_code >= 400:
                continue
            data = resp.json()
            if data.get("code") != 200:
                continue
            tdata = data.get("data", {})
            status = (tdata.get("status") or "").upper()
            if status in ("SUCCESS", "COMPLETE", "COMPLETED"):
                response = tdata.get("response") or {}
                clips = response.get("sunoData") or response.get("data") or []
                if not clips:
                    raise BgMusicGenError("Suno completed но sunoData пустой")
                return clips
            if status in ("FAILED", "ERROR", "CANCELLED", "GENERATE_AUDIO_FAILED"):
                err = tdata.get("errorMessage") or tdata.get("error") or status
                raise BgMusicGenError(f"Suno failed: {err}")
    raise BgMusicGenError(f"Suno таймаут после {MAX_WAIT_SEC}s")


async def _download_clip(clip: dict, out_path: Path) -> bool:
    """Скачивает audioUrl клипа в локальный файл."""
    audio_url = (
        clip.get("audioUrl")
        or clip.get("sourceAudioUrl")
        or clip.get("streamAudioUrl")
    )
    if not audio_url:
        logger.warning("Suno clip без audioUrl: %s", clip)
        return False
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        resp = await client.get(audio_url)
        if resp.status_code != 200:
            logger.warning("Не скачался audio: %s", resp.status_code)
            return False
        out_path.write_bytes(resp.content)
        return True


async def generate_one_bg_track(style_idx: int | None = None) -> Path | None:
    """Генерирует один фоновый трек через Suno V5 и сохраняет в cache/ambient/.

    style_idx — индекс пресета из LULLABY_STYLES (если None — случайно).
    Возвращает путь к сохранённому файлу или None при ошибке.

    Suno V5 в customMode=True + instrumental=True даёт инструментал ~2-3 минуты.
    Иногда возвращает 2 клипа (Suno стандартно даёт 2 варианта) — оба сохраняем.
    """
    if not config.kie_api_key:
        raise BgMusicGenError("KIE_API_KEY не задан в .env")

    style = LULLABY_STYLES[style_idx] if style_idx is not None else random.choice(LULLABY_STYLES)

    headers = {
        "Authorization": f"Bearer {config.kie_api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "prompt": style["prompt"],
        "style": style["style"],
        "title": style["title"],
        "customMode": True,
        "instrumental": True,  # ← без вокала, только мелодия
        "model": config.suno_model,
        "callBackUrl": "https://api.kie.ai/api/v1/health",
    }

    task_id = await _submit_task(payload, headers)
    logger.info("Suno BG task: %s (style=%s)", task_id, style["title"])
    clips = await _poll_task(task_id, headers)

    ambient_dir = _ambient_dir()
    ambient_dir.mkdir(parents=True, exist_ok=True)

    saved_paths: list[Path] = []
    for clip in clips:
        # Уникальное имя — short uuid + slug стиля
        slug = style["title"].lower().replace(" ", "_")
        filename = f"{slug}_{uuid.uuid4().hex[:6]}.mp3"
        out_path = ambient_dir / filename
        ok = await _download_clip(clip, out_path)
        if ok:
            saved_paths.append(out_path)
            logger.info("BG track saved: %s (%.1f KB)", out_path.name, out_path.stat().st_size / 1024)

    if not saved_paths:
        raise BgMusicGenError("Ни один клип не скачался")

    # Возвращаем первый — но в папке остаются все
    return saved_paths[0]


async def generate_bg_pool(count: int) -> tuple[int, int]:
    """Генерирует пул из count треков. Возвращает (успешно, ошибок).

    Идёт последовательно, чтобы не упереться в rate limit kie.ai.
    Каждый запрос даёт ~2 клипа от Suno — реальный размер пула может быть 2x.
    """
    succeeded = 0
    failed = 0
    for i in range(count):
        # Циклически перебираем стили чтоб пул был разнообразным
        style_idx = i % len(LULLABY_STYLES)
        try:
            await generate_one_bg_track(style_idx=style_idx)
            succeeded += 1
            logger.info("Pool progress: %d/%d", succeeded, count)
        except Exception as e:
            failed += 1
            logger.warning("Generate BG track %d failed: %s", i, e)
            # Не падаем — продолжаем пул
        # Небольшая пауза между запросами для kie.ai rate limit
        await asyncio.sleep(2)
    return succeeded, failed


def list_bg_tracks() -> list[Path]:
    """Возвращает список всех mp3 в cache/ambient/."""
    d = _ambient_dir()
    if not d.exists():
        return []
    return sorted(d.glob("*.mp3"))


def clear_bg_pool() -> int:
    """Удаляет все mp3 из cache/ambient/. Возвращает количество удалённых."""
    tracks = list_bg_tracks()
    for p in tracks:
        try:
            p.unlink()
        except Exception as e:
            logger.warning("Не удалился %s: %s", p, e)
    return len(tracks)
