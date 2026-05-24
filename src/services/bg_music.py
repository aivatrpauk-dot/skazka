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
# Кинематографическая «волшебная классика» в стиле саундтреков, которые
# любят И ДЕТИ И РОДИТЕЛИ. НЕ детская пиликалка, а Williams/Hisaishi/Tchaikovsky.
# Suno V5 в этих промптах генерит orchestral fairy-tale-style композиции,
# которые свои (по авторскому праву), но звучат «как из кино».
#
# 6 категорий вместо одной — каждая создаёт своё настроение чтения:
#   magical_waltz       — волшебный вальс (как Sugar Plum Fairy / M&M's под ёлкой)
#   mysterious_quest    — таинственное приключение (Hedwig's theme + Lord of the Rings dark)
#   cozy_shire          — уют и радость (Concerning Hobbits, Шир, Хоббиты счастливы)
#   soaring_wonder      — парящее чудо (Williams Jurassic, Howl's Moving Castle)
#   starlit_lullaby     — звёздная колыбельная (Casper's Lullaby, Clair de Lune)
#   whimsical_mischief  — игривая хитрость (M&M's подкрадываются, Amélie)
LULLABY_STYLES = [
    {
        "slug": "magical_waltz",
        "label_ru": "Волшебный вальс",
        "prompt": (
            "magical orchestral waltz in the style of Tchaikovsky Sugar Plum Fairy, "
            "celesta and pizzicato strings, enchanting fairy-tale atmosphere, "
            "elegant 3/4 time, whimsical and dreamy, John Williams scoring sensibility, "
            "perfect for reading a bedtime story aloud, instrumental, no vocals"
        ),
        "style": "magical orchestral waltz, celesta, strings, fairy tale, cinematic",
    },
    {
        "slug": "mysterious_quest",
        "label_ru": "Таинственное приключение",
        "prompt": (
            "mysterious magical orchestral theme in the style of Hedwig's Theme by John Williams, "
            "celesta arpeggios with soft strings, twinkling enchanted melody, "
            "wonder and curiosity, fantasy adventure cinematic score, "
            "slow and atmospheric, instrumental, no vocals"
        ),
        "style": "mysterious orchestral, celesta, fantasy theme, cinematic, magical",
    },
    {
        "slug": "cozy_shire",
        "label_ru": "Уютная радость",
        "prompt": (
            "warm pastoral folk orchestral piece in the style of Concerning Hobbits "
            "by Howard Shore, gentle Irish whistle and acoustic strings, "
            "cozy countryside atmosphere, joy and home, soft fiddle, "
            "perfect background for reading aloud, instrumental, no vocals"
        ),
        "style": "pastoral folk orchestral, whistle, fiddle, cozy, cinematic",
    },
    {
        "slug": "soaring_wonder",
        "label_ru": "Парящее чудо",
        "prompt": (
            "soaring magical orchestral piece in the style of Joe Hisaishi Merry-Go-Round of Life, "
            "warm piano with rising strings, sense of wonder and flight, "
            "Studio Ghibli atmosphere, gentle waltz that lifts the heart, "
            "instrumental cinematic score, no vocals"
        ),
        "style": "Ghibli-style orchestral, piano, strings, soaring, cinematic",
    },
    {
        "slug": "starlit_lullaby",
        "label_ru": "Звёздная колыбельная",
        "prompt": (
            "tender orchestral lullaby in the style of James Horner Casper's Lullaby "
            "and Debussy Clair de Lune, soft piano with delicate strings underneath, "
            "moonlit nighttime atmosphere, intimate and peaceful, very slow, "
            "perfect for ending a bedtime story, instrumental, no vocals"
        ),
        "style": "tender orchestral lullaby, piano, strings, moonlit, cinematic",
    },
    {
        "slug": "whimsical_mischief",
        "label_ru": "Хитрая шалость",
        "prompt": (
            "playful whimsical orchestral piece in the style of Yann Tiersen Comptine d'un autre été "
            "and Tchaikovsky's Dance of the Sugar Plum Fairy, light pizzicato strings "
            "and bouncy piano, sneaky tiptoeing character, charming and clever, "
            "instrumental cinematic, no vocals"
        ),
        "style": "whimsical orchestral, pizzicato, playful, cinematic, light",
    },
]


def _style_by_slug(slug: str) -> dict | None:
    """Возвращает стиль по slug или None если не найден."""
    for s in LULLABY_STYLES:
        if s["slug"] == slug:
            return s
    return None


def label_for_track(path: Path) -> str:
    """Возвращает человеко-читаемое название для трека по имени файла.

    Имя файла начинается со slug стиля (magical_waltz_xxx.mp3 → «Волшебный вальс»).
    Если slug не распознан — возвращает 'Фоновая музыка'.
    """
    name = path.stem.lower()
    for s in LULLABY_STYLES:
        if name.startswith(s["slug"]):
            return s["label_ru"]
    return "Фоновая музыка"


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
        "title": style["label_ru"],
        "customMode": True,
        "instrumental": True,  # ← без вокала, только мелодия
        "model": config.suno_model,
        "callBackUrl": "https://api.kie.ai/api/v1/health",
    }

    task_id = await _submit_task(payload, headers)
    logger.info("Suno BG task: %s (style=%s)", task_id, style["slug"])
    clips = await _poll_task(task_id, headers)

    ambient_dir = _ambient_dir()
    ambient_dir.mkdir(parents=True, exist_ok=True)

    saved_paths: list[Path] = []
    for clip in clips:
        # Имя файла: {slug}_{short_uuid}.mp3 — чтобы label_for_track мог
        # распознать стиль по началу имени и показать «Волшебный вальс» и т.п.
        filename = f"{style['slug']}_{uuid.uuid4().hex[:8]}.mp3"
        out_path = ambient_dir / filename
        ok = await _download_clip(clip, out_path)
        if ok:
            saved_paths.append(out_path)
            logger.info("BG track saved: %s (%.1f KB)", out_path.name, out_path.stat().st_size / 1024)

    if not saved_paths:
        raise BgMusicGenError("Ни один клип не скачался")

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


# ─────────────────────── Stitching: склейка 2-3 треков в один ───────────────────────
# Suno генерит ~2-3 минутные клипы, а под чтение сказки нужно 6-8 минут.
# Склеиваем 2-3 случайных трека через ffmpeg crossfade (плавный 2-сек переход),
# нормализуем громкость через loudnorm. Результат кэшируется по хэшу входных
# файлов — если те же 2 трека уже склеивались, отдаём готовый файл из кэша.

import hashlib
import shutil


def _stitched_dir() -> Path:
    """cache/ambient_stitched/ — папка для склеенных длинных треков."""
    return Path(config.audio_cache_dir).parent / "ambient_stitched"


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


async def _ffmpeg_stitch(tracks: list[Path], out_path: Path, crossfade_sec: int = 2) -> bool:
    """Склеивает N mp3 в один через ffmpeg acrossfade.

    Каждый соседний переход делается crossfade'ом (плавно затухает предыдущий
    и появляется следующий). Это устраняет «щелчок» на стыках.
    Перед склейкой каждый трек проходит loudnorm для выравнивания громкости.

    Возвращает True при успехе.
    """
    if len(tracks) < 1:
        return False
    if len(tracks) == 1:
        # Только один трек — просто копируем без перекодирования
        shutil.copyfile(tracks[0], out_path)
        return True

    # Собираем -i для каждого файла и filter_complex с acrossfade цепочкой
    inputs: list[str] = []
    for p in tracks:
        inputs.extend(["-i", str(p)])

    # Цепочка: [0:a][1:a] acrossfade=d=2 → [a01]; [a01][2:a] acrossfade=d=2 → [a012]
    # Перед каждым входом — loudnorm для нормализации.
    filter_parts: list[str] = []
    # Сначала нормализуем каждый вход
    for i in range(len(tracks)):
        filter_parts.append(f"[{i}:a]loudnorm=I=-16:TP=-1.5:LRA=11[n{i}]")
    # Затем строим acrossfade цепочку
    prev_label = "n0"
    for i in range(1, len(tracks)):
        out_label = f"a{i}"
        filter_parts.append(
            f"[{prev_label}][n{i}]acrossfade=d={crossfade_sec}:c1=tri:c2=tri[{out_label}]"
        )
        prev_label = out_label

    filter_str = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_str,
        "-map", f"[{prev_label}]",
        "-c:a", "libmp3lame", "-b:a", "160k",
        str(out_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.error("ffmpeg stitch failed: %s", stderr.decode(errors="replace")[-500:])
        return False
    return True


async def stitch_ambient(target_minutes: int = 7) -> tuple[Path, str] | None:
    """Делает склейку 2-3 случайных треков в один ~target_minutes минутный mp3.

    Возвращает (path, human_label) где human_label — что показать юзеру в Telegram:
    «Волшебный вальс и Парящее чудо», «Звёздная колыбельная», и т.п.

    Если в пуле меньше 2 треков — берёт сколько есть. Если пул пустой → None.
    Кэширует результат по хэшу набора файлов, чтоб не пересчитывать одну и ту же
    комбинацию.
    """
    tracks = list_bg_tracks()
    if not tracks:
        logger.warning("Stitch: cache/ambient/ пустой")
        return None

    if not _ffmpeg_available():
        logger.warning("Stitch: ffmpeg не найден — возвращаю случайный трек как есть")
        chosen = random.choice(tracks)
        return chosen, label_for_track(chosen)

    # Сколько треков сшивать: Suno даёт ~2-3 мин на клип, поэтому 3 = ~7-9 мин,
    # 2 = ~4-6 мин. Если target_minutes=7 (по дефолту), берём 3.
    n_to_pick = 3 if target_minutes >= 6 else 2
    n_to_pick = min(n_to_pick, len(tracks))

    chosen = random.sample(tracks, n_to_pick)

    # Кэш-ключ — отсортированные имена файлов (порядок не важен для кэша).
    cache_key = "|".join(sorted(p.name for p in chosen)) + f"|cf2-target{target_minutes}"
    digest = hashlib.sha256(cache_key.encode()).hexdigest()[:16]

    stitched_dir = _stitched_dir()
    stitched_dir.mkdir(parents=True, exist_ok=True)
    out_path = stitched_dir / f"stitch_{digest}.mp3"

    if not out_path.exists():
        logger.info("Stitch: склеиваю %d треков → %s", n_to_pick, out_path.name)
        ok = await _ffmpeg_stitch(chosen, out_path)
        if not ok:
            # Fallback — отдаём первый исходник без склейки
            logger.warning("Stitch failed — отдаю первый трек как есть")
            return chosen[0], label_for_track(chosen[0])
    else:
        logger.info("Stitch: cache hit для %s", out_path.name)

    # Лейбл — комбинация имён стилей.
    # «Волшебный вальс и Парящее чудо» / «Волшебный вальс, Уютная радость и Звёздная колыбельная»
    labels = [label_for_track(p) for p in chosen]
    # Уникальные в порядке появления
    seen: set[str] = set()
    unique_labels = []
    for l in labels:
        if l not in seen:
            unique_labels.append(l)
            seen.add(l)
    if len(unique_labels) == 1:
        human_label = unique_labels[0]
    elif len(unique_labels) == 2:
        human_label = f"{unique_labels[0]} и {unique_labels[1]}"
    else:
        human_label = ", ".join(unique_labels[:-1]) + f" и {unique_labels[-1]}"
    return out_path, human_label
