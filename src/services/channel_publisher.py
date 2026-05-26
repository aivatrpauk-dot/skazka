"""Ежедневная публикация сказки в Telegram-канал — воронка продаж.

Каждый день в configured CHANNEL_PUBLISH_HOUR_MSK бот выбирает случайное
имя ребёнка из CIS-списка, генерирует сказку (теми же промптами и моделью
что и платный продукт) с ОДНОЙ обложкой (не тремя — для экономии в
канальной воронке), собирает облегчённый PDF и постит в канал:

    [cover photo с caption]
    [PDF документ]

Caption — рандомизация из 15 шаблонов, человеческая интонация.
Каждый CHANNEL_CTA_EVERY_N-й пост (по умолчанию 3-й) содержит CTA-блок
со ссылкой на сам бот для персонализированной сказки.

Состояние (счётчик постов + дата последней публикации) хранится в файле
cache/channel_state.json — переживает рестарты, проще чем DB-таблица,
обновляется атомарно.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import random
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile

from ..config import config
from ..prompts import THEME_CHOICES
from .image import generate_cover
from .llm import extract_scene, generate_story
from .pdf_book import build_story_pdf
from .story_params import pick_params

logger = logging.getLogger(__name__)


# ─────────────── Имена для ротации (только персонажные) ───────────────
# Anti-cannibalization: в канале НЕТ обычных CIS-имён (Маша, Ваня, Аня).
# Если бы они были, родитель ребёнка по имени Маша мог бы взять PDF из
# канала и не идти в бот — это убивает мотивацию подписки. Поэтому в
# канал идут только «персонажные» имена: либо классические сказочные
# (Тимоша, Кузя, Капитошка), либо смешные/природные (Морошка, Бубуля,
# Каркуша). Это явно «имя героя сказки», а не «имя моего ребёнка».
#
# Зритель видит «бот пишет хорошую персональную сказку про конкретного
# героя» → хочет такую же с именем своего ребёнка → идёт в бот.
_CHANNEL_NAMES = [
    # Сказочные имена из русских книг и мультиков
    "Тимоша", "Тёпа", "Топа", "Топтыжка", "Кузя", "Капитошка",
    "Бим", "Снежок", "Дружок", "Тимка", "Платоша",
    # Старо-русские редкие, сейчас не дают детям
    "Тася", "Глаша", "Феня", "Поля", "Дуся",
    # Природа/предмет как имя-образ (стиль Чарушина, Бианки)
    "Морошка", "Капелька", "Земляничка",
    # Лёгкие иностранные/фантазийные
    "Том", "Финик",
    # Смешные характерные
    "Бубуля", "Чудик", "Каркуша", "Хрюша",
]

# Возраст канальной сказки — серединка нашего диапазона 3-6.
# 5 лет = старший промпт (SYSTEM_STORYTELLER_5_6), более насыщенный текст,
# который и для просмотра в канале выглядит ярче.
_CHANNEL_CHILD_AGE = 5


# ─────────────── Caption-шаблоны ───────────────
# Два плейсхолдера для разных падежей:
#   {name_nom} — именительный («Главный герой — Хрюша»)
#   {name_acc} — винительный («сказка про Хрюшу»)
# Склонение делает pymorphy3 в hero_accusative — он справляется с
# нарицательно-животными именами типа «Хрюша/Кузя/Том».
# Гендерных местоимений (его/её, он/она) намеренно избегаем — список
# имён смешанного рода, не должно сломаться ни на одном.
_CAPTIONS = [
    "🌙 Сегодняшняя сказка — про {name_acc}. Уютной ночи всем нашим маленьким слушателям 💛",
    "📖 Вечерняя сказка для самых уютных деток. Сегодня — про {name_acc} 🌟",
    "✨ Включайте лампу, читайте детишкам перед сном. Сегодня в гостях у нас — {name_nom}",
    "🕯 Сказка на ночь — про {name_acc}. Пусть согреет ваш вечер ✨",
    "💫 Каждый вечер — новая сказка. Сегодня — про {name_acc}. Доброго сна, маленькие читатели 🌙",
    "📕 Свежая вечерняя сказка — про {name_acc}. Пусть детям приснится что-нибудь доброе и тёплое",
    "🌙 На сегодня сказка — про {name_acc}. Забирайте PDF, читайте всей семьёй перед сном",
    "✨ Каждый закат — новая сказка. Сегодняшняя — про {name_acc}. Хорошего вечера, дорогие 💛",
    "🌟 Сказка на сегодня для наших любимых деток — про {name_acc}. Пусть ночь будет ласковой",
    "📖 Тёплая вечерняя сказка — про {name_acc}. Сохраняйте PDF, чтобы читать перед сном",
    "🕯 Сказка перед сном — наша ежедневная традиция. Сегодня встречаем нового героя: {name_nom}",
    "💫 На ночь — сказка про {name_acc}. PDF можно скачать и читать каждый вечер",
    "🌙 Уютная сказка на сегодня. Главный герой — {name_nom}. Пусть детям спится сладко",
    "✨ Доброго вечера! Сегодняшняя сказка — про {name_acc}. Скачивайте PDF и читайте вслух",
    "📖 Вечерняя сказка от Сказочника. Сегодня — про {name_acc}. Тёплой ночи, добрых снов 🌟",
]

# CTA-блок добавляется к caption каждый N-й раз. Бот @username подставляется
# динамически из bot.get_me() — нет хардкода.
_CTA_TEMPLATE = (
    "\n\n— — —\n"
    "💌 А хотите, чтобы герой сказки носил имя ВАШЕГО ребёнка?\n"
    "Откройте @{bot_username} — Сказочник напишет персональную сказку "
    "за минуту, с именем, любимым героем и тёплой картинкой."
)


# ─────────────── Файл состояния ───────────────
# В одном небольшом JSON-файле: счётчик постов (для определения CTA-такта)
# и дата последней публикации (для защиты от двойных постов при рестарте).
def _state_path() -> Path:
    """cache/channel_state.json — рядом с другими кэшами."""
    p = Path(config.audio_cache_dir).parent / "channel_state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _read_state() -> dict:
    """Читает состояние или возвращает дефолт."""
    path = _state_path()
    if not path.exists():
        return {"total_posts": 0, "last_posted_date": None}
    try:
        return json.loads(path.read_text())
    except Exception as e:
        logger.warning("channel_state.json повреждён, начинаю с нуля: %s", e)
        return {"total_posts": 0, "last_posted_date": None}


def _write_state(state: dict) -> None:
    """Атомарная запись через temp-файл + rename."""
    path = _state_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.replace(path)


# ─────────────── Дата по МСК ───────────────
_MSK_TZ = dt.timezone(dt.timedelta(hours=3))


def _today_msk_iso() -> str:
    """Текущая дата по МСК в ISO. Используется как ключ «уже постили сегодня»."""
    return dt.datetime.now(dt.timezone.utc).astimezone(_MSK_TZ).date().isoformat()


# ─────────────── Главная функция ───────────────

async def publish_to_channel(bot: Bot, *, force: bool = False) -> bool:
    """Полный flow одной публикации: сгенерить → собрать → запостить.

    Возвращает True если опубликовано (или уже было сегодня — это тоже
    «успех», ничего не делаем). False если случилась ошибка.

    force=False (по умолчанию) — идемпотентно: если за сегодня уже
    постили, пропускаем. Это защита от рестарта бота в момент срабатывания
    scheduler'а.

    force=True — обходим проверку идемпотентности. Используется командой
    /seed_channel для бэкфилла (нужно сделать N постов подряд для
    наполнения канала перед запуском рекламы).
    """
    if not config.channel_publish_enabled:
        logger.info("CHANNEL_PUBLISH_ENABLED=false — пропускаю публикацию")
        return False
    if not config.channel_id:
        logger.warning("CHANNEL_ID не задан, не могу публиковать")
        return False

    state = _read_state()
    today = _today_msk_iso()
    if not force and state.get("last_posted_date") == today:
        logger.info("Сегодня (%s) уже постили в канал — пропускаю", today)
        return True  # уже сделано, не ошибка

    try:
        # 1. Выбираем имя/возраст/параметры
        name = random.choice(_CHANNEL_NAMES)
        params = pick_params(
            child_age=_CHANNEL_CHILD_AGE,
            used_architectures=None,
            used_humors=None,
            used_openings=None,
            used_tones=None,
            last_category=None,
        )
        logger.info(
            "Channel: имя=%s, возраст=%d, форма=%s, юмор=%s, жанр=%s, "
            "зачин=%s, интонация=%s",
            name, _CHANNEL_CHILD_AGE,
            params.form, params.humor, params.genre, params.opening, params.tone,
        )

        # 2. Генерируем сказку (тот же сказочник что и платный продукт),
        # но с флагом is_channel_post=True — это переопределит финальное
        # пожелание перед сном на обращение ко всем зрителям канала во
        # множественном числе («детишки», «друзья»), без упоминания
        # имени героя. Аудитория канала — сотни разных детей, не Маша.
        text, story_title, scenes = await generate_story(
            child_name=name,
            child_age=_CHANNEL_CHILD_AGE,
            form=params.form,
            humor=params.humor,
            genre=params.genre,
            opening=params.opening,
            tone=params.tone,
            paid_quality=True,
            is_channel_post=True,
        )
        from ..utils import strip_emo_markers
        display_text = strip_emo_markers(text)

        # 3. Одна обложка — экономия. Сцена берётся НЕ из SCENES-блока
        # (он по нашей инструкции содержит «параллельный мир сказки»,
        # т.е. что ЕЩЁ могло бы там происходить — это даёт шаблонные
        # картинки про обитателей мира на пикнике), а через extract_scene:
        # отдельный Gemini-вызов, который читает текст сказки и
        # формулирует конкретную визуально-сильную сцену из НЕЁ.
        # Так обложка отражает сам сюжет, а не выдуманный мир вокруг.
        story_scene = await extract_scene(display_text)
        if not story_scene:
            # Fallback: если Gemini-ключ не настроен или extract_scene
            # упал — используем opening из SCENES-блока (старое поведение).
            story_scene = (scenes or {}).get("opening") if scenes else None

        # theme_key и hero не критичны для канальной обложки — наш
        # стиль уже всё определяет. Передаём пустую строку для hero
        # и фиктивный theme_key из имеющихся.
        theme_key = random.choice(list(THEME_CHOICES.keys()))
        cover_path = await generate_cover(
            hero="",
            theme_key=theme_key,
            scene_description=story_scene,
            stage="opening",
        )

        # 4. PDF с одной картинкой. build_story_pdf уже умеет принимать
        # None для climax/ending — просто пропустит их страницы.
        from ..utils import genitive as _gen
        book_title = story_title or f"Сказка для {_gen(name)}"
        try:
            theme_phrase = THEME_CHOICES[theme_key][2]
        except (KeyError, IndexError):
            theme_phrase = ""
        pdf_path = build_story_pdf(
            title=book_title,
            subtitle=theme_phrase,
            text=display_text,
            cover_image=cover_path,
            climax_image=None,
            ending_image=None,
        )

        # 5. Caption + опционально CTA. Считаем такт по обновлённому
        # total_posts: если это будет N-й по счёту пост (1-based) — CTA.
        new_total = (state.get("total_posts") or 0) + 1
        is_cta_post = (
            config.channel_cta_every_n > 0
            and new_total % config.channel_cta_every_n == 0
        )
        # Подставляем имя в нужных падежах. hero_accusative через pymorphy3
        # знает что «Хрюша → Хрюшу», «Кузя → Кузю», «Том → Тома».
        from ..utils import hero_accusative
        caption = random.choice(_CAPTIONS).format(
            name_nom=name,
            name_acc=hero_accusative(name),
        )
        if is_cta_post:
            me = await bot.get_me()
            caption += _CTA_TEMPLATE.format(bot_username=me.username)

        # 6. Постим в канал — сначала фото с caption, потом PDF.
        if cover_path and cover_path.exists():
            await bot.send_photo(
                config.channel_id,
                FSInputFile(str(cover_path)),
                caption=caption,
            )
        else:
            # Без обложки шлём caption отдельным сообщением.
            await bot.send_message(config.channel_id, caption)

        if pdf_path and pdf_path.exists():
            safe_name = f"{book_title}.pdf".replace("/", "-")
            await bot.send_document(
                config.channel_id,
                FSInputFile(str(pdf_path), filename=safe_name),
            )
        else:
            logger.warning("PDF не собрался, шлю в канал только обложку")

        # 7. Обновляем состояние ТОЛЬКО после успешной отправки.
        _write_state({
            "total_posts": new_total,
            "last_posted_date": today,
        })
        logger.info(
            "Опубликовано в канал: %s, имя=%s, post #%d%s",
            config.channel_id, name, new_total,
            " (с CTA)" if is_cta_post else "",
        )
        return True

    except Exception as e:
        logger.exception("Сбой публикации в канал: %s", e)
        return False


async def maybe_publish_on_startup(bot: Bot) -> None:
    """При старте бота: если сегодня уже после CHANNEL_PUBLISH_HOUR_MSK
    и за сегодня в канал ничего не уходило — публикуем немедленно.

    Это защита от ситуации «бот лежал в 18:00, поднялся в 19:30 — без
    этой логики канал бы просто пропустил день».
    """
    if not config.channel_publish_enabled or not config.channel_id:
        return
    now_msk = dt.datetime.now(dt.timezone.utc).astimezone(_MSK_TZ)
    if now_msk.hour < config.channel_publish_hour_msk:
        return  # время ещё не настало, scheduler сам выстрелит
    state = _read_state()
    if state.get("last_posted_date") == _today_msk_iso():
        return  # уже постили сегодня
    logger.info(
        "Канал: сегодня пост ещё не уходил, а время уже %s МСК > %s — публикую",
        now_msk.strftime("%H:%M"), config.channel_publish_hour_msk,
    )
    # Запускаем в фоне, не блокируем bootstrap бота.
    asyncio.create_task(publish_to_channel(bot))
