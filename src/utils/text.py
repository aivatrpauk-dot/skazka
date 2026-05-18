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


def _decline(name: str, case: Case, gender: Gender | None = None) -> str:
    """Склоняет имя. Сначала petrovich (он точнее для редких имён вроде Илья,
    Никита, Лука), потом, если результат не изменился, — pymorphy3 как fallback
    (работает с именами на «ё»: Алёна, Семён, Артём)."""
    if not name:
        return name
    # Составные имена через дефис — рекурсия
    if "-" in name:
        return "-".join(_decline(p, case, gender) for p in name.split("-"))

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
