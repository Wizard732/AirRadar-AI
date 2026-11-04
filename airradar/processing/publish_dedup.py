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
        """True — публікувати. Побічний ефект: запам'ятовує опубліковане (див. reserve/release)."""
        return self.reserve(weapon, parsed, regions, now) is not None

    def reserve(self, weapon: str, parsed, regions: list[str], now: float | None = None):
        """Як allow, але повертає токен скасування (або None, якщо публікувати не треба).

        Запис «опубліковано» робиться до фактичної відправки; якщо відправка не вдалась,
        викликають release(token) — повтор того ж повідомлення не буде відкинутий як дубль.
        """
        now = time.time() if now is None else now
        if weapon == "stand_down":
            if self._standdown_ts is not None and now - self._standdown_ts < STANDDOWN_WINDOW_S:
                return None
            prev = self._standdown_ts
            self._standdown_ts = now
            return ("standdown", prev, now)
        if weapon in STRATEGIC:
            prev_entry = self._strategic.get(weapon)
            seen_at, seen = prev_entry or (0.0, set())
            if now - seen_at > STRATEGIC_WINDOW_S:
                seen = set()
            regs = {r for r in regions if r and r not in ("unknown", "multi")}
            if seen_at and now - seen_at <= STRATEGIC_WINDOW_S and (not regs or regs <= seen):
                return None
            self._strategic[weapon] = (now, seen | regs)
            return ("strategic", weapon, prev_entry, (now, seen | regs))
        token = ("none",)
        if parsed is not None and parsed.kind == "track" and parsed.observations:
            sig = frozenset((o.oblast, o.place or "*") for o in parsed.observations if o.relation != "from")
            self._uav = [e for e in self._uav if now - e[0] <= UAV_WINDOW_S]
            if sig:
                known = set().union(*(s for _, s in self._uav)) if self._uav else set()
                if known and len(sig - known) / len(sig) < NEW_SHARE:
                    return None
                entry = (now, sig)
                self._uav.append(entry)
                token = ("uav", entry)
        return token

    def release(self, token) -> None:
        """Скасувати запис, зроблений reserve(), якщо публікація не відбулась."""
        if not token:
            return
        kind = token[0]
        if kind == "standdown" and self._standdown_ts == token[2]:
            self._standdown_ts = token[1]
        elif kind == "strategic" and self._strategic.get(token[1]) == token[3]:
            if token[2] is None:
                self._strategic.pop(token[1], None)
            else:
                self._strategic[token[1]] = token[2]
        elif kind == "uav":
            if token[1] in self._uav:
                self._uav.remove(token[1])
