"""Озвучка сказки.

Provider chain (выбор через TTS_PROVIDER):
  azure (default, premium) → yandex (fallback) → elevenlabs (last resort)

  • Azure Neural — ru-RU-SvetlanaNeural с style=affectionate, ~7 ₽/сказка.
    Заметно живее Яндекса, есть «любящая» интонация. Логика — tts_azure.py.
  • Yandex SpeechKit — alena/good, ~1.5 ₽/сказка. Хороший плановый fallback.
  • ElevenLabs Turbo v2.5 — последний шанс, ~20 ₽/сказка, держим для аварий.

После TTS — микс с фоновой колыбельной музыкой через ffmpeg (loudnorm,
fade-in/out). Эта часть не зависит от TTS-провайдера.

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
from ..utils.text import strip_emo_markers

logger = logging.getLogger(__name__)

ELEVENLABS_BASE = "https://api.elevenlabs.io/v1/text-to-speech"
YANDEX_TTS_URL = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"

# Yandex SpeechKit v1 имеет лимит 5000 символов за запрос.
# Сказка 500-700 слов = ~3500-5000 символов — на границе.
# Чтоб не упереться — разбиваем по абзацам с запасом.
# Дополнительный запас на SSML-разметку: <p><s>...</s></p> добавляет
# ~15-20% символов, поэтому режем чанки агрессивнее.
YANDEX_MAX_CHARS_PER_REQUEST = 3500

# Громкость фоновой музыки ОТНОСИТЕЛЬНО голоса (в децибелах).
# С момента нормализации обоих стримов через loudnorm это теперь
# истинно-относительное число, не зависит от исходной громкости трека:
# голос всегда на -14 LUFS, фон — на (-14 + BACKGROUND_DB) LUFS.
#
# Перцептивная шкала:
#  -6 dB  = музыка громкая, чувствуется наравне с голосом (можно для эпика)
#  -9 dB  = музыка слышна как «атмосфера сцены», но голос явно главный (норма)
#  -12 dB = тихий фон, едва ощутимый
#  -15 dB = очень тихо, на грани восприятия
BACKGROUND_DB = -9

# Fade-in голоса — короткий мягкий вход (после интро музыки)
FADE_IN_SEC = 2
# ВАЖНО: голос НЕ ЗАТУХАЕТ в конце. Раньше был fade-out, но из-за него
# финальная фраза «Сладких снов тебе, имя» звучала роботизированно — она
# попадала в зону затухания. Теперь голос идёт на полной громкости
# до самого последнего слова, а угасает только фоновая музыка.

# Замедление голоса (ffmpeg atempo). 1.0 = обычный темп, 0.92 = на 8% медленнее.
# Для сказок на ночь — медленнее лучше, ребёнок успевает «вжиться» в фразу.
VOICE_TEMPO = 0.92

# Музыкальный «театральный занавес»:
# - INTRO: ровно столько секунд музыки звучит ДО первого слова рассказчика.
#   Это даёт «настройку» — ребёнок успевает устроиться, мама нажать play,
#   мозг переключается в режим истории. 5 сек — комфортный минимум.
# - OUTRO: столько музыки звучит ПОСЛЕ финальной фразы. Не обрубается, а
#   плавно гаснет — ребёнок остаётся в тишине, готовой ко сну.
# Обе доли выполняются как fade-in/out у музыки, голос НЕ затрагивается.
MUSIC_INTRO_SEC = 5
MUSIC_OUTRO_SEC = 5


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

    Структура итогового аудио:
      [0..INTRO]                        — только музыка, fade-in
      [INTRO..INTRO+voice]              — музыка + голос рассказчика
      [INTRO+voice..INTRO+voice+OUTRO]  — только музыка, fade-out
      ─────────────────────────────────
      total = INTRO + voice + OUTRO

    Это даёт «театральный занавес» вместо резкого обрубания: ребёнок
    устраивается под музыку → слушает сказку → засыпает под угасающую
    мелодию. Без интро/аутро микс ощущался техничным, как ассистент
    зачитал документ. С ними — как мама села рядом и включила пластинку.
    """
    raw_duration = await _get_audio_duration(voice_path)
    # ВАЖНО: учитываем atempo=0.92. ffmpeg внутри замедлит голос, поэтому
    # реальная длительность голоса в миксе = raw_duration / VOICE_TEMPO.
    voice_duration_in_mix = raw_duration / VOICE_TEMPO if raw_duration > 0 else raw_duration

    # Для совсем коротких тестов (<5 сек) полный интро/аутро бессмыслен —
    # используем компактные fades, чтобы тест не растягивался на 15 секунд.
    if voice_duration_in_mix < 5:
        intro = 0.5
        outro = 1.0
    else:
        intro = MUSIC_INTRO_SEC
        outro = MUSIC_OUTRO_SEC

    intro_ms = int(intro * 1000)
    total_dur = voice_duration_in_mix + intro + outro

    cmd = [
        "ffmpeg", "-y",
        "-i", str(voice_path),
        "-stream_loop", "-1", "-i", str(ambient_path),
        "-filter_complex",
        # ─────────────── Голос ───────────────
        # 1) atempo — замедляем на ~8% для уютного сказочного темпа
        # 2) loudnorm — выравниваем громкость до -14 LUFS (стандарт стримингов)
        # 3) afade=t=in — короткий мягкий вход (2 сек), уже ВНУТРИ голоса
        # 4) adelay — сдвигаем голос на INTRO мс вперёд (тишина в начале,
        #    в которой звучит только музыка-интро)
        # 5) apad pad_dur=OUTRO — в конце добавляем OUTRO сек тишины,
        #    чтобы микс продлился ещё на OUTRO сек после последнего слова
        # БЕЗ fade-out на голосе! Финальная фраза должна звучать целиком.
        f"[0:a]atempo={VOICE_TEMPO},"
        f"loudnorm=I=-14:TP=-1.5:LRA=11,"
        f"afade=t=in:st=0:d={FADE_IN_SEC},"
        f"adelay={intro_ms}|{intro_ms},"
        f"apad=pad_dur={outro}[voice];"
        # ─────────────── Музыка ───────────────
        # 1) loudnorm — нормализуем фон к -14 LUFS как голос
        # 2) volume={BACKGROUND_DB}dB — относительное понижение под голос
        # 3) atrim duration=total — обрезаем зацикленный поток до длины микса
        # 4) afade in длиной INTRO — музыка плавно появляется за первые INTRO сек
        # 5) afade out длиной OUTRO — музыка плавно гаснет в финале OUTRO сек
        f"[1:a]loudnorm=I=-14:TP=-1.5:LRA=11,"
        f"volume={BACKGROUND_DB}dB,"
        f"atrim=duration={total_dur},"
        f"afade=t=in:st=0:d={intro},"
        f"afade=t=out:st={total_dur - outro}:d={outro}[bg];"
        # ─────────────── Микс ───────────────
        # duration=first — длительность по голосу (он уже длиной total_dur
        # благодаря adelay + apad), музыка обрезана до того же
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
    logger.info("Mix: voice=%.1fs (raw=%.1fs), intro=%.1fs, outro=%.1fs, total=%.1fs",
                voice_duration_in_mix, raw_duration, intro, outro, total_dur)
    return True


def _split_text_for_yandex(text: str, max_chars: int = YANDEX_MAX_CHARS_PER_REQUEST) -> list[str]:
    """Делит текст на куски по абзацам, не превышая max_chars в каждом.
    Сохраняет целостность предложений и абзацев."""
    if len(text) <= max_chars:
        return [text]
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current = ""
    for p in paragraphs:
        if len(current) + len(p) + 2 > max_chars:
            if current:
                chunks.append(current.strip())
            # Если один абзац > max_chars (бывает редко) — режем по предложениям
            if len(p) > max_chars:
                sentences = p.replace("! ", "!\n").replace("? ", "?\n").replace(". ", ".\n").split("\n")
                buf = ""
                for s in sentences:
                    if len(buf) + len(s) > max_chars:
                        if buf:
                            chunks.append(buf.strip())
                        buf = s
                    else:
                        buf = buf + " " + s if buf else s
                if buf:
                    current = buf
                else:
                    current = ""
            else:
                current = p
        else:
            current = current + "\n\n" + p if current else p
    if current:
        chunks.append(current.strip())
    return chunks


def _escape_ssml(s: str) -> str:
    """Экранирует спецсимволы XML для безопасной вставки в SSML."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# Регэксп для разделения текста на предложения. Учитываем многоточие («…», «...»),
# чтобы оно НЕ считалось концом предложения, а оставалось внутри.
import re as _re

_SENTENCE_SPLIT_RE = _re.compile(r'(?<=[.!?])\s+(?=[А-ЯA-Z—«"])')


def _to_ssml(text: str) -> str:
    """Превращает сырой текст сказки в SSML-разметку для Yandex SpeechKit.

    Зачем: голос alena без разметки читает как новости — ровно, без пауз,
    без вдоха между фразами. С SSML появляются:
      - паузы между абзацами (драматический ритм рассказчика)
      - короткие паузы между предложениями (даёт «дышать»)
      - явная пауза перед финальной фразой («Сладких снов»)
        делает её акцентом, а не последним словом нон-стопа.

    Поддерживаемые Yandex v1 теги:
      <speak>     — корневой
      <p>...</p>  — абзац (длинная пауза вокруг)
      <s>...</s>  — предложение (короткая пауза в конце)
      <break time="..."/> — явная пауза
    Тегов <emphasis>, <prosody> у Yandex v1 НЕТ — выразительность достигается
    исключительно расстановкой пауз и базовой эмоцией голоса (emotion=good).
    """
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return f"<speak>{_escape_ssml(text)}</speak>"

    parts: list[str] = ["<speak>"]
    for i, para in enumerate(paragraphs):
        sentences = _SENTENCE_SPLIT_RE.split(para)
        parts.append("<p>")
        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue
            parts.append(f"<s>{_escape_ssml(sent)}</s>")
        parts.append("</p>")
        # Дополнительная пауза между абзацами — кроме последнего.
        # 500мс — достаточно «вдохнуть», не достаточно чтобы заснул на середине.
        if i < len(paragraphs) - 1:
            parts.append('<break time="500ms"/>')
    parts.append("</speak>")
    return "".join(parts)


async def _synthesize_yandex_chunk(text: str) -> bytes | None:
    """Один запрос к Yandex SpeechKit. Возвращает байты mp3 или None при ошибке.

    Отправляем текст как SSML — это даёт паузы между абзацами и предложениями,
    голос звучит как рассказчик у кровати, а не как диктор новостей.
    """
    ssml = _to_ssml(text)
    data = {
        "ssml": ssml,
        "voice": config.yandex_tts_voice,
        "emotion": config.yandex_tts_emotion,
        "speed": str(config.yandex_tts_speed),
        "format": "mp3",
        "folderId": config.yandex_folder_id,
    }
    headers = {"Authorization": f"Api-Key {config.yandex_api_key}"}
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(YANDEX_TTS_URL, data=data, headers=headers)
        if resp.status_code != 200:
            logger.error("Yandex SpeechKit error %s: %s", resp.status_code, resp.text[:400])
            # На случай если SSML вызвал 400 (битый тег) — ретрай с plain text
            if resp.status_code == 400:
                logger.warning("SSML отверг — пробую plain text fallback")
                data.pop("ssml", None)
                data["text"] = text
                resp = await client.post(YANDEX_TTS_URL, data=data, headers=headers)
                if resp.status_code == 200:
                    return resp.content
                logger.error("Plain text fallback тоже упал: %s %s",
                             resp.status_code, resp.text[:300])
            return None
        return resp.content


async def _ffmpeg_concat_mp3(parts: list[Path], out_path: Path) -> bool:
    """Склеивает несколько mp3 в один через ffmpeg concat filter.
    Используется когда сказка длинная и пришлось разбить на куски."""
    if len(parts) == 1:
        parts[0].replace(out_path)
        return True
    inputs: list[str] = []
    for p in parts:
        inputs.extend(["-i", str(p)])
    filter_str = "".join(f"[{i}:a]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=0:a=1[a]"
    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_str,
        "-map", "[a]",
        "-c:a", "libmp3lame", "-b:a", "128k",
        str(out_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.error("ffmpeg concat failed: %s", stderr.decode(errors="replace")[-400:])
        return False
    return True


async def _synthesize_yandex(text: str, out_path: Path, cache_dir: Path, digest: str) -> bool:
    """Yandex TTS с авто-разбиением длинных текстов на куски и склейкой через ffmpeg.
    Записывает результат в out_path. Возвращает True при успехе."""
    if not config.yandex_api_key or not config.yandex_folder_id:
        logger.warning("YANDEX_API_KEY/YANDEX_FOLDER_ID не заданы")
        return False

    chunks = _split_text_for_yandex(text)
    logger.info("Yandex TTS: %d символов → %d кусков", len(text), len(chunks))

    if len(chunks) == 1:
        audio = await _synthesize_yandex_chunk(chunks[0])
        if not audio:
            return False
        out_path.write_bytes(audio)
        return True

    # Длинная сказка — несколько кусков параллельно
    parts: list[Path] = []
    audio_results = await asyncio.gather(
        *[_synthesize_yandex_chunk(c) for c in chunks],
        return_exceptions=True,
    )
    for i, result in enumerate(audio_results):
        if isinstance(result, Exception) or not result:
            logger.error("Yandex TTS: кусок %d не озвучен", i)
            return False
        part_path = cache_dir / f"{digest}_part{i}.mp3"
        part_path.write_bytes(result)
        parts.append(part_path)

    ok = await _ffmpeg_concat_mp3(parts, out_path)
    # Чистим временные куски
    for p in parts:
        p.unlink(missing_ok=True)
    return ok


async def _synthesize_elevenlabs(text: str, out_path: Path) -> bool:
    """ElevenLabs TTS — fallback. Записывает результат в out_path."""
    if not config.elevenlabs_api_key:
        logger.warning("ELEVENLABS_API_KEY не задан — fallback недоступен")
        return False
    url = f"{ELEVENLABS_BASE}/{config.elevenlabs_voice_id}"
    headers = {
        "xi-api-key": config.elevenlabs_api_key,
        "accept": "audio/mpeg",
        "content-type": "application/json",
    }
    payload = {
        "text": text,
        "model_id": config.elevenlabs_model,
        "voice_settings": {
            "stability": 0.45,
            "similarity_boost": 0.75,
            "style": 0.5,
            "use_speaker_boost": True,
        },
        "output_format": "mp3_44100_128",
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            logger.error("ElevenLabs error %s: %s", resp.status_code, resp.text[:200])
            return False
        out_path.write_bytes(resp.content)
        return True


async def synthesize_speech(text: str, voice_id: str | None = None) -> Path | None:
    """Озвучивает текст, миксует с фоновой колыбельной (если есть треки).
    Кэширует итоговый mp3 по хэшу. Возвращает путь к файлу или None если отключено.

    Provider chain (выбирается через config.tts_provider):
      - azure (default) → yandex → elevenlabs
      - yandex          → elevenlabs
      - elevenlabs      → только сам

    Если primary упал, попробуем следующего в цепочке. Логи покажут какой
    в итоге сработал — пригодится для алертов.
    """
    text = strip_emo_markers(text)

    # Кэш-ключ привязан к провайдеру и его настройкам — смена голоса даст новый кэш.
    # Маркер "intro5out5" — фиксированный 5сек интро/аутро музыки.
    # Каждый раз когда меняется структура микса — поднимаем маркер, чтобы
    # старые «обрубленные» миксы не реюзались как готовый продукт.
    provider = config.tts_provider
    if provider == "azure":
        cache_key = (
            f"azure-v1-intro5out5:{config.azure_tts_voice}:"
            f"{config.azure_tts_style}:{config.azure_tts_rate}:{text}"
        )
    elif provider == "yandex":
        cache_key = (
            f"yandex-ssml-v2-intro5out5:{config.yandex_tts_voice}:"
            f"{config.yandex_tts_emotion}:{config.yandex_tts_speed}:{text}"
        )
    else:
        cache_key = f"el:{config.elevenlabs_voice_id}:{config.elevenlabs_model}:{text}"
    digest = hashlib.sha256(cache_key.encode()).hexdigest()[:32]

    cache_dir = Path(config.audio_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    out_mixed = cache_dir / f"{digest}.mp3"
    if out_mixed.exists():
        return out_mixed

    # Шаг 1: получаем голос. Provider chain с fallback.
    voice_path = cache_dir / f"{digest}_voice.mp3"
    actual_provider: str | None = None
    if not voice_path.exists():
        ok = False
        if provider == "azure":
            # Импорт здесь чтобы избежать циклической зависимости (azure → tts)
            from .tts_azure import synthesize_azure
            ok = await synthesize_azure(text, voice_path, cache_dir, digest)
            if ok:
                actual_provider = "azure"
            elif config.yandex_api_key and config.yandex_folder_id:
                logger.warning("Azure TTS упал — пробую Yandex fallback")
                ok = await _synthesize_yandex(text, voice_path, cache_dir, digest)
                if ok:
                    actual_provider = "yandex"
            if not ok and config.elevenlabs_api_key:
                logger.warning("Yandex тоже упал — пробую ElevenLabs last-resort")
                ok = await _synthesize_elevenlabs(text, voice_path)
                if ok:
                    actual_provider = "elevenlabs"
        elif provider == "yandex":
            ok = await _synthesize_yandex(text, voice_path, cache_dir, digest)
            if ok:
                actual_provider = "yandex"
            elif config.elevenlabs_api_key:
                logger.warning("Yandex TTS упал — пробую ElevenLabs fallback")
                ok = await _synthesize_elevenlabs(text, voice_path)
                if ok:
                    actual_provider = "elevenlabs"
        else:
            ok = await _synthesize_elevenlabs(text, voice_path)
            if ok:
                actual_provider = "elevenlabs"
        if not ok:
            logger.error("Все TTS провайдеры упали (primary=%s)", provider)
            return None
        if actual_provider and actual_provider != provider:
            logger.warning("TTS fallback: ожидался %s, отработал %s", provider, actual_provider)

    # Шаг 2: пытаемся смикшировать с фоном
    ambient = _pick_ambient()
    if not ambient:
        logger.info("Фоновая папка пустая — отдаю голую озвучку.")
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

    voice_path.unlink(missing_ok=True)
    logger.info("Сказка озвучена (%s) с фоном %s",
                actual_provider or config.tts_provider, ambient.name)
    return out_mixed
