"""trust.py — AirRadar Trust: рівень довіри до інформації (не «індекс небезпеки»).

Чисті функції без БД: їх викликає конвеєр і перевіряють тести/бенчмарк.
Рівні: official > confirmed > preliminary > uncertain; stale — окремий стан «застаріло».
Відсотків упевненості LLM тут немає навмисно: відкаліброваної моделі ймовірностей нема.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from source_policy import source_group


class Confidence(str, Enum):
    OFFICIAL = "official"
    CONFIRMED = "confirmed"
    PRELIMINARY = "preliminary"
    UNCERTAIN = "uncertain"
    STALE = "stale"


LABELS_UK = {
    Confidence.OFFICIAL: "Офіційне джерело",
    Confidence.CONFIRMED: "Підтверджено кількома незалежними джерелами",
    Confidence.PRELIMINARY: "Попередньо: одне джерело",
    Confidence.UNCERTAIN: "Даних недостатньо",
    Confidence.STALE: "Інформація застаріла",
}

DEFAULT_TTL_S = 30 * 60        # після цього подія вважається застарілою
STALE_LAG_S = 10 * 60          # пост обробили пізніше ніж через 10 хв після публікації — не «свіже»


@dataclass(frozen=True)
class Provenance:
    """Походження події: три часи + незалежні джерела."""
    observed_at: int                     # коли подія була повідомлена джерелом
    received_at: int                     # коли AirRadar отримав повідомлення
    last_updated_at: int
    source_ids: tuple[str, ...] = ()
    independent_groups: tuple[str, ...] = ()
    official: bool = False
    expires_at: int = 0

    @property
    def source_count(self) -> int:
        return len(self.source_ids)

    @property
    def source_independence(self) -> int:
        return len(self.independent_groups)

    @property
    def ingest_lag_s(self) -> int:
        return max(0, self.received_at - self.observed_at)


def build_provenance(observed_at: int, received_at: int, sources: list[str], *,
                     official: bool = False, last_updated_at: int | None = None,
                     overrides: dict[str, str] | None = None, ttl_s: int = DEFAULT_TTL_S) -> Provenance:
    """Зібрати Provenance; репости/мережа одного власника (SOURCE_GROUPS) — одна незалежна група."""
    ids: list[str] = []
    groups: list[str] = []
    for s in sources:
        if s and s not in ids:
            ids.append(s)
        g = source_group(s, overrides)
        if g and g not in groups:
            groups.append(g)
    upd = last_updated_at if last_updated_at is not None else received_at
    return Provenance(observed_at, received_at, upd, tuple(ids), tuple(groups), official,
                      expires_at=observed_at + ttl_s)


def classify(prov: Provenance, now: int, *, required_independent: int = 2) -> Confidence:
    """Рівень довіри. Застарілість має пріоритет: стара інформація не може бути «підтверджена»."""
    if now >= prov.expires_at:
        return Confidence.STALE
    if prov.official:
        return Confidence.OFFICIAL
    if prov.source_independence >= required_independent:
        return Confidence.CONFIRMED
    if prov.source_independence == 1:
        return Confidence.PRELIMINARY
    return Confidence.UNCERTAIN


def is_fresh(prov: Provenance, now: int) -> bool:
    """Свіже = не прострочене і оброблене без великого лагу."""
    return now < prov.expires_at and prov.ingest_lag_s <= STALE_LAG_S


def describe(prov: Provenance, now: int) -> str:
    """Короткий людський опис для повідомлення: рівень + число незалежних джерел + вік."""
    level = classify(prov, now)
    age_min = max(0, (now - prov.observed_at) // 60)
    parts = [LABELS_UK[level]]
    if level in (Confidence.CONFIRMED, Confidence.PRELIMINARY):
        parts.append(f"незалежних джерел: {prov.source_independence}")
    parts.append(f"повідомлено {age_min} хв тому")
    if prov.ingest_lag_s > STALE_LAG_S:
        parts.append(f"оброблено із затримкою {prov.ingest_lag_s // 60} хв")
    return " · ".join(parts)


@dataclass
class RepostClusters:
    """Групування переказів одного повідомлення: n каналів, що переказали пост = 1 підтвердження."""
    clusters: list[tuple[str, set[str]]] = field(default_factory=list)  # (нормалізований текст, канали)

    @staticmethod
    def _norm(text: str) -> frozenset[str]:
        return frozenset(w for w in "".join(c.lower() if c.isalnum() else " " for c in text).split() if len(w) > 2)

    def add(self, source: str, text: str, threshold: float = 0.7) -> int:
        """Додати пост; повертає індекс кластера. Схожі (Jaccard ≥ threshold) тексти — один кластер."""
        words = self._norm(text)
        for i, (key, sources) in enumerate(self.clusters):
            kw = frozenset(key.split())
            union = words | kw
            if union and len(words & kw) / len(union) >= threshold:
                sources.add(source)
                return i
        self.clusters.append((" ".join(sorted(words)), {source}))
        return len(self.clusters) - 1

    def independent_count(self) -> int:
        """Скільки НЕЗАЛЕЖНИХ повідомлень (а не переказів) побачено."""
        return len(self.clusters)
