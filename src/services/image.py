"""Генерация картинки-обложки.

Поддерживается два провайдера, выбирается автоматически по наличию ключей в .env:
  • FusionBrain (Kandinsky 3.x от Сбера) — для России. Платится рублями.
    Регистрация: https://fusionbrain.ai → Войти → API → Создать ключ.
    Бесплатно: 1 запрос / 15 секунд.
  • FAL.AI (Flux Schnell) — международный. Платится зарубежной картой.

Если оба заданы — приоритет у FusionBrain.
Если ни один не задан — функция возвращает None, бот шлёт сказку без обложки."""

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
    IMAGE_PROMPT_TEMPLATE,
    THEME_TO_EN,
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
) -> Path | None:
    """Генерирует обложку в стиле «детский рисунок с волшебством».

    Если передан scene_description (одно предложение по-английски из конкретной сказки) —
    картинка будет про этот момент. Иначе используется fallback с heroем и темой."""
    theme_en = THEME_TO_EN.get(theme_key, "kindness and warmth")
    if not scene_description:
        scene_description = FALLBACK_SCENE_TEMPLATE.format(hero=hero, theme_en=theme_en)

    prompt = IMAGE_PROMPT_TEMPLATE.format(scene_description=scene_description)
    out = _cache_path(prompt)
    if out.exists():
        return out

    # Приоритет: FusionBrain (если есть ключи) → FAL → ничего
    if config.fusionbrain_api_key and config.fusionbrain_secret_key:
        result = await _generate_fusionbrain(prompt, out)
        if result:
            return result
        logger.warning("FusionBrain не отдал картинку — пробую FAL")

    if config.fal_api_key:
        return await _generate_fal(prompt, out)

    logger.warning("Ни FUSIONBRAIN_*, ни FAL_KEY не заданы — картинка пропущена")
    return None


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


# ─────────────────── FAL.AI (Flux Schnell) ───────────────────

async def _generate_fal(prompt: str, out: Path) -> Path | None:
    """Генерация через FAL. До 2-х попыток: первая без safety_checker,
    вторая — со сменой seed, на случай если FAL всё равно выдал чёрный кадр.

    enable_safety_checker=False отключает фильтр NSFW, который у Flux Schnell
    периодически ложно срабатывает на сказочных промптах (принцесса, единорог,
    дракон) и возвращает чёрный квадрат вместо картинки."""
    url = f"https://fal.run/{config.fal_model}"
    headers = {"Authorization": f"Key {config.fal_api_key}", "content-type": "application/json"}

    async with httpx.AsyncClient(timeout=60) as client:
        for attempt in range(2):
            payload = {
                "prompt": prompt,
                "image_size": "square_hd",
                "num_inference_steps": 4,
                "num_images": 1,
                "enable_safety_checker": False,
            }
            if attempt > 0:
                # на ретрае подкручиваем seed чтобы получить другую генерацию
                import random
                payload["seed"] = random.randint(1, 2**31 - 1)

            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                logger.error("FAL error %s: %s", resp.status_code, resp.text[:200])
                return None
            data = resp.json()

            # Иногда FAL всё равно возвращает has_nsfw_concepts=true → проскочило
            # через safety_checker, ретраимся с новым seed
            if any(data.get("has_nsfw_concepts") or []):
                logger.warning("FAL отметил кадр как NSFW (попытка %d), ретрай", attempt + 1)
                continue

            img_url = (data.get("images") or [{}])[0].get("url")
            if not img_url:
                logger.error("FAL вернул пустой images: %s", str(data)[:200])
                return None
            img_bytes = (await client.get(img_url, timeout=30)).content
            out.write_bytes(img_bytes)
            return out

        logger.error("FAL: 2 попытки, картинка всё равно not safe — пропускаю")
        return None
