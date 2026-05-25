"""Русская морфология для имён и общих существительных:
- petrovich — специализирован для имён/фамилий (знает Илья, Никита, Лука)
- pymorphy3 — общий движок для любых нарицательных (Котик, Лисёнок, Зайчик)

Для героев (hero) используем pymorphy3.
Для имён детей (child_name) используем petrovich."""

from __future__ import annotations

import logging

from petrovich.enums import Case, Gender
from petrovich.main import Petrovich

logger = logging.getLogger(__name__)
_p = Petrovich()

# pymorphy3 — опциональный, если не установлен, общие склонения вернут как есть
try:
    import pymorphy3
    _morph = pymorphy3.MorphAnalyzer()
except Exception as e:
    logger.warning("pymorphy3 недоступен — склонение существительных отключено: %s", e)
    _morph = None


# ─────────────────── Словарь CIS / татарских / кавказских имён ───────────────────
#
# petrovich по умолчанию знает только русские имена. CIS-имена (Амина, Айдар,
# Тимур, Айгуль и т.д.) он не распознаёт, гендер не определяет → склоняет
# криво или вообще не склоняет. pymorphy3 как fallback тоже падает (например,
# «Амина» парсится как родительный множественного от «амины» — химических
# соединений → срезает «а» → выдаёт «Амин»).
#
# Решение: явный словарь распространённых CIS/татарских/кавказских/
# узбекских/казахских имён. Если имя в словаре — подставляем гендер вручную
# до петровича. С явным гендером petrovich применяет правильное общее
# правило для имён, оканчивающихся на «-а» (женское) или согласный (мужское).

_CIS_FEMALE_NAMES = {
    "айгерим", "айгуль", "айдан", "айжан", "айна", "айсу", "айше",
    "алина", "альбина", "альфия", "амина", "анэлия", "арина", "асель",
    "асия", "балжан", "бэлла", "венера", "гузель", "гульнара", "гульнур",
    "гульшат", "дана", "данагуль", "диана", "дильнара", "динара", "дилара",
    "дарина", "элина", "эльвира", "эльза", "эльмира", "эльнара",
    "жанна", "жадиша", "зара", "зарина", "зейнаб", "зейнэп", "зухра",
    "ильмира", "индира", "ирада", "карина", "камила", "камилла",
    "лейла", "лиана", "лилия", "лия", "ляля", "ляйсан",
    "мадина", "майя", "малика", "марьям", "мариам", "медина", "милана",
    "мираль", "мунира", "наргиз", "наргиза", "нилюфар", "нурлана",
    "ралина", "раиля", "регина", "ризвана", "розалия", "роксана",
    "румия", "сабина", "сабрина", "саида", "самира", "сафия",
    "севиль", "сурия", "тамила", "фарида", "фатима",
    "хадиджа", "хава", "чулпан", "шахноза", "шарифа",
    "юлдуз", "ясмина", "ясмин",
    # ещё часто встречаются на постсоветском пространстве
    "софия", "софья", "ева", "мия", "ника", "вероника",
}

_CIS_MALE_NAMES = {
    "айбек", "айдар", "айдын", "айнур", "айрат", "акмал", "алан", "алишер",
    "альберт", "амир", "амрит", "арсен", "арслан", "артур", "ахмад", "ахмат",
    "бахрам", "бахтияр", "батыр", "берик",
    "вагиф", "вильдан",
    "габдулла", "гасан", "герман",
    "давлат", "давуд", "дамир", "даниил", "данил", "данияр", "джамал",
    "ербол", "ерлан",
    "жан", "жасур",
    "зариф",
    "ибрагим", "ильдар", "ильнур", "ильяс", "иса", "ислам", "ихсан",
    "карим", "касим",
    "магомед", "марат", "марсель", "мухаммад", "мухаммед", "мухтар",
    "наиль", "ниязбек", "нурлан",
    "омар", "ораз", "осман",
    "рамиль", "ратмир", "рахим", "рашид", "ринат", "руслан", "рустам",
    "рустем",
    "сабит", "саид", "салават", "самир", "санжар", "сафар", "сулейман",
    "тагир", "темур", "тимур",
    "фарид",
    "хабиб", "хамза", "хасан",
    "шамиль",
    "эльдар", "эмиль", "эмир",
    "юсуф",
}


def detect_name_gender(name: str) -> "Gender | None":
    """Пытается определить гендер по словарю CIS/татарских/кавказских имён.
    Возвращает Gender.FEMALE / Gender.MALE / None (если не нашли).

    None означает, что петрович сам разберётся (для классических русских имён
    он работает корректно).
    """
    if not name:
        return None
    n = name.strip().lower()
    if n in _CIS_FEMALE_NAMES:
        return Gender.FEMALE
    if n in _CIS_MALE_NAMES:
        return Gender.MALE
    return None


# Карта названий падежей: наши русские → коды pymorphy
_PYMORPHY_CASES = {
    "gent": "gent",   # родительный  (для Лисёнка)
    "datv": "datv",   # дательный    (Лисёнку)
    "accs": "accs",   # винительный  (про Лисёнка)
    "ablt": "ablt",   # творительный (с Лисёнком)
    "loct": "loct",   # предложный   (о Лисёнке)
}

# Карта петровичевских Case → pymorphy3 коды.
# Используется как fallback когда petrovich не смог склонить имя
# (характерно для имён с «ё»: Алёна, Семён, Артём).
_PETROVICH_TO_PYMORPHY = {
    Case.GENITIVE: "gent",
    Case.DATIVE: "datv",
    Case.ACCUSATIVE: "accs",
    Case.INSTRUMENTAL: "ablt",
    Case.PREPOSITIONAL: "loct",
}


def _capitalize_like(target: str, source: str) -> str:
    """Сохраняет первую заглавную, если source начинался с заглавной."""
    if not target or not source:
        return target
    if source[0].isupper():
        return target[0].upper() + target[1:]
    return target


def _decline_noun(word: str, pymorphy_case: str) -> str:
    """Склоняет нарицательное существительное (Котик/Лисёнок) через pymorphy3.
    На любой ошибке/незнании — возвращает как есть."""
    if not _morph or not word:
        return word
    try:
        parts = []
        # На случай составных героев типа «Робот-садовник» — склоняем каждую часть
        for token in word.split("-"):
            sub_parts = []
            for sub in token.split():
                if not sub:
                    continue
                parsed = _morph.parse(sub)[0]
                inflected = parsed.inflect({pymorphy_case})
                if inflected:
                    sub_parts.append(_capitalize_like(inflected.word, sub))
                else:
                    sub_parts.append(sub)
            parts.append(" ".join(sub_parts))
        return "-".join(parts)
    except Exception as e:
        logger.debug("pymorphy не смог склонить '%s' в %s: %s", word, pymorphy_case, e)
        return word


def hero_genitive(word: str) -> str:
    """Родительный для героя: «для Котика», «у Лисёнка»."""
    return _decline_noun(word, "gent")


def hero_dative(word: str) -> str:
    """Дательный для героя: «Котику», «Лисёнку»."""
    return _decline_noun(word, "datv")


def hero_accusative(word: str) -> str:
    """Винительный для героя: «про Котика», «про Лисёнка»."""
    return _decline_noun(word, "accs")


def hero_instrumental(word: str) -> str:
    """Творительный для героя: «с Котиком», «рядом с Лисёнком»."""
    return _decline_noun(word, "ablt")


def hero_prepositional(word: str) -> str:
    """Предложный для героя: «о Котике», «о Лисёнке»."""
    return _decline_noun(word, "loct")


def normalize_name(name: str) -> str:
    """Приводит «ЛИза», «лиза», «ЛИЗА», «лИзА» → «Лиза».
    Для «анна-мария» → «Анна-Мария». Сохраняет имена с дефисом."""
    name = (name or "").strip()
    if not name:
        return ""
    # Делим по дефисам, нормализуем каждую часть отдельно
    parts = []
    for p in name.split("-"):
        p = p.strip()
        if not p:
            continue
        parts.append(p[0].upper() + p[1:].lower())
    return "-".join(parts)


# Безопасные символы: буквы (русские/латин), цифры, пробелы, дефисы и
# базовая пунктуация. Всё остальное (HTML, JSON, спецсимволы,
# prompt-injection токены вроде ``` или """) вырезаем.
import re as _re
_SAFE_INPUT_RE = _re.compile(r"[^\w\sа-яА-ЯёЁ\-.,!?'’ʼ]", flags=_re.UNICODE)


# Маркеры эмоций для ElevenLabs ([laughs softly], [sighs], [whispers] и т.п.)
# Gemini вставляет их в текст сказки, чтобы озвучка звучала живо.
# Пользователю показываем БЕЗ маркеров — они нужны только для TTS.
_EMO_MARKER_RE = _re.compile(r"\[[^\]\n]{1,40}\]")


def strip_emo_markers(text: str) -> str:
    """Удаляет [laughs softly], [sighs], [whispers], [warmly] и подобные
    маркеры из текста перед отображением пользователю. Возвращает
    «человеческий» текст. Используется только для UI/Telegram, для TTS
    оставляем оригинал с маркерами."""
    if not text:
        return text
    # Убираем маркеры
    cleaned = _EMO_MARKER_RE.sub("", text)
    # Схлопываем двойные пробелы и пробелы перед знаками препинания
    cleaned = _re.sub(r"\s+([,.!?;:])", r"\1", cleaned)
    cleaned = _re.sub(r" {2,}", " ", cleaned)
    # Удаляем пустые строки (если маркер занимал всю строку)
    cleaned = _re.sub(r"\n[ \t]+\n", "\n\n", cleaned)
    return cleaned.strip()


def sanitize_user_text(text: str, max_len: int = 64) -> str:
    """Очистка пользовательского ввода перед подстановкой в LLM-промпт
    и записью в БД.

    Что делает:
    - Обрезает длину до `max_len` (защита от 5000-символьной «инструкции»
      для LLM, переданной как имя ребёнка)
    - Удаляет управляющие/спецсимволы, оставляет буквы / цифры / пробелы /
      тире / базовую пунктуацию
    - Схлопывает многократные пробелы
    - Trim'ит края

    Применять к: child_name, hero, theme, кастомному посланию подарка."""
    if not text:
        return ""
    cleaned = _SAFE_INPUT_RE.sub("", text)
    cleaned = " ".join(cleaned.split())
    return cleaned[:max_len].strip()


def _decline(name: str, case: Case, gender: Gender | None = None) -> str:
    """Склоняет имя. Сначала petrovich (он точнее для редких имён вроде Илья,
    Никита, Лука), потом, если результат не изменился, — pymorphy3 как fallback
    (работает с именами на «ё»: Алёна, Семён, Артём).

    Если gender не задан явно — пытаемся определить через словарь CIS-имён
    (Амина, Айдар, Тимур, Айгуль и т.п.), которых petrovich не знает в лицо."""
    if not name:
        return name
    # Составные имена через дефис — рекурсия
    if "-" in name:
        return "-".join(_decline(p, case, gender) for p in name.split("-"))

    # Авто-детект гендера для CIS/татарских/кавказских имён.
    # Petrovich со ЯВНЫМ гендером применяет правильное общее правило
    # для имён на -а (женское) или согласный (мужское). Без гендера —
    # пасует на незнакомых именах, и pymorphy3 fallback может выдать
    # «Амина» → «Амин» (родительный множественного от «амины»).
    if gender is None:
        gender = detect_name_gender(name)

    result = name
    try:
        result = _p.firstname(name, case, gender=gender) if gender else _p.firstname(name, case)
    except Exception as e:
        logger.debug("petrovich не смог склонить '%s' в %s: %s", name, case, e)

    # Если petrovich не справился (вернул то же что было) — пробуем pymorphy3
    if result == name:
        py_case = _PETROVICH_TO_PYMORPHY.get(case)
        if py_case:
            result = _decline_noun(name, py_case)
    return result


def genitive(name: str, gender: Gender | None = None) -> str:
    """Родительный: «для Лизы», «у Тимофея»."""
    return _decline(name, Case.GENITIVE, gender)


def dative(name: str, gender: Gender | None = None) -> str:
    """Дательный: «Лизе», «Тимофею»."""
    return _decline(name, Case.DATIVE, gender)


def accusative(name: str, gender: Gender | None = None) -> str:
    """Винительный: «вижу Лизу», «знаю Тимофея»."""
    return _decline(name, Case.ACCUSATIVE, gender)


def instrumental(name: str, gender: Gender | None = None) -> str:
    """Творительный: «рядом с Лизой», «с Тимофеем»."""
    return _decline(name, Case.INSTRUMENTAL, gender)


def prepositional(name: str, gender: Gender | None = None) -> str:
    """Предложный: «о Лизе», «о Тимофее»."""
    return _decline(name, Case.PREPOSITIONAL, gender)
