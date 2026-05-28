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

import random as _random

from ..config import config
from ..prompts import (
    FALLBACK_SCENE_TEMPLATE,
    IMAGE_STYLE_BASE,
    IMAGE_STYLE_VARIANTS,
    THEME_TO_EN,
    pick_hero_visual,
)

logger = logging.getLogger(__name__)

FUSIONBRAIN_BASE = "https://api-key.fusionbrain.ai"


def _cache_path(prompt: str) -> Path:
    """SHA256 от (prompt + параметры стиля). КРИТИЧНО: в ключ должны входить
    ВСЕ параметры, которые реально меняют картинку. Иначе при смене пресета
    в .env будет возвращаться старая закэшированная картинка с предыдущим
    стилем — кэш переживает docker rebuild (bind-mount ./cache:/app/cache).

    Раньше ключом был только prompt, и при смене RECRAFT_STYLE_PRESET картинки
    не менялись — это был самый коварный баг иттерации над стилем.
    """
    cache_key = "||".join([
        prompt,
        str(config.image_model or ""),
        str(config.recraft_style_id or ""),
        str(config.recraft_style_preset or ""),
    ])
    digest = hashlib.sha256(cache_key.encode()).hexdigest()[:32]
    cache_dir = Path(config.image_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{digest}.png"


async def generate_cover(
    hero: str,
    theme_key: str,
    *,
    scene_description: str | None = None,
    stage: str | None = None,
    style_variant: str | None = None,
    child_gender: str | None = None,
    hero_visual: str | None = None,
) -> Path | None:
    """Генерирует иллюстрацию для PDF-книжки.

    scene_description — одно английское предложение от сказочника-Claude,
        описывающее одну из трёх параллельных сцен мира сказки.
    stage — legacy, больше не используется.
    style_variant — ключ из IMAGE_STYLE_VARIANTS (vast_world, busy_scene,
        magic_moment, cozy_interior, journey, character_focus).
        Если None — выбирается рандомно.
    child_gender — "male" / "female". Если задан и hero_visual=None,
        собираем hero-описание через pick_hero_visual(child_gender).
    hero_visual — готовая строка hero-hint (если генератор сцены хочет
        зафиксировать тот же визуал на все три картинки сказки).
        Приоритетнее child_gender.
    """
    _ = THEME_TO_EN.get(theme_key, "kindness and warmth")  # legacy theme
    _ = stage  # больше не используется

    if style_variant and style_variant in IMAGE_STYLE_VARIANTS:
        style_prompt = IMAGE_STYLE_VARIANTS[style_variant]
        logger.info("Image style variant: %s (explicit)", style_variant)
    else:
        chosen_key = _random.choice(list(IMAGE_STYLE_VARIANTS.keys()))
        style_prompt = IMAGE_STYLE_VARIANTS[chosen_key]
        logger.info("Image style variant: %s (random)", chosen_key)

    # Hero-hint — тип главного героя (мальчик/девочка/фея/...). Это
    # ключевой anti-Brambly-Hedge инструмент: без него натренированный
    # на мышах style_id тянет в зверушек в одежде.
    if hero_visual is None:
        hero_visual = pick_hero_visual(child_gender)
    logger.info("Hero visual hint: %s", hero_visual[:60].replace("\n", " "))

    # Чистый художественный промпт (см. IMAGE_STYLE_BASE) без технических
    # guard'ов. Раньше тут был «// wordless, no text.» суффикс — его
    # убрали ради pure-теста авторского промпта. Если Recraft начнёт
    # дорисовывать «The End» / подписи — вернём минимальный «Без надписей.»
    # в конец. Composition rules, anti-list, watercolor-tech слова — тоже
    # вычищены, чтобы не противоречить художественной интонации.
    #
    # Сцена-описание идёт как «Сцена: <текст>» после стиля. Это контекст
    # происходящего, не команда «нарисуй именно это» — финальную композицию
    # художник выбирает сам.
    # Склейка: style + hero + scene. Hero идёт ПОСЛЕ style — он
    # перебивает дефолтные тяги модели в Brambly-Hedge мышей.
    # Scene — последней, она про конкретный момент мира.
    scene_hint = (scene_description or "").strip()
    # Бюджет model-aware: Recraft Direct ограничивает prompt ~980 char,
    # FLUX-pro/dev и Gemini Imagen — гораздо мягче (5000+). Под Recraft
    # держим жёсткий лимит, иначе scene дропается и три картинки сольются.
    hero_block = (hero_visual or "").strip()
    image_model = (config.image_model or "").strip().lower()
    total_budget = 975 if image_model == "recraft-v3" else 2400
    fixed_overhead = len(style_prompt) + len(hero_block) + len("Scene: ") + 4
    budget_for_scene = total_budget - fixed_overhead

    parts = [style_prompt]
    if hero_block:
        parts.append(hero_block)
    if scene_hint and budget_for_scene > 5:
        if len(scene_hint) > budget_for_scene:
            cut = scene_hint[: budget_for_scene - 1].rstrip()
            space = cut.rfind(" ")
            if space > budget_for_scene * 0.6:
                cut = cut[:space]
            scene_hint = cut + "…"
        parts.append(f"Scene: {scene_hint}")
    prompt = "\n\n".join(parts)
    # Логируем хвост prompt'а (последние 120 char) — там лежит scene hint
    # после стиля. Помогает увидеть, что РЕАЛЬНО попадает в Recraft:
    # одинаковый ли это хвост у трёх картинок (тогда они сольются) или
    # каждый раз свой.
    logger.info(
        "Image prompt assembled, %d chars, tail: …%s",
        len(prompt), prompt[-120:].replace("\n", " ⏎ "),
    )

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
    child_name: str | None = None,
    child_gender: str | None = None,
) -> dict[str, Path | None]:
    """Генерирует 3 иллюстрации для PDF-книжки: обложка, кульминация, финал.

    Принимает scenes = {"opening": "...", "climax": "...", "ending": "..."}.
    Каждая сцена идёт в свой generate_cover() параллельно.

    child_gender — "male" / "female". Передаётся в pick_hero_visual для
    выбора облика главного героя (мальчик/девочка/фея/...). Один и тот
    же hero_visual фиксируется на все 3 картинки одной сказки, чтобы
    герой не менялся между разворотами книжки.

    Если scenes=None — fallback: картинки рисуются без сюжетного мотива,
    только по style+hero.

    Возвращает dict {"opening": Path|None, "climax": Path|None, "ending": Path|None}.
    """
    import asyncio
    if not scenes:
        scenes = {"opening": None, "climax": None, "ending": None}

    _ = child_name  # пока не используется

    # Фиксируем один hero_visual на все 3 картинки — чтобы Маша на
    # обложке не превратилась в фею к концу книжки. Ротация типа героя
    # происходит МЕЖДУ сказками, не внутри.
    fixed_hero_visual = pick_hero_visual(child_gender)
    logger.info(
        "Fixed hero visual for this story (gender=%s): %s…",
        child_gender, fixed_hero_visual[:50].replace("\n", " "),
    )

    # 3 РАЗНЫХ style_variant без повторов — каждая из трёх картинок
    # одной книжки в своём регистре композиции.
    variant_keys = list(IMAGE_STYLE_VARIANTS.keys())
    chosen_variants = _random.sample(variant_keys, k=min(3, len(variant_keys)))
    logger.info(
        "Style variants for 3 illustrations: opening=%s, climax=%s, ending=%s",
        *chosen_variants,
    )
    variants_per_stage = {
        "opening": chosen_variants[0],
        "climax": chosen_variants[1],
        "ending": chosen_variants[2],
    }

    async def _one(scene_desc: str | None, stage: str) -> Path | None:
        try:
            return await generate_cover(
                hero, theme_key,
                scene_description=scene_desc,
                stage=stage,
                style_variant=variants_per_stage[stage],
                hero_visual=fixed_hero_visual,
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
        # ВАЖНО: FAL-обёртка для recraft-v3 принимает ТОЛЬКО верхний уровень
        # style ("any", "realistic_image", "digital_illustration",
        # "vector_illustration"). Substyle (например, "hand_drawn") FAL НЕ
        # поддерживает — он только в Recraft Direct API. Раньше тут стоял
        # слитный "digital_illustration/hand_drawn", и FAL валился с 422 как
        # только Recraft Direct падал. Теперь срезаем substyle до слэша.
        preset = config.recraft_style_preset or "digital_illustration/hand_drawn"
        top_style = preset.split("/", 1)[0] if "/" in preset else preset
        return "fal-ai/recraft-v3", {
            "style": top_style,
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
    # Recraft Direct API имеет ЖЁСТКИЙ лимит 1000 символов на prompt
    # (возвращает 400 «prompt length should be in [1, 1000]»). Обрезаем
    # тем же алгоритмом, что и FAL-путь: 55% начала (стиль) + 40% конца
    # (сцена + запрет текста), склейка через "...". Сцена критичнее стиля.
    max_prompt_chars = 980
    safe_prompt = prompt
    if len(prompt) > max_prompt_chars:
        head_len = int(max_prompt_chars * 0.55)
        tail_len = max_prompt_chars - head_len - 5
        safe_prompt = prompt[:head_len] + " ... " + prompt[-tail_len:]
        logger.info(
            "Recraft direct: prompt обрезан с %d до %d символов",
            len(prompt), len(safe_prompt),
        )
    payload: dict[str, object] = {
        "prompt": safe_prompt,
        "model": "recraftv3",
        # Квадрат 1024x1024 — самый предсказуемый размер для Recraft v3,
        # без растяжения. Раньше тут было 1024x1536 (2:3 портрет), но это
        # «вытягивало» картинку. Квадрат хорошо смотрится в Telegram
        # превью и аккуратно ложится в PDF.
        "size": "1024x1024",
        "n": 1,
        "response_format": "url",
    }
    # Стиль: либо наш натренированный (приоритет), либо встроенный preset.
    if config.recraft_style_id:
        payload["style_id"] = config.recraft_style_id
    else:
        # Встроенный preset берётся из .env RECRAFT_STYLE_PRESET. Дефолт —
        # «digital_illustration/hand_drawn». Recraft Direct API ожидает
        # «style» и «substyle» РАЗДЕЛЬНО (FAL-обёртка более либеральна и
        # принимает слитную строку «digital_illustration/hand_drawn», но
        # прямой API на такое отвечает 400 Invalid style). Разделяем по «/».
        preset = config.recraft_style_preset or "digital_illustration/hand_drawn"
        if "/" in preset:
            style_top, substyle = preset.split("/", 1)
            payload["style"] = style_top
            if substyle:
                payload["substyle"] = substyle
        else:
            payload["style"] = preset

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
        # Квадрат 1024x1024 — без растяжения. Раньше было {1024,1536}
        # (2:3 портрет), но это «вытягивало» картинку. FAL принимает
        # explicit {width,height} для recraft-v3 и Flux одинаково.
        "image_size": {"width": 1024, "height": 1024},
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
