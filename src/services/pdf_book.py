"""PDF-генерация сказочной книжки.

Структура книжки:
  Стр. 1   — обложка: большая иллюстрация + название («Сказка про Машу и
             робота Мику, о том, как хорошо быть честным»)
  Стр. 2+  — первая половина текста
  Стр. N   — иллюстрация-кульминация (на всю страницу)
  Стр. N+1 — вторая половина текста
  Стр. N+M — финальная иллюстрация (на всю страницу)

Формат: A5 (148×210 мм) — стандартный книжный размер для детских книжек.
Шрифт: DejaVu Serif 14pt с line-spacing 1.7 (комфортно для чтения вслух).
Кириллица обеспечивается шрифтом DejaVu (поставляется в Dockerfile через
fonts-dejavu-core пакет в /usr/share/fonts/truetype/dejavu/)."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Optional

from reportlab.lib.pagesizes import A5
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    Image,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
)
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY

from ..config import config

logger = logging.getLogger(__name__)

# ─────────────────────── Регистрация шрифтов с кириллицей ───────────────────────
# DejaVu — поставляется в Debian/Ubuntu пакетом fonts-dejavu-core.
# Если на хосте/в контейнере шрифта нет — fallback на Helvetica (без кириллицы).
_FONTS_REGISTERED = False
_FONT_REGULAR = "Helvetica"
_FONT_BOLD = "Helvetica-Bold"
_FONT_ITALIC = "Helvetica-Oblique"

_DEJAVU_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",          # Debian/Ubuntu
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf",
    "/Library/Fonts/DejaVuSerif.ttf",                            # macOS user-installed
    "/System/Library/Fonts/Supplemental/Times New Roman.ttf",   # macOS системный fallback
]


def _register_fonts() -> None:
    """Регистрирует TTF-шрифты с кириллицей. Безопасно вызывать повторно."""
    global _FONTS_REGISTERED, _FONT_REGULAR, _FONT_BOLD, _FONT_ITALIC
    if _FONTS_REGISTERED:
        return

    # Пробуем DejaVu Serif (есть в Debian/Ubuntu образе)
    regular_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
        "/Library/Fonts/DejaVuSerif.ttf",
    ]
    bold_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
        "/Library/Fonts/DejaVuSerif-Bold.ttf",
    ]
    italic_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf",
        "/Library/Fonts/DejaVuSerif-Italic.ttf",
    ]

    def _first_existing(paths: list[str]) -> Optional[str]:
        for p in paths:
            if Path(p).exists():
                return p
        return None

    reg = _first_existing(regular_paths)
    bld = _first_existing(bold_paths)
    ita = _first_existing(italic_paths)

    if reg:
        try:
            pdfmetrics.registerFont(TTFont("BookSerif", reg))
            _FONT_REGULAR = "BookSerif"
        except Exception as e:
            logger.warning("Не зарегистрировался regular TTF: %s", e)
    if bld:
        try:
            pdfmetrics.registerFont(TTFont("BookSerif-Bold", bld))
            _FONT_BOLD = "BookSerif-Bold"
        except Exception as e:
            logger.warning("Не зарегистрировался bold TTF: %s", e)
    if ita:
        try:
            pdfmetrics.registerFont(TTFont("BookSerif-Italic", ita))
            _FONT_ITALIC = "BookSerif-Italic"
        except Exception as e:
            logger.warning("Не зарегистрировался italic TTF: %s", e)

    _FONTS_REGISTERED = True
    if reg is None:
        logger.warning(
            "DejaVu Serif не найден — PDF будет без кириллицы! "
            "Поставь fonts-dejavu-core в Dockerfile."
        )


# ─────────────────────── Стили параграфов ───────────────────────

def _styles() -> dict:
    """Стили для книжной вёрстки. Вызывать ПОСЛЕ _register_fonts()."""
    return {
        "title": ParagraphStyle(
            name="title",
            fontName=_FONT_BOLD,
            fontSize=22,
            leading=28,
            alignment=TA_CENTER,
            textColor="#2a2a2a",
            spaceAfter=18,
        ),
        "subtitle": ParagraphStyle(
            name="subtitle",
            fontName=_FONT_ITALIC,
            fontSize=14,
            leading=18,
            alignment=TA_CENTER,
            textColor="#666666",
            spaceAfter=24,
        ),
        "body": ParagraphStyle(
            name="body",
            fontName=_FONT_REGULAR,
            fontSize=13,
            leading=22,            # ~1.7 line-spacing — комфортно вслух
            alignment=TA_JUSTIFY,
            textColor="#1a1a1a",
            spaceAfter=10,
            firstLineIndent=12,    # красная строка
        ),
        "credit": ParagraphStyle(
            name="credit",
            fontName=_FONT_ITALIC,
            fontSize=9,
            leading=12,
            alignment=TA_CENTER,
            textColor="#999999",
            spaceBefore=20,
        ),
    }


# ─────────────────────── Главная функция ───────────────────────

def build_story_pdf(
    *,
    title: str,
    subtitle: str | None,
    text: str,
    cover_image: Path | None,
    climax_image: Path | None,
    ending_image: Path | None,
    out_path: Path | None = None,
) -> Path:
    """Собирает PDF-книжку и возвращает путь к файлу.

    Параметры:
      title — «Сказка про Машу и робота Мику» (большой заголовок на обложке).
      subtitle — «о том, как хорошо быть честным» (подзаголовок-фраза темы).
      text — полный текст сказки (с переносами строк \\n\\n между абзацами).
      cover_image, climax_image, ending_image — пути к 3 иллюстрациям.
        Любой из них может быть None — тогда страница с этим изображением
        просто не вставляется.
      out_path — куда сохранить. Если None — генерится по хэшу в cache/pdf/.

    Кэширование: если out_path не задан, ключ кэша = sha256(title|text|images),
    повторный вызов с теми же данными вернёт готовый файл.
    """
    _register_fonts()

    if out_path is None:
        cache_key = (
            title + "|" + (subtitle or "") + "|" + text + "|"
            + str(cover_image) + "|" + str(climax_image) + "|" + str(ending_image)
        )
        digest = hashlib.sha256(cache_key.encode()).hexdigest()[:24]
        cache_dir = Path(config.audio_cache_dir).parent / "pdf"
        cache_dir.mkdir(parents=True, exist_ok=True)
        out_path = cache_dir / f"story_{digest}.pdf"
        if out_path.exists():
            return out_path

    out_path.parent.mkdir(parents=True, exist_ok=True)

    page_w, page_h = A5
    # Поля страницы — щедрые для комфортного чтения
    margin = 16 * mm

    doc = BaseDocTemplate(
        str(out_path),
        pagesize=A5,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=margin,
        title=title,
        author="Сказка",
    )
    frame = Frame(
        margin, margin,
        page_w - 2 * margin, page_h - 2 * margin,
        id="main", showBoundary=0,
    )
    doc.addPageTemplates([PageTemplate(id="default", frames=[frame])])

    styles = _styles()
    story: list = []

    # ─── Стр. 1 — название + первая иллюстрация ───
    # Отдельной обложки нет (продукт чище). Заголовок маленьким сверху,
    # opening-иллюстрация под ним крупно, затем сразу первая половина
    # текста — без перехода на новую страницу.
    story.append(Paragraph(_escape(title), styles["title"]))
    if cover_image and cover_image.exists():
        story.append(Spacer(1, 4 * mm))
        story.append(_fit_image(cover_image, page_w - 2 * margin, (page_h - 2 * margin) * 0.55))
        story.append(Spacer(1, 6 * mm))

    # ─── Текст: первая половина ───
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    mid = len(paragraphs) // 2 if len(paragraphs) > 1 else len(paragraphs)
    first_half = paragraphs[:mid]
    second_half = paragraphs[mid:]

    for p in first_half:
        story.append(Paragraph(_escape(p), styles["body"]))

    # ─── Иллюстрация-кульминация (на всю страницу) ───
    if climax_image and climax_image.exists():
        story.append(PageBreak())
        story.append(Spacer(1, 5 * mm))
        story.append(_fit_image(climax_image, page_w - 2 * margin, (page_h - 2 * margin) * 0.85))
        story.append(PageBreak())

    # ─── Вторая половина текста ───
    for p in second_half:
        story.append(Paragraph(_escape(p), styles["body"]))

    # ─── Финальная иллюстрация ───
    if ending_image and ending_image.exists():
        story.append(PageBreak())
        story.append(Spacer(1, 5 * mm))
        story.append(_fit_image(ending_image, page_w - 2 * margin, (page_h - 2 * margin) * 0.85))

    # ─── Подпись бренда внизу последней страницы ───
    # Музыкальных кредитов больше нет — фоновая музыка из продукта убрана.
    story.append(Spacer(1, 8 * mm))
    story.append(Paragraph(
        f"Создано в {config.bot_brand} · персональная поучительная сказка на ночь",
        styles["credit"],
    ))

    doc.build(story)
    logger.info("PDF готов: %s (%.1f KB)", out_path.name, out_path.stat().st_size / 1024)
    return out_path


# ─────────────────────── Helpers ───────────────────────

def _escape(s: str) -> str:
    """Базовое экранирование для Paragraph (ReportLab парсит мини-XML)."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _fit_image(path: Path, max_w: float, max_h: float) -> Image:
    """Помещает картинку в указанные размеры (max_w/max_h в пунктах),
    сохраняя пропорции. ReportLab Image сам это делает если задать
    width и height, но без сохранения aspect ratio. Поэтому считаем сами."""
    try:
        from PIL import Image as PILImage
        with PILImage.open(path) as im:
            orig_w, orig_h = im.size
        ratio = orig_w / orig_h if orig_h else 1.0
        # Подгоняем чтобы влезло и по ширине и по высоте
        w = max_w
        h = w / ratio
        if h > max_h:
            h = max_h
            w = h * ratio
        return Image(str(path), width=w, height=h)
    except Exception as e:
        logger.warning("PIL fit_image failed: %s — рисую как есть", e)
        return Image(str(path), width=max_w, height=max_h)
