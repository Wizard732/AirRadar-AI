"""Регрессия: непубликуемые impact-посты обязаны попадать в журнал угроз.

До фикса ранний return в _process_message (publishable-гейт) стоял ДО
db.add_event/db.add_threat, поэтому посты со стадией "past" (прилёт,
последствия) вообще не записывались в БД: ETA-кнопка не могла накопить
пары пуск→прилёт и всегда отвечала «недостаточно данных».

Публикация при этом по-прежнему закрыта: impact не постится ни в канал,
ни подписчикам (правило: точки прилётов не публикуем).
"""

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from database import Database
from dedup import DedupCache
from eta import estimate_eta

main = None


def _import_main():
    global main
    if main is None:
        import main as _main
        main = _main
    return main


class FakeSummarizer:
    """summarize возвращает исходник, classify (entities) — пусто."""

    async def summarize(self, text: str, *a, **kw) -> str:
        return text

    async def classify(self, text: str, prompt: str, max_tokens: int = 30) -> str:
        return ""


class FakePublisher:
    def __init__(self):
        self.sent: list[str] = []
        self.edits: list[str] = []

    async def send(self, text: str, *a, **kw):
        self.sent.append(text)
        return {"chat": {"id": "chat"}, "message_id": 1}

    async def edit(self, chat_id, message_id, text: str, *a, **kw):
        self.edits.append(text)
        return True


def _event(text: str, ts: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        message=SimpleNamespace(text=text, date=ts, photo=None),
        chat=SimpleNamespace(username="test_source"),
    )


class ImpactJournalTests(unittest.TestCase):
    """stage=past пост пишется в threats, но не публикуется."""

    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.db = Database(self.file.name)
        _import_main()

    def tearDown(self):
        self.db.close()
        os.unlink(self.file.name)

    def _process(self, text: str, ts: datetime, publisher: FakePublisher) -> None:
        dedup = DedupCache(ttl=60)
        asyncio.run(
            main._process_message(
                _event(text, ts), FakeSummarizer(), publisher, dedup, self.db
            )
        )

    def test_past_impact_is_journaled_but_not_published(self):
        publisher = FakePublisher()
        ts = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
        # «Прилетіла/пошкодження» — детерминированная стадия past.
        self._process("Ракета прилетіла у Києві, є пошкодження", ts, publisher)

        rows = self.db._conn.execute(
            "SELECT threat_type, region FROM threats"
        ).fetchall()
        self.assertTrue(rows, "impact-пост обязан попасть в журнал угроз")
        regions = {r["region"] for r in rows}
        self.assertIn("kyivska", regions)
        # Публикации быть не должно: past не постится.
        self.assertEqual(publisher.sent, [])
        self.assertEqual(publisher.edits, [])
        # В журнале событий стадия impact.
        ev = self.db._conn.execute(
            "SELECT stage, outcome FROM threat_events"
        ).fetchall()
        self.assertTrue(any(r["stage"] == "impact" for r in ev), "events.stage=impact")

    def test_impact_feeds_eta_pairs(self):
        """Пуски + «вибухи» через живой конвейер дают available=True у estimate_eta.

        Реальный поток источников: «БпЛА курсом…» (launch) → «Чутко вибухи»
        (explosion). Пары пуск→прилёт считает estimate_eta по таблице threats.
        """
        publisher = FakePublisher()
        base = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
        from datetime import timedelta
        for i in range(3):
            self._process(
                f"БпЛА курсом на Київ, кількість невідома {i}",
                base + timedelta(minutes=i * 10),
                publisher,
            )
            self._process(
                f"Чутко вибухи у Києві {i}",
                base + timedelta(minutes=i * 10 + 15),
                publisher,
            )
        est = estimate_eta(self.db, "kyivska")
        self.assertTrue(est["available"], "3 пари мають давати ETA")
        self.assertGreaterEqual(est["samples"], 3)
        self.assertGreater(est["avg_seconds"], 0)
        # Пуски и «вибухи» (stage unknown) — публикуемые; отличия от past-постов
        # (не публикуются) покрыты предыдущим тестом.
        self.assertEqual(len(publisher.sent), 6)


if __name__ == "__main__":
    unittest.main()
