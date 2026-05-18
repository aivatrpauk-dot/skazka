"""Одноразовый скрипт: генерирует аватарку для бота через FAL.

Запуск:
    cd skazka-bot
    python3 scripts/generate_avatar.py

На выходе — assets/avatar.png (1024x1024) + варианты по разным стилям.
Выбираешь понравившийся, обрезаешь до 512x512 (Preview на маке умеет),
загружаешь в @BotFather через /setuserpic.

Стоимость: ~0.5₽ за все 4 варианта."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

FAL_KEY = os.getenv("FAL_KEY")
ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
ASSETS.mkdir(exist_ok=True)


VARIANTS = {
    "moon_fox": (
        "Soft watercolor children book illustration of a glowing crescent moon "
        "with a tiny sleeping fox curled on top, surrounded by warm golden stars. "
        "Cozy, dreamy night sky. Minimalist composition, centered. "
        "No text, no letters, no signatures. Classic European bedtime story aesthetic."
    ),
    "open_book": (
        "Soft watercolor illustration of an open storybook glowing with golden light, "
        "small stars and a tiny moon rising from the pages. Cozy bedtime atmosphere, "
        "warm cream and indigo colors. Minimalist, centered. No text, no letters."
    ),
    "bear_cloud": (
        "Soft watercolor illustration of a sleepy teddy bear sitting on a fluffy cloud, "
        "tiny stars around, warm dreamy colors. Children book aesthetic, minimalist, "
        "centered composition. No text, no letters, no signatures."
    ),
    "lantern": (
        "Soft watercolor illustration of a cozy paper lantern glowing in a deep blue "
        "night sky, small stars around. Warm gold and indigo. Children book aesthetic, "
        "minimalist, centered. No text, no letters."
    ),
}


async def generate(prompt: str, out_path: Path, client: httpx.AsyncClient) -> None:
    url = "https://fal.run/fal-ai/flux/schnell"
    headers = {"Authorization": f"Key {FAL_KEY}", "content-type": "application/json"}
    payload = {
        "prompt": prompt,
        "image_size": "square_hd",   # 1024x1024
        "num_inference_steps": 4,
        "num_images": 1,
    }
    resp = await client.post(url, json=payload, headers=headers, timeout=60)
    if resp.status_code != 200:
        print(f"FAIL {out_path.name}: {resp.status_code} {resp.text[:200]}")
        return
    data = resp.json()
    img_url = data["images"][0]["url"]
    img_bytes = (await client.get(img_url, timeout=30)).content
    out_path.write_bytes(img_bytes)
    print(f"  ✓ {out_path.name}  ({len(img_bytes)/1024:.0f} KB)")


async def main() -> None:
    if not FAL_KEY:
        print("Ошибка: FAL_KEY не задан в .env", file=sys.stderr)
        sys.exit(1)

    print(f"Генерирую {len(VARIANTS)} варианта аватарки в {ASSETS}/ …\n")
    async with httpx.AsyncClient() as client:
        await asyncio.gather(
            *(generate(prompt, ASSETS / f"avatar_{name}.png", client)
              for name, prompt in VARIANTS.items())
        )

    print("\nГотово.\n")
    print("Дальше:")
    print(f"  1. Открой {ASSETS}/  и посмотри все 4 варианта")
    print("  2. Выбери понравившийся → переименуй в avatar.png (для красоты)")
    print("  3. Открой его в Preview, Tools → Crop → квадрат 512x512")
    print("  4. В Telegram → @BotFather → /setuserpic → выбери своего бота → отправь файл")
    print()
    print("Если ни один не подошёл — поправь промпт в VARIANTS и запусти заново.")


if __name__ == "__main__":
    asyncio.run(main())
