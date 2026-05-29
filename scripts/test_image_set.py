"""Тестовый прогон генератора иллюстраций.

Запускает generate_three_illustrations() с фиксированными тестовыми
сценами и сохраняет три картинки в cache/test/. Используется чтобы
быстро увидеть текущее состояние стиля без полного flow сказки.

Запуск (на сервере, в docker-контейнере):
    docker compose exec bot python -m scripts.test_image_set
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> int:
    from src.services.image import generate_three_illustrations

    # Тестовая сказка: мальчик и дракончик. Сцены РАЗНЫЕ по местам/
    # действиям, в climax намеренно конкретное существо (дракон) —
    # проверка что FLUX не подменит на «кита из стиля».
    scenes = {
        "opening": "The boy reads a book on the doorstep of his hilltop home",
        "climax": "The boy meets a small green dragon cub beside a sunlit pond",
        "ending": "The boy waves goodbye from a wooden bridge over a stream",
    }

    out_dir = Path("cache/test")
    out_dir.mkdir(parents=True, exist_ok=True)
    # Чистим старые файлы чтобы видеть только свежие.
    for f in out_dir.glob("*.png"):
        f.unlink()
    for f in out_dir.glob("*.jpg"):
        f.unlink()

    logger.info("Запускаю генерацию 3 иллюстраций...")
    results = await generate_three_illustrations(
        hero="",
        theme_key="",
        scenes=scenes,
        child_name="Тимоша",
        child_gender="male",
    )

    logger.info("Готово, результаты:")
    for stage, path in results.items():
        if path and path.exists():
            target = out_dir / f"{stage}{path.suffix}"
            shutil.copy2(path, target)
            logger.info("  %s -> %s (%d bytes)", stage, target, target.stat().st_size)
        else:
            logger.warning("  %s: НЕТ ФАЙЛА", stage)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
