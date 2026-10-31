"""publish_dedup.py — не публікувати в канал те саме по колу.

Канали мониторингу повторюють один і той самий вихід десятки разів (кожен зі своїм текстом), а зведення по
областях приходять кожні кілька хвилин із майже тим самим списком міст. Тому перед публікацією перевіряємо,
чи є в цьому повідомленні щось НОВЕ відносно вже опублікованого за останні хвилини:

* ракети/балістика: одна пачка постів про загрозу = один пост, поки не з'явилась нова область;
* БпЛА-зведення: публікуємо, якщо нових (область, місто) не менше 40%;
* відбій без області («🟢 ВІДБІЙ — 09:05»): не частіше ніж раз на 20 хв.
Усе пишеться в БД як і раніше — відсікається лише публікація.
"""
from __future__ import annotations

import time

STRATEGIC = {"ballistic", "cruise_missile", "air_missile", "coastal_missile", "missile"}
STRATEGIC_WINDOW_S = 10 * 60
UAV_WINDOW_S = 20 * 60
NEW_SHARE = 0.4
STANDDOWN_WINDOW_S = 20 * 60


class PublishDedup:
    def __init__(self) -> None:
        self._strategic: dict[str, tuple[float, set[str]]] = {}
        self._uav: list[tuple[float, frozenset]] = []
        self._standdown_ts: float | None = None

    def allow(self, weapon: str, parsed, regions: list[str], now: float | None = None) -> bool:
        """True — публікувати. Побічний ефект: запам'ятовує опубліковане."""
        now = time.time() if now is None else now
        if weapon == "stand_down":
            if self._standdown_ts is not None and now - self._standdown_ts < STANDDOWN_WINDOW_S:
                return False
            self._standdown_ts = now
            return True
        if weapon in STRATEGIC:
            seen_at, seen = self._strategic.get(weapon, (0.0, set()))
            if now - seen_at > STRATEGIC_WINDOW_S:
                seen = set()
            regs = {r for r in regions if r and r not in ("unknown", "multi")}
            if seen_at and now - seen_at <= STRATEGIC_WINDOW_S and (not regs or regs <= seen):
                return False
            self._strategic[weapon] = (now, seen | regs)
            return True
        if parsed is not None and parsed.kind == "track" and parsed.observations:
            sig = frozenset((o.oblast, o.place or "*") for o in parsed.observations if o.relation != "from")
            self._uav = [(t, s) for t, s in self._uav if now - t <= UAV_WINDOW_S]
            if sig:
                known = set().union(*(s for _, s in self._uav)) if self._uav else set()
                if known and len(sig - known) / len(sig) < NEW_SHARE:
                    return False
                self._uav.append((now, sig))
        return True
