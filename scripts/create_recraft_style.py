"""Одноразовый скрипт тренировки приватного Recraft Custom Style.

Загружает 5 эталонных картинок из ../style_references/, обучает на них
приватный style_id, который потом будет использоваться во всех генерациях
бота вместо встроенного preset'а вроде "digital_illustration/hand_drawn".

Зачем нужно. Встроенные preset'ы Recraft (digital_illustration, hand_drawn,
pastel_painting и т.д.) — общие, под среднее представление о «детской
иллюстрации». Custom style — это твой персональный preset, натренированный
на конкретных эталонах: модель учится по картинкам, а не по словам в
промпте. Тогда «правильный стиль» становится дефолтным, а не результатом
борьбы с промптом.

Запуск:

    export RECRAFT_API_KEY=твой_ключ_с_recraft_ai
    python scripts/create_recraft_style.py

Получи ключ на https://www.recraft.ai → Profile → API. Тренировка одного
стиля стоит примерно $1 (одноразово, потом юзаешь сколько хочешь).

После выполнения скрипт напечатает style_id вида
«12345678-abcd-...» — скопируй его и пропиши в .env:

    RECRAFT_STYLE_ID=12345678-abcd-...

После этого деплой бота — image.py подхватит и начнёт рисовать в твоём
натренированном стиле.
"""

from __future__ import annotations

import os
import sys
from io import BytesIO
from pathlib import Path

import httpx

try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

# ─────────────────── Параметры ───────────────────

API_KEY = os.environ.get("RECRAFT_API_KEY")
if not API_KEY:
    print("ERROR: переменная окружения RECRAFT_API_KEY не задана.")
    print("Получи ключ на https://www.recraft.ai → Profile → API")
    print()
    print("Затем запусти:")
    print("    export RECRAFT_API_KEY=твой_ключ")
    print("    python scripts/create_recraft_style.py")
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent
REF_DIR = REPO_ROOT / "style_references"

# Базовый шаблон, на котором тренируется кастомный стиль. Варианты:
#   digital_illustration — наш случай (книжная иллюстрация)
#   realistic_image      — фотореализм
#   vector_illustration  — векторная графика
#   icon                 — иконки
BASE_STYLE = "digital_illustration"

# Recraft API. У них OpenAI-совместимый эндпоинт external.api.recraft.ai.
# POST /styles принимает multipart/form-data:
#   - style: базовый шаблон (см. BASE_STYLE)
#   - file: до 5 эталонных изображений (PNG/JPEG)
# Возвращает { "id": "uuid-style-id" }
RECRAFT_STYLES_URL = "https://external.api.recraft.ai/v1/styles"


# Recraft требует суммарный размер всех файлов <= 5 MB. Скриншоты на 2.4 MB
# каждый суммарно дают 12 MB → переупаковываем в JPEG quality 85 с ресайзом
# до 1536×1536. Получается ~300-500 KB на картинку, влезает с большим
# запасом. Стиль модель учит всё равно с уменьшенных версий — Recraft
# downscale-ит сама перед обучением.
MAX_DIMENSION = 1536
JPEG_QUALITY = 85
RECRAFT_TOTAL_MAX_BYTES = 5 * 1024 * 1024


def prepare_image(path: Path) -> tuple[str, bytes, str]:
    """Готовит картинку под аплоад: ресайз + конверт в JPEG.

    Возвращает (filename, bytes, content_type).
    Если Pillow не установлен — отдаёт файл как есть (риск 413).
    """
    raw = path.read_bytes()
    if not HAS_PILLOW:
        ext = path.suffix.lower().lstrip(".")
        ct = "image/jpeg" if ext in ("jpg", "jpeg") else "image/png"
        return path.name, raw, ct

    img = Image.open(BytesIO(raw))
    # JPEG не поддерживает alpha — конвертим в RGB
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGB")
    # Ресайз вниз с сохранением пропорций (если уже меньше — не трогаем)
    img.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)
    # Перепаковка в JPEG
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    out = buf.getvalue()
    return path.stem + ".jpg", out, "image/jpeg"


# ─────────────────── Логика ───────────────────

def main() -> None:
    # Собираем PNG и JPG вместе. Раньше брали либо одно либо другое
    # (.png ИЛИ .jpg), и если попадались оба формата в одном датасете,
    # JPG-картинки тихо отваливались. Берём всё и сортируем по имени.
    image_files = sorted(
        list(REF_DIR.glob("0?_*.png"))
        + list(REF_DIR.glob("0?_*.jpg"))
        + list(REF_DIR.glob("0?_*.jpeg"))
    )

    if not image_files:
        print(f"ERROR: не найдено картинок в {REF_DIR}")
        print("Ожидаются файлы вида 01_*.png, 02_*.png ... 05_*.png")
        sys.exit(1)

    if len(image_files) > 5:
        print(f"WARN: найдено {len(image_files)} картинок, Recraft принимает")
        print("максимум 5. Возьму первые 5 (по алфавиту):")
        image_files = image_files[:5]

    print(f"Эталонный сет: {len(image_files)} картинок из {REF_DIR}")
    for f in image_files:
        size_mb = f.stat().st_size / 1024 / 1024
        print(f"  {f.name}  ({size_mb:.1f} MB raw)")
    print()

    if not HAS_PILLOW:
        print("WARN: Pillow не установлен — отправлю как есть.")
        print("      Если суммарный размер > 5 MB, Recraft вернёт image_too_big.")
        print("      Установи: python3 -m pip install pillow")
        print()

    # Готовим (ресайз + JPEG) и считаем суммарный размер
    print(f"Готовлю картинки (ресайз до {MAX_DIMENSION}px, JPEG q{JPEG_QUALITY})...")
    prepared = []
    total_bytes = 0
    for f in image_files:
        name, blob, ct = prepare_image(f)
        prepared.append((name, blob, ct))
        total_bytes += len(blob)
        print(f"  {name}  ({len(blob)/1024:.0f} KB)")
    print(f"  ─ суммарно {total_bytes/1024/1024:.2f} MB "
          f"(лимит Recraft {RECRAFT_TOTAL_MAX_BYTES/1024/1024:.0f} MB)")
    if total_bytes > RECRAFT_TOTAL_MAX_BYTES:
        print(f"ERROR: суммарный размер всё ещё > {RECRAFT_TOTAL_MAX_BYTES} bytes.")
        print(f"      Уменьши MAX_DIMENSION или JPEG_QUALITY в скрипте.")
        sys.exit(1)
    print()
    print(f"Базовый шаблон: {BASE_STYLE}")
    print(f"Отправляю в Recraft Custom Style API ({RECRAFT_STYLES_URL})...")
    print()

    headers = {"Authorization": f"Bearer {API_KEY}"}
    files = [
        ("file", (name, BytesIO(blob), ct))
        for name, blob, ct in prepared
    ]
    data = {"style": BASE_STYLE}

    try:
        with httpx.Client(timeout=300) as client:
            r = client.post(
                RECRAFT_STYLES_URL,
                headers=headers,
                data=data,
                files=files,
            )

        if r.status_code != 200:
            print(f"ERROR: Recraft вернул HTTP {r.status_code}")
            print(r.text[:1000])
            sys.exit(1)

        result = r.json()
        style_id = result.get("id")
        if not style_id:
            print(f"ERROR: ответ без поля 'id': {result}")
            sys.exit(1)

        print("=" * 60)
        print("SUCCESS! Твой style_id:")
        print()
        print(f"    {style_id}")
        print()
        print("=" * 60)
        print()
        print("Шаг 1. Пропиши в .env (в корне проекта):")
        print()
        print(f"    RECRAFT_STYLE_ID={style_id}")
        print()
        print("Шаг 2. Передай эту же переменную в Docker на сервере (тот же")
        print("       .env-файл, который монтируется в контейнер).")
        print()
        print("Шаг 3. Деплой как обычно — image.py подхватит RECRAFT_STYLE_ID")
        print("       и начнёт рисовать в твоём натренированном стиле.")

    finally:
        # BytesIO не требует close, но на всякий случай
        for _, (_, buf, _) in files:
            try:
                buf.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
