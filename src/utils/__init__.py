from .text import (
    normalize_name,
    # Для имён (petrovich)
    genitive,
    dative,
    accusative,
    instrumental,
    prepositional,
    # Для героев — нарицательных существительных (pymorphy3)
    hero_genitive,
    hero_dative,
    hero_accusative,
    hero_instrumental,
    hero_prepositional,
)

__all__ = [
    "normalize_name",
    "genitive",
    "dative",
    "accusative",
    "instrumental",
    "prepositional",
    "hero_genitive",
    "hero_dative",
    "hero_accusative",
    "hero_instrumental",
    "hero_prepositional",
]
