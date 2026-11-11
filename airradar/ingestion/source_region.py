"""Регіональний контекст джерел: місцеві канали пишуть без назви області («Пуск КАБів у бік Юнаківка/Хотінь»).

Підказка використовується лише для (а) вибору між однойменними селами та (б) загальних попереджень без місця
(«Заходять на пуски КАБ по Сумському району») — тоді це загроза для області каналу, а не вигадана точка.
Ключ — username каналу без @ у нижньому регістрі.
"""
SOURCE_OBLAST: dict[str, str] = {
    "sumyregion": "sumska",
    "sumyliketop": "sumska",
    "kiev_nebo": "kyivska",
    "kiev_levyy_bereg": "kyivska",
    "kievskiyvanek": "kyivska",
    "poznyakyosokorkykharkivskiy": "kharkivska",
}


def oblast_for(source: str) -> str:
    return SOURCE_OBLAST.get((source or "").lower().lstrip("@"), "")
