"""Витринный образец сказки.

Юзер до покупки видит готовую сказку из «галереи» — это снимает риск
покупать вслепую и заменяет старую модель «первая бесплатно» (которая
обходилась нам в ~3₽ на каждого нового тестера).

Источник файлов — два пути по приоритету:

1. cache/demo/ — override админом через /save_as_demo (см. handlers/admin.py).
   Эти файлы переживают рестарт через docker-volume и могут быть заменены
   на лету любой сгенерированной сказкой.

2. resources/demo/ — fallback из репо (если /save_as_demo ещё не звали).
   Лежит как статика в Docker-образе.

Ожидаемые файлы (одинаковые имена в обоих путях):
  story.txt      — текст сказки, первая строка — название
  cover.jpg      — обложка (opening illustration)
  climax.jpg     — кульминационная иллюстрация
  ending.jpg     — финальная иллюстрация
  book.pdf       — собранная PDF-книжка

Если файлов нет ни там ни там — handler покажет заглушку «образец готовим,
пока попробуйте сложить свою сказку».
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from ..db import Session, Story

logger = logging.getLogger(__name__)


# Корень репо. __file__ = src/services/demo.py → parents[2] = repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_RESOURCES_DIR = _REPO_ROOT / "resources" / "demo"
_CACHE_DIR = _REPO_ROOT / "cache" / "demo"


@dataclass
class DemoStory:
    """Что есть в витрине. Любое поле может быть None если файла нет."""
    title: str | None        # из story.txt, первая строка
    text: str | None         # из story.txt, после первой строки
    cover_path: Path | None
    climax_path: Path | None
    ending_path: Path | None
    pdf_path: Path | None


def _first_existing(filename: str) -> Path | None:
    """Возвращает путь к файлу из cache/demo если он есть, иначе из
    resources/demo, иначе None."""
    cache_path = _CACHE_DIR / filename
    if cache_path.exists():
        return cache_path
    repo_path = _RESOURCES_DIR / filename
    if repo_path.exists():
        return repo_path
    return None


def load_demo_story() -> DemoStory:
    """Собирает витринный образец из доступных файлов. Может вернуть
    DemoStory со всеми None — это значит образец ещё не настроен,
    handler покажет заглушку.
    """
    story_path = _first_existing("story.txt")
    title: str | None = None
    text: str | None = None
    if story_path:
        try:
            raw = story_path.read_text(encoding="utf-8").strip()
            if "\n" in raw:
                first_line, rest = raw.split("\n", 1)
                title = first_line.strip()
                text = rest.lstrip()
            else:
                title = raw
                text = ""
        except Exception as e:
            logger.warning("Не смог прочитать %s: %s", story_path, e)

    return DemoStory(
        title=title,
        text=text,
        cover_path=_first_existing("cover.jpg"),
        climax_path=_first_existing("climax.jpg"),
        ending_path=_first_existing("ending.jpg"),
        pdf_path=_first_existing("book.pdf"),
    )


def is_demo_available() -> bool:
    """Витрина считается готовой если есть хотя бы текст + PDF."""
    demo = load_demo_story()
    return bool(demo.text and demo.pdf_path)


async def save_story_as_demo(story_id: int) -> tuple[bool, str]:
    """Копирует файлы указанной сказки из БД в cache/demo/, делая её
    новым витринным образцом.

    Возвращает (success, сообщение_для_админа).

    Не трогает resources/demo/ в репо — это default fallback и должен
    обновляться через git-commit разработчиком, не админ-командой.
    """
    async with Session() as s:
        story = await s.get(Story, story_id)
    if story is None:
        return False, f"Сказка #{story_id} не найдена в БД"

    if not story.text:
        return False, f"У сказки #{story_id} пустой текст — нечего сохранять"

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Текст: первая строка — название (как сказочник его пишет в исходном
    # выводе), потом пустая строка, потом сам текст.
    title_line = ""
    # Title в БД не хранится отдельной колонкой, берём из image_path
    # косвенно нельзя — используем простой fallback на child_name.
    # Лучше — сохраняем title из самого текста если он есть на первой строке.
    first_line, *_rest_lines = story.text.split("\n", 1)
    if first_line and len(first_line) < 100 and not first_line.endswith("."):
        # Похоже на название — оставляем как есть, текст уже его содержит
        story_dump = story.text.strip()
    else:
        # Названия не было в тексте — формируем шапку из имени ребёнка
        title_line = f"Сказка для {story.child_name}"
        story_dump = f"{title_line}\n\n{story.text.strip()}"

    (_CACHE_DIR / "story.txt").write_text(story_dump, encoding="utf-8")

    copied = ["story.txt"]
    # image_path в Story — это обложка (opening illustration). climax и
    # ending не сохраняются в БД отдельными колонками, поэтому копируем
    # только обложку как cover. climax/ending для демо генерятся или
    # подкладываются вручную в cache/demo/ — это admin's responsibility.
    if story.image_path:
        src = Path(story.image_path)
        if src.exists():
            shutil.copy2(src, _CACHE_DIR / "cover.jpg")
            copied.append("cover.jpg")
        else:
            logger.warning("image_path=%s не существует", story.image_path)

    if story.pdf_path:
        src = Path(story.pdf_path)
        if src.exists():
            shutil.copy2(src, _CACHE_DIR / "book.pdf")
            copied.append("book.pdf")
        else:
            logger.warning("pdf_path=%s не существует", story.pdf_path)

    msg = (
        f"✅ Витрина обновлена. Сохранено в {_CACHE_DIR}:\n"
        + "\n".join(f"  • {f}" for f in copied)
    )
    if "cover.jpg" not in copied:
        msg += "\n\n⚠️ Обложка не скопирована — у сказки нет image_path."
    if "book.pdf" not in copied:
        msg += "\n\n⚠️ PDF не скопирован — у сказки нет pdf_path."
    msg += (
        "\n\nclimax.jpg и ending.jpg не копируются автоматически "
        "(в БД хранится только cover). Если нужны три иллюстрации в "
        "витрине — положи их вручную в cache/demo/."
    )
    return True, msg
