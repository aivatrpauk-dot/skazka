"""Генерация картинки-обложки.

Главный провайдер — FAL.AI, модель выбирается через IMAGE_MODEL:
  • recraft-v3   — заточен под книжную/детскую иллюстрацию, ~3.6 ₽ (рекомендуется).
                   Эстетика «акварельная книжка», лучший вариант для retention.
  • flux-pro-1.1 — фотогенично-иллюстративный, ~3.6 ₽
  • flux-dev     — заметно лучше Schnell по деталям, ~2.3 ₽
  • flux-schnell — дёшево и быстро, 0.3 ₽ (legacy default)

FusionBrain (Kandinsky 3.x) — legacy fallback, используется только если
FAL_KEY не задан, а FUSIONBRAIN_* заданы. Если ни один не настроен —
бот шлёт сказку без обложки (graceful degrade)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from pathlib import Path

import httpx

from ..config import config
from ..prompts import (
    FALLBACK_SCENE_TEMPLATE,
    THEME_TO_EN,
    random_image_style,
    stage_style,
)

logger = logging.getLogger(__name__)

FUSIONBRAIN_BASE = "https://api-key.fusionbrain.ai"


def _cache_path(prompt: str) -> Path:
    digest = hashlib.sha256(prompt.encode()).hexdigest()[:32]
    cache_dir = Path(config.image_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{digest}.png"


async def generate_cover(
    hero: str,
    theme_key: str,
    *,
    scene_description: str | None = None,
    stage: str | None = None,
) -> Path | None:
    """Генерирует иллюстрацию для PDF-книжки.

    scene_description — одно предложение по-английски из конкретной сцены сказки.
    stage — 'opening' | 'climax' | 'ending' для выбора stage-специфичного стиля.
            Если None — случайный стиль (legacy/одиночный вызов).
    """
    theme_en = THEME_TO_EN.get(theme_key, "kindness and warmth")
    if not scene_description:
        scene_description = FALLBACK_SCENE_TEMPLATE.format(hero=hero, theme_en=theme_en)

    # Stage-specific стиль (opening/climax/ending) или случайный для legacy.
    if stage:
        style_id, style_prompt = stage_style(stage)
    else:
        style_id, style_prompt = random_image_style()

    # Жёстко запрещаем любой текст на картинке. Recraft v3 особенно склонен
    # дорисовывать заголовки/имена/«The End» (он натренирован на book covers).
    # Опытным путём: запрет работает лучше в САМОМ НАЧАЛЕ промпта (модель
    # сильнее весит первые слова), а не в конце. Плюс negative prompt для
    # Flux-моделей (см. _generate_fal).
    no_text_prefix = (
        "ABSOLUTELY NO TEXT, no letters, no words, no titles, no signatures, "
        "no watermarks anywhere. Pure wordless illustration. "
    )
    # Короткий дублирующий тег в самом конце — модели attend сильнее к началу
    # и концу строки. Короткий, чтобы не вытеснить scene description при
    # обрезке до 950 chars (логика truncation ниже сохраняет первые 55% и
    # последние 40%). Recraft v3 натренирован на book covers и любит
    # дорисовывать названия — повтор в двух местах помогает заметно.
    no_text_suffix = " // wordless image, no text, no letters."
    prompt = (
        f"{no_text_prefix}{style_prompt} Scene to depict: "
        f"{scene_description}{no_text_suffix}"
    )
    logger.info("Image style chosen: %s", style_id)

    out = _cache_path(prompt)
    if out.exists():
        return out

    # Маршрутизация генерации, в порядке приоритета:
    # 1. Recraft Direct API — если задан RECRAFT_API_KEY и модель recraft-v3.
    #    Это наш основной путь: прямой вызов api.recraft.ai, полная поддержка
    #    custom style_id, без FAL-обёртки.
    # 2. FAL — для других моделей (Flux Pro/Dev/Schnell), либо если Recraft
    #    key не задан и модель recraft-v3.
    # 3. FusionBrain — самый последний legacy fallback.
    use_recraft_direct = (
        config.recraft_api_key
        and (config.image_model or "").strip().lower() == "recraft-v3"
    )
    if use_recraft_direct:
        result = await _generate_recraft_direct(prompt, out)
        if result:
            return result
        logger.warning("Recraft direct не отдал картинку — пробую FAL fallback")

    if config.fal_api_key:
        result = await _generate_fal(prompt, out)
        if result:
            return result
        logger.warning("FAL не отдал картинку — пробую FusionBrain fallback")

    if config.fusionbrain_api_key and config.fusionbrain_secret_key:
        return await _generate_fusionbrain(prompt, out)

    logger.warning("Ни RECRAFT_API_KEY, ни FAL_KEY, ни FUSIONBRAIN_* не заданы — картинка пропущена")
    return None


async def generate_three_illustrations(
    hero: str,
    theme_key: str,
    *,
    scenes: dict[str, str] | None,
) -> dict[str, Path | None]:
    """Генерирует 3 иллюстрации для PDF-книжки: обложка, кульминация, финал.

    Принимает scenes = {"opening": "...", "climax": "...", "ending": "..."}.
    Каждая сцена идёт в свой generate_cover() параллельно.

    Если scenes=None — fallback: для всех трёх используется один и тот же
    placeholder из FALLBACK_SCENE_TEMPLATE (картинки будут похожи, но не сорвут
    PDF полностью).

    Возвращает dict {"opening": Path|None, "climax": Path|None, "ending": Path|None}.
    Каждая может быть None если соответствующая генерация упала — PDF умеет
    рендериться без любой из иллюстраций (просто пропустит страницу).
    """
    import asyncio
    # Если сцен нет — используем один и тот же fallback для всех (хотя бы что-то).
    if not scenes:
        scenes = {"opening": None, "climax": None, "ending": None}

    async def _one(scene_desc: str | None, stage: str) -> Path | None:
        try:
            return await generate_cover(
                hero, theme_key,
                scene_description=scene_desc,
                stage=stage,
            )
        except Exception as e:
            logger.warning("Иллюстрация %s упала: %s", stage, e)
            return None

    results = await asyncio.gather(
        _one(scenes.get("opening"), "opening"),
        _one(scenes.get("climax"), "climax"),
        _one(scenes.get("ending"), "ending"),
        return_exceptions=False,
    )
    return {
        "opening": results[0],
        "climax": results[1],
        "ending": results[2],
    }


# ─────────────────── Маппинг IMAGE_MODEL → FAL endpoint и payload ───────────────────
# Каждая модель FAL принимает свой набор параметров. Recraft особенно отличается
# (есть style="children_book_illustration" и т.д.), Flux Pro нет safety override,
# Flux Dev требует num_inference_steps выше чем Schnell.

def _fal_endpoint_for_model(model: str) -> tuple[str, dict]:
    """Возвращает (endpoint_path, extra_payload) для FAL по выбранной модели.
    extra_payload — это параметры специфичные для модели, например style/steps.

    Базовые поля (prompt, image_size, num_images) добавляются в _generate_fal.
    """
    # IMAGE_MODEL формат принимаем гибко: и короткие имена (recraft-v3), и
    # legacy full endpoint (fal-ai/flux/schnell).
    m = (model or "").strip().lower()

    # Если IMAGE_MODEL не задан совсем — fallback на legacy FAL_MODEL (старые
    # инсталляции, где в .env только FAL_MODEL и нет IMAGE_MODEL).
    if not m and config.fal_model_legacy:
        return config.fal_model_legacy, {"num_inference_steps": 4}

    if m == "recraft-v3":
        # https://fal.ai/models/fal-ai/recraft-v3
        # Стратегия выбора стиля:
        # 1. Если задан RECRAFT_STYLE_ID в .env — используем наш натрениро-
        #    ванный приватный стиль (создан scripts/create_recraft_style.py
        #    на 5 эталонных картинках из style_references/). Это даёт точно
        #    тот визуальный язык, что мы выбрали, без борьбы с промптом.
        # 2. Если RECRAFT_STYLE_ID не задан — фолбэк на встроенный preset
        #    digital_illustration/hand_drawn (английская детская книжка —
        #    Квентин Блейк / Gruffalo). Минус preset'а: иногда дорисовывает
        #    «заголовок» сверху (Recraft натренирован на book covers).
        #    Митигируется двойным anti-text guard (no_text_prefix+suffix).
        if config.recraft_style_id:
            return "fal-ai/recraft-v3", {
                "style_id": config.recraft_style_id,
            }
        return "fal-ai/recraft-v3", {
            "style": "digital_illustration/hand_drawn",
        }
    if m == "flux-pro-1.1":
        # https://fal.ai/models/fal-ai/flux-pro/v1.1
        return "fal-ai/flux-pro/v1.1", {
            "safety_tolerance": "5",  # максимальная (1 строгая, 6 свободная)
        }
    if m == "flux-dev":
        # https://fal.ai/models/fal-ai/flux/dev
        return "fal-ai/flux/dev", {"num_inference_steps": 28, "guidance_scale": 3.5}
    if m == "flux-schnell" or not m:
        return "fal-ai/flux/schnell", {"num_inference_steps": 4}

    # Незнакомое значение — считаем что это уже endpoint path (fal-ai/...)
    if m.startswith("fal-ai/"):
        return m, {"num_inference_steps": 4}

    # Совсем непонятно — fallback на дефолт
    logger.warning("IMAGE_MODEL=%r не распознан, использую flux-schnell", model)
    return "fal-ai/flux/schnell", {"num_inference_steps": 4}


# ─────────────────── FusionBrain (Kandinsky) ───────────────────

async def _generate_fusionbrain(prompt: str, out: Path) -> Path | None:
    headers = {
        "X-Key": f"Key {config.fusionbrain_api_key}",
        "X-Secret": f"Secret {config.fusionbrain_secret_key}",
    }
    async with httpx.AsyncClient(timeout=120) as client:
        # 1. Берём первый доступный pipeline (обычно Kandinsky 3.1)
        resp = await client.get(f"{FUSIONBRAIN_BASE}/key/api/v1/pipelines", headers=headers)
        if resp.status_code != 200:
            logger.error("FusionBrain pipelines: %s %s", resp.status_code, resp.text[:200])
            return None
        pipelines = resp.json()
        if not pipelines:
            logger.error("FusionBrain вернул пустой список pipelines")
            return None
        pipeline_id = pipelines[0]["id"]

        # 2. Запуск генерации
        params = {
            "type": "GENERATE",
            "numImages": 1,
            "width": 1024,
            "height": 1024,
            "generateParams": {"query": prompt},
        }
        files = {
            "pipeline_id": (None, pipeline_id),
            "params": (None, json.dumps(params), "application/json"),
        }
        resp = await client.post(
            f"{FUSIONBRAIN_BASE}/key/api/v1/pipeline/run",
            headers=headers,
            files=files,
        )
        if resp.status_code not in (200, 201):
            logger.error("FusionBrain run: %s %s", resp.status_code, resp.text[:200])
            return None
        uuid = resp.json().get("uuid")
        if not uuid:
            logger.error("FusionBrain run: нет uuid в ответе: %s", resp.text[:200])
            return None

        # 3. Polling статуса (Kandinsky обычно отвечает за 10-30 секунд)
        for _ in range(40):  # до ~80 секунд
            await asyncio.sleep(2)
            r = await client.get(
                f"{FUSIONBRAIN_BASE}/key/api/v1/pipeline/status/{uuid}",
                headers=headers,
            )
            if r.status_code != 200:
                continue
            data = r.json()
            status = data.get("status")
            if status == "DONE":
                files_b64 = (
                    (data.get("result") or {}).get("files")
                    or data.get("images")
                    or []
                )
                if not files_b64:
                    logger.error("FusionBrain DONE без файлов: %s", json.dumps(data)[:300])
                    return None
                img_bytes = base64.b64decode(files_b64[0])
                out.write_bytes(img_bytes)
                return out
            if status == "FAIL":
                logger.error("FusionBrain FAIL: %s", data.get("errorDescription") or data)
                return None
        logger.error("FusionBrain timeout для uuid=%s", uuid)
        return None


# ─────────────────── Recraft Direct API ───────────────────
# Прямой вызов api.recraft.ai (без FAL-обёртки). Используется как основной
# путь для recraft-v3, если задан RECRAFT_API_KEY в .env. Поддерживает наш
# натренированный custom style_id, чего FAL может не уметь правильно
# пробрасывать. FAL остаётся как fallback для Flux-моделей.

RECRAFT_GENERATIONS_URL = "https://external.api.recraft.ai/v1/images/generations"


async def _generate_recraft_direct(prompt: str, out: Path) -> Path | None:
    """Прямая генерация через api.recraft.ai (минуя FAL-обёртку).

    Если задан config.recraft_style_id — рисует в нашем натренированном
    приватном стиле. Иначе — фолбэк на встроенный preset hand_drawn.
    """
    headers = {
        "Authorization": f"Bearer {config.recraft_api_key}",
        "Content-Type": "application/json",
    }
    payload: dict[str, object] = {
        "prompt": prompt,
        "model": "recraftv3",
        "size": "1024x1024",
        "n": 1,
        "response_format": "url",
    }
    # Стиль: либо наш натренированный (приоритет), либо встроенный preset.
    if config.recraft_style_id:
        payload["style_id"] = config.recraft_style_id
    else:
        payload["style"] = "digital_illustration/hand_drawn"

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(RECRAFT_GENERATIONS_URL, headers=headers, json=payload)
            if resp.status_code != 200:
                logger.error(
                    "Recraft direct error %s: %s",
                    resp.status_code, resp.text[:500],
                )
                return None
            data = resp.json()
            images = data.get("data") or []
            if not images:
                logger.error("Recraft direct: пустой data в ответе: %s", str(data)[:200])
                return None
            img_url = images[0].get("url")
            if not img_url:
                logger.error("Recraft direct: нет url в data[0]: %s", str(images[0])[:200])
                return None
            # Качаем картинку отдельным запросом
            img_bytes = (await client.get(img_url, timeout=30)).content
            out.write_bytes(img_bytes)
            logger.info(
                "Recraft direct: сгенерил %d KB (style_id=%s)",
                len(img_bytes) // 1024,
                "custom" if config.recraft_style_id else "preset hand_drawn",
            )
            return out
    except Exception as e:
        logger.exception("Recraft direct упал: %s", e)
        return None


# ─────────────────── FAL.AI (Flux Schnell) ───────────────────

async def _generate_fal(prompt: str, out: Path) -> Path | None:
    """Генерация через FAL. Выбор модели через IMAGE_MODEL в .env.

    До 2-х попыток: первая обычная, вторая — со сменой seed (для retry на NSFW
    false-positive у Flux). Recraft не имеет safety_checker — ретрай не нужен.
    """
    endpoint, extra_payload = _fal_endpoint_for_model(config.image_model)
    url = f"https://fal.run/{endpoint}"
    headers = {"Authorization": f"Key {config.fal_api_key}", "content-type": "application/json"}

    # Recraft v3 имеет ЖЁСТКИЙ лимит 1000 символов на prompt — обрезаем с запасом.
    # Flux-семейство принимает до ~2000-4000, там обрезка не нужна.
    # Урезаем умно: сохраняем начало (стиль) + конец (сцену + запрет текста).
    max_prompt_chars = 950 if endpoint == "fal-ai/recraft-v3" else 4000
    safe_prompt = prompt
    if len(prompt) > max_prompt_chars:
        # Берём первые 60% (стиль) и последние 40% (сцена + no_text),
        # склеиваем через "...". Сцена критичнее стиля — её нельзя терять.
        head_len = int(max_prompt_chars * 0.55)
        tail_len = max_prompt_chars - head_len - 5
        safe_prompt = prompt[:head_len] + " ... " + prompt[-tail_len:]
        logger.info("Prompt обрезан с %d до %d символов для %s",
                    len(prompt), len(safe_prompt), endpoint)

    # Recraft v3 принимает image_size по-другому (использует строки или {w,h}).
    # Для квадратной обложки 1024x1024 — square_hd подходит для всех моделей.
    base_payload = {
        "prompt": safe_prompt,
        "image_size": "square_hd",
        "num_images": 1,
    }

    # Flux-семейство поддерживает enable_safety_checker (false снимает ложные
    # срабатывания на сказочных промптах: принцесса, единорог, дракон).
    # Recraft и FluxPro 1.1 параметра не имеют — туда не передаём.
    if endpoint.startswith("fal-ai/flux/"):
        base_payload["enable_safety_checker"] = False

    # Запрет текста на картинке через negative prompt — поддерживается Flux Pro/Dev
    # (полный набор параметров) и Recraft v3 (через style attributes). Schnell
    # негатив-промпт игнорирует, но мы и так в основном промпте запретили.
    negative = (
        "text, letters, words, captions, titles, signatures, logos, watermarks, "
        "subtitles, written language, alphabet, characters, typography"
    )
    if endpoint in ("fal-ai/flux-pro/v1.1", "fal-ai/flux/dev"):
        base_payload["negative_prompt"] = negative

    async with httpx.AsyncClient(timeout=120) as client:
        for attempt in range(2):
            payload = {**base_payload, **extra_payload}
            if attempt > 0:
                import random
                payload["seed"] = random.randint(1, 2**31 - 1)

            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                logger.error("FAL %s error %s: %s",
                             endpoint, resp.status_code, resp.text[:300])
                # 422 чаще всего невалидный параметр — ретрай не поможет, выходим.
                # 429/503 — временная проблема, имеет смысл retry на втором проходе.
                if resp.status_code in (400, 401, 403, 422):
                    return None
                if attempt == 0:
                    continue
                return None
            data = resp.json()

            # У Flux-моделей есть has_nsfw_concepts — иногда срабатывает ложно,
            # ретраимся с новым seed
            if any(data.get("has_nsfw_concepts") or []):
                logger.warning("FAL отметил кадр как NSFW (попытка %d), ретрай", attempt + 1)
                continue

            # Картинку возвращают по-разному: Recraft → images[0].url,
            # Flux → images[0].url, оба формата унифицированы.
            images = data.get("images") or []
            if not images:
                logger.error("FAL %s вернул пустой images: %s",
                             endpoint, str(data)[:200])
                return None
            img_url = images[0].get("url")
            if not img_url:
                logger.error("FAL %s: нет url в images[0]: %s",
                             endpoint, str(images[0])[:200])
                return None
            img_bytes = (await client.get(img_url, timeout=30)).content
            out.write_bytes(img_bytes)
            logger.info("Image generated via %s (%d bytes)", endpoint, len(img_bytes))
            return out

        logger.error("FAL %s: 2 попытки, картинка всё равно not safe — пропускаю", endpoint)
        return None
